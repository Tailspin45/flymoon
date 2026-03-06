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
DIFF_THRESHOLD   = 12       # pixel intensity difference to flag as changed
DISK_MARGIN_PCT  = 0.03     # fraction of radius to trim from limb (jitter margin)
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

    # ── Output writer ──────────────────────────────────────────────────────────
    out = None
    out_path = path.with_name(path.stem + "_analyzed.mp4")
    if output_annotated:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(str(out_path), fourcc, fps, (w, h))

    # ── Main loop ─────────────────────────────────────────────────────────────
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
            # Write plain frame while still accumulating reference
            if out is not None:
                out.write(frame)
            frame_idx += 1
            continue

        if True:  # reference is always set here (frozen above)
            # Difference inside disk only
            diff = cv2.absdiff(gray, reference)
            diff_masked = cv2.bitwise_and(diff, diff, mask=mask)

            # Threshold
            _, binary = cv2.threshold(diff_masked, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)

            # Morphological cleanup
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
            binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

            # Blob analysis
            num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
                binary, connectivity=8
            )

            annotated = frame.copy()

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
                color = CONFIDENCE_COLORS.get(conf, ANNOTATION_COLOR)

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

                if out is not None:
                    cv2.rectangle(annotated, (bx, by), (bx + bw, by + bh), color, 2)
                    label_txt = f"{conf[0].upper()} {area}px"
                    cv2.putText(annotated, label_txt, (bx, max(0, by - 4)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)

            if out is not None:
                # Draw disk outline
                cv2.circle(annotated, (disk_cx, disk_cy), disk_radius,
                           (80, 80, 80), 1)
                out.write(annotated)

        frame_idx += 1

        if progress_cb and frame_idx % 30 == 0:
            progress_cb(frame_idx / max(1, total_frames))

    cap.release()
    if out is not None:
        out.release()
        logger.info(f"[Analyzer] Annotated video → {out_path.name}")

    # ── Group detections into transit events ───────────────────────────────────
    transit_events = _group_detections(detections, fps)

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
