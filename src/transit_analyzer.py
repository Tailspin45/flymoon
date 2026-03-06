"""
Transit video analyzer — Phase 1 (post-capture).

Detects objects crossing the solar or lunar disk in an already-saved MP4.
Operates entirely independently of the prediction system: any dark blob
that moves across the disk is a candidate, whether it's a jet, balloon,
bird, or anything else.

Usage (CLI):
    python -m src.transit_analyzer path/to/recording.mp4

Usage (API):
    from src.transit_analyzer import analyze_video
    result = analyze_video("static/captures/2026/03/recording_xyz.mp4")
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from src import logger

# ── Tunable parameters ────────────────────────────────────────────────────────
REFERENCE_WINDOW = 90       # frames in rolling reference (≈3 s at 30 fps)
MIN_BLOB_PIXELS  = 3        # ignore single hot pixels / sub-pixel noise
DIFF_THRESHOLD   = 8        # pixel intensity difference to flag as changed
DISK_MARGIN_PCT  = 0.02     # fraction of radius to trim from limb (jitter margin)
ANNOTATION_COLOR = (0, 0, 255)   # red (BGR)
CONFIDENCE_COLORS = {           # BGR by confidence tier
    "high":   (0,  0, 255),
    "medium": (0, 128, 255),
    "low":    (0, 200, 200),
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class BlobDetection:
    frame_index: int
    time_seconds: float
    x: int          # bounding-box centre x
    y: int          # bounding-box centre y
    width: int
    height: int
    area_px: int
    aspect_ratio: float   # width/height — >2 suggests elongated (aircraft)
    disk_x_norm: float    # x relative to disk centre, normalised by radius (-1..1)
    disk_y_norm: float
    confidence: str       # "high" | "medium" | "low"
    is_static: bool = False   # True = likely sunspot/static feature (filtered out)


@dataclass
class AnalysisResult:
    source_file: str
    duration_seconds: float
    fps: float
    frame_count: int
    disk_detected: bool
    disk_cx: Optional[int]  # disk centre x (pixels)
    disk_cy: Optional[int]
    disk_radius: Optional[int]
    detections: List[BlobDetection] = field(default_factory=list)
    transit_events: List[dict] = field(default_factory=list)  # grouped detections
    analyzed_at: str = ""
    error: Optional[str] = None


# ── Core analysis ──────────────────────────────────────────────────────────────

def _detect_disk(frame: np.ndarray) -> Optional[Tuple[int, int, int]]:
    """Return (cx, cy, radius) of the solar/lunar disk, or None."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # Blur first to suppress sunspot/crater noise
    blurred = cv2.GaussianBlur(gray, (9, 9), 2)
    h, w = blurred.shape
    min_r = min(h, w) // 8
    max_r = min(h, w) // 2

    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=min(h, w) // 2,
        param1=50,
        param2=30,
        minRadius=min_r,
        maxRadius=max_r,
    )
    if circles is not None:
        c = np.round(circles[0][0]).astype(int)
        return int(c[0]), int(c[1]), int(c[2])

    # Fallback: threshold bright region and fit min-enclosing circle
    _, thresh = cv2.threshold(blurred, 200, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        largest = max(contours, key=cv2.contourArea)
        if cv2.contourArea(largest) > (min_r ** 2 * np.pi):
            (cx, cy), radius = cv2.minEnclosingCircle(largest)
            return int(cx), int(cy), int(radius)
    return None


def _disk_mask(shape: Tuple[int, int], cx: int, cy: int, radius: int,
               margin_pct: float = DISK_MARGIN_PCT) -> np.ndarray:
    """Binary mask: 255 inside disk (minus limb margin), 0 outside."""
    mask = np.zeros(shape[:2], dtype=np.uint8)
    inner_r = max(1, int(radius * (1.0 - margin_pct)))
    cv2.circle(mask, (cx, cy), inner_r, 255, -1)
    return mask


def _reencode_h264(src: Path, dst: Path) -> None:
    """Re-encode an OpenCV mp4v video to H.264 using FFmpeg so browsers can play it."""
    import subprocess
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-movflags", "+faststart",
             str(dst)],
            check=True,
            capture_output=True,
        )
        src.unlink(missing_ok=True)  # remove temp file
    except Exception as exc:
        logger.warning(f"[Analyzer] FFmpeg re-encode failed ({exc}), keeping mp4v file")
        # Fall back: just rename temp to final
        src.rename(dst)


def _confidence(blob_area: int, disk_radius: int) -> str:
    frac = blob_area / max(1, np.pi * disk_radius ** 2)
    if frac > 0.002:
        return "high"
    if frac > 0.0003:
        return "medium"
    return "low"


def analyze_video(
    video_path: str,
    output_annotated: bool = True,
    progress_cb=None,
) -> AnalysisResult:
    """
    Analyze a saved MP4 for transiting objects.

    Parameters
    ----------
    video_path : str
        Path to the MP4 recording.
    output_annotated : bool
        If True, write an annotated copy alongside the source with suffix
        ``_analyzed.mp4``.
    progress_cb : callable(fraction: float) | None
        Optional progress callback, called with 0.0–1.0.

    Returns
    -------
    AnalysisResult
        Detection metadata. Also written as ``<video>_analysis.json``.
    """
    from datetime import datetime, timezone

    path = Path(video_path)
    if not path.exists():
        return AnalysisResult(
            source_file=str(path),
            duration_seconds=0,
            fps=0,
            frame_count=0,
            disk_detected=False,
            disk_cx=None, disk_cy=None, disk_radius=None,
            error=f"File not found: {path}",
            analyzed_at=datetime.now(timezone.utc).isoformat(),
        )

    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = total_frames / fps

    # For short clips, use fewer reference frames (min 10, or half the clip)
    ref_window = min(REFERENCE_WINDOW, max(10, total_frames // 2))

    logger.info(f"[Analyzer] {path.name}: {total_frames} frames @ {fps:.1f} fps, {w}x{h}, ref_window={ref_window}")

    # ── Probe for disk in first 30 frames ─────────────────────────────────────
    disk = None
    probe_frames = []
    for _ in range(min(30, total_frames)):
        ok, frame = cap.read()
        if not ok:
            break
        probe_frames.append(frame)
        if disk is None:
            disk = _detect_disk(frame)

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # rewind

    disk_detected = disk is not None
    disk_cx = disk_cy = disk_radius = None
    if disk:
        disk_cx, disk_cy, disk_radius = disk
        logger.info(f"[Analyzer] Disk at ({disk_cx},{disk_cy}) r={disk_radius}px")
    else:
        logger.warning("[Analyzer] No disk detected — scanning full frame")
        # Use full frame with a generous virtual disk
        disk_cy, disk_cx = h // 2, w // 2
        disk_radius = min(h, w) // 2

    # ── Build reference from first REFERENCE_WINDOW frames ───────────────────
    # We freeze the reference after the initial window rather than rolling it,
    # so that a transit happening early in the clip doesn't contaminate the
    # baseline and become invisible.
    from collections import deque
    ref_buffer: deque = deque(maxlen=ref_window)
    reference = None          # frozen once buffer is full
    mask = _disk_mask((h, w), disk_cx, disk_cy, disk_radius)

    # ── Output writer — write to temp file, re-encode to H.264 via FFmpeg ────
    out = None
    temp_path = path.with_name(path.stem + "_analyzed_tmp.mp4")
    out_path   = path.with_name(path.stem + "_analyzed.mp4")
    if output_annotated:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(str(temp_path), fourcc, fps, (w, h))

    # ── Main detection loop (no annotation yet) ─────────────────────────────
    detections: List[BlobDetection] = []
    frame_idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Build reference from early frames, then freeze it
        if reference is None:
            ref_buffer.append(gray)
            if len(ref_buffer) >= ref_window:
                reference = np.median(np.stack(ref_buffer), axis=0).astype(np.uint8)
                logger.info(f"[Analyzer] Reference locked at frame {frame_idx}")
            frame_idx += 1
            continue

        # Blur both to suppress sensor noise while preserving real transit blobs
        gray_blur = cv2.GaussianBlur(gray, (5, 5), 0)
        ref_blur = cv2.GaussianBlur(reference, (5, 5), 0)

        # Difference inside disk only
        diff = cv2.absdiff(gray_blur, ref_blur)
        diff_masked = cv2.bitwise_and(diff, diff, mask=mask)

        # Threshold
        _, binary = cv2.threshold(diff_masked, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)

        # Morphological cleanup — use small kernel to preserve tiny transit blobs
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)  # fill gaps first
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)   # then remove noise

        # Blob analysis
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
            binary, connectivity=8
        )

        for lbl in range(1, num_labels):  # skip background (0)
            area = int(stats[lbl, cv2.CC_STAT_AREA])
            if area < MIN_BLOB_PIXELS:
                continue

            bx = int(stats[lbl, cv2.CC_STAT_LEFT])
            by = int(stats[lbl, cv2.CC_STAT_TOP])
            bw = int(stats[lbl, cv2.CC_STAT_WIDTH])
            bh = int(stats[lbl, cv2.CC_STAT_HEIGHT])
            bcx = bx + bw // 2
            bcy = by + bh // 2

            ar = bw / max(1, bh)
            conf = _confidence(area, disk_radius)

            dx_norm = (bcx - disk_cx) / max(1, disk_radius)
            dy_norm = (bcy - disk_cy) / max(1, disk_radius)

            det = BlobDetection(
                frame_index=frame_idx,
                time_seconds=round(frame_idx / fps, 3),
                x=bcx, y=bcy,
                width=bw, height=bh,
                area_px=area,
                aspect_ratio=round(ar, 2),
                disk_x_norm=round(dx_norm, 3),
                disk_y_norm=round(dy_norm, 3),
                confidence=conf,
            )
            detections.append(det)

        frame_idx += 1

        if progress_cb and frame_idx % 30 == 0:
            progress_cb(frame_idx / max(1, total_frames) * 0.7)  # 70% for detection

    cap.release()

    # ── Filter out static blobs (sunspots) ───────────────────────────────────
    # Sunspots appear at the same position across many frames.  A real transit
    # moves across the disk.  Cluster detections by spatial proximity and mark
    # clusters present in >50% of detection frames as static.
    detections = _filter_static_blobs(detections, proximity_px=8)
    moving_detections = [d for d in detections if not d.is_static]
    n_static = sum(1 for d in detections if d.is_static)
    if n_static:
        logger.info(f"[Analyzer] Filtered {n_static} static-blob detections (sunspots)")

    # ── Group detections into transit events ───────────────────────────────────
    transit_events = _group_detections(moving_detections, fps)

    # ── Annotation pass (second read of video) ────────────────────────────────
    # Now that we know which blobs are static vs transit, re-read and annotate
    # with correct colors: red/orange for transits, gray for filtered sunspots.
    temp_path = path.with_name(path.stem + "_analyzed_tmp.mp4")
    out_path   = path.with_name(path.stem + "_analyzed.mp4")
    if output_annotated:
        _write_annotated_video(
            path, temp_path, fps, w, h, total_frames,
            detections, disk_cx, disk_cy, disk_radius,
            progress_cb, 0.7,  # start at 70% progress
        )
        _reencode_h264(temp_path, out_path)
        logger.info(f"[Analyzer] Annotated video → {out_path.name}")

    result = AnalysisResult(
        source_file=str(path),
        duration_seconds=round(duration, 2),
        fps=round(fps, 2),
        frame_count=total_frames,
        disk_detected=disk_detected,
        disk_cx=disk_cx,
        disk_cy=disk_cy,
        disk_radius=disk_radius,
        detections=detections,
        transit_events=transit_events,
        analyzed_at=datetime.now(timezone.utc).isoformat(),
    )

    # Write JSON sidecar
    sidecar = path.with_name(path.stem + "_analysis.json")
    _write_sidecar(result, sidecar)
    logger.info(
        f"[Analyzer] Done: {len(transit_events)} event(s), "
        f"{len(detections)} blob detection(s) → {sidecar.name}"
    )
    return result


STATIC_COLOR = (140, 140, 140)  # gray for sunspots/static features


def _write_annotated_video(
    src: Path, dst: Path, fps: float, w: int, h: int, total_frames: int,
    detections: List[BlobDetection],
    disk_cx: int, disk_cy: int, disk_radius: int,
    progress_cb=None, progress_offset: float = 0.0,
):
    """Second pass: re-read source video and overlay annotations."""
    # Index detections by frame for fast lookup
    from collections import defaultdict
    by_frame: dict = defaultdict(list)
    for d in detections:
        by_frame[d.frame_index].append(d)

    cap = cv2.VideoCapture(str(src))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(dst), fourcc, fps, (w, h))
    frame_idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        annotated = frame
        dets = by_frame.get(frame_idx)
        if dets:
            annotated = frame.copy()
            for d in dets:
                if d.is_static:
                    color = STATIC_COLOR
                    label = f"S {d.area_px}px"  # S = sunspot/static
                    thickness = 1
                else:
                    color = CONFIDENCE_COLORS.get(d.confidence, ANNOTATION_COLOR)
                    label = f"{d.confidence[0].upper()} {d.area_px}px"
                    thickness = 3
                half_w = max(d.width // 2 + 4, 8)
                half_h = max(d.height // 2 + 4, 8)
                cv2.ellipse(annotated, (d.x, d.y), (half_w, half_h),
                            0, 0, 360, color, thickness)
                cv2.putText(annotated, label,
                            (d.x - d.width // 2, max(12, d.y - d.height // 2 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)

            # Disk outline
            cv2.circle(annotated, (disk_cx, disk_cy), disk_radius, (0, 200, 200), 2)

        # Timestamp
        ts = f"{frame_idx / fps:.2f}s"
        cv2.putText(annotated, ts, (8, h - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1, cv2.LINE_AA)

        out.write(annotated)
        frame_idx += 1

        if progress_cb and frame_idx % 30 == 0:
            frac = progress_offset + (1.0 - progress_offset) * (frame_idx / max(1, total_frames))
            progress_cb(frac)

    cap.release()
    out.release()


def _filter_static_blobs(detections: List[BlobDetection],
                        proximity_px: int = 8) -> List[BlobDetection]:
    """Mark blobs that stay at the same position across many frames as static (sunspots).

    Algorithm: group all detections by spatial proximity.  If a spatial cluster
    spans more than 50% of the frames that contain *any* detection, it's a
    static feature — sunspot, crater, sensor hot-spot — not a transit.
    """
    if not detections:
        return detections

    # Unique frames that contain at least one detection
    det_frames = set(d.frame_index for d in detections)
    n_det_frames = len(det_frames)
    if n_det_frames < 3:
        return detections  # too few frames to judge

    # Simple greedy spatial clustering by centroid proximity
    clusters: List[List[int]] = []  # each cluster = list of indices into detections
    assigned = set()
    for i, d in enumerate(detections):
        if i in assigned:
            continue
        cluster = [i]
        assigned.add(i)
        for j in range(i + 1, len(detections)):
            if j in assigned:
                continue
            if abs(detections[j].x - d.x) <= proximity_px and abs(detections[j].y - d.y) <= proximity_px:
                cluster.append(j)
                assigned.add(j)
        clusters.append(cluster)

    # Mark clusters that appear in >50% of detection-containing frames as static
    threshold = n_det_frames * 0.5
    for cluster in clusters:
        unique_frames = set(detections[i].frame_index for i in cluster)
        if len(unique_frames) > threshold:
            for i in cluster:
                detections[i].is_static = True

    return detections


def _group_detections(detections: List[BlobDetection], fps: float,
                      gap_seconds: float = 0.5) -> List[dict]:
    """Merge per-frame detections into discrete transit events."""
    if not detections:
        return []

    events = []
    current: List[BlobDetection] = [detections[0]]

    for det in detections[1:]:
        gap = det.time_seconds - current[-1].time_seconds
        if gap <= gap_seconds:
            current.append(det)
        else:
            events.append(_summarize_event(current))
            current = [det]
    events.append(_summarize_event(current))
    return events


def _summarize_event(blobs: List[BlobDetection]) -> dict:
    t_start = blobs[0].time_seconds
    t_end   = blobs[-1].time_seconds
    best    = max(blobs, key=lambda b: b.area_px)
    confs   = [b.confidence for b in blobs]
    overall = "high" if "high" in confs else ("medium" if "medium" in confs else "low")
    # Estimate direction from first→last blob centroid
    dx = blobs[-1].disk_x_norm - blobs[0].disk_x_norm
    dy = blobs[-1].disk_y_norm - blobs[0].disk_y_norm
    import math
    heading_deg = round(math.degrees(math.atan2(dy, dx)), 1) if len(blobs) > 1 else None
    speed_norm = round(math.hypot(dx, dy) / max(0.001, t_end - t_start), 3) if t_end > t_start else None
    return {
        "start_seconds":  t_start,
        "end_seconds":    t_end,
        "duration_ms":    round((t_end - t_start) * 1000),
        "peak_area_px":   best.area_px,
        "peak_aspect_ratio": best.aspect_ratio,
        "confidence":     overall,
        "heading_deg":    heading_deg,
        "speed_norm_per_s": speed_norm,
        "frame_count":    len(blobs),
    }


def _write_sidecar(result: AnalysisResult, path: Path):
    data = {
        "source_file":      result.source_file,
        "analyzed_at":      result.analyzed_at,
        "duration_seconds": result.duration_seconds,
        "fps":              result.fps,
        "frame_count":      result.frame_count,
        "disk_detected":    result.disk_detected,
        "disk_cx":          result.disk_cx,
        "disk_cy":          result.disk_cy,
        "disk_radius":      result.disk_radius,
        "transit_events":   result.transit_events,
        "detection_count":  len(result.detections),
        "error":            result.error,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m src.transit_analyzer <video.mp4>")
        sys.exit(1)
    r = analyze_video(sys.argv[1])
    print(json.dumps({
        "events": r.transit_events,
        "detections": len(r.detections),
        "disk": {"cx": r.disk_cx, "cy": r.disk_cy, "r": r.disk_radius},
    }, indent=2))
