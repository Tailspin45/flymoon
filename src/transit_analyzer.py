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
import platform
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from src import logger

# ── Default tunable parameters (can be overridden per-call) ───────────────────
REFERENCE_WINDOW = 90       # frames in rolling reference (≈3 s at 30 fps)
MIN_BLOB_PIXELS  = 20       # ignore tiny noise; a real transit blob is ≥20 px²
DIFF_THRESHOLD   = 15       # pixel intensity difference — tuned for post-stabilization noise floor
DISK_MARGIN_PCT  = 0.12     # fraction of radius to trim from limb (atmosphere margin)


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
    composite_image: Optional[str] = None  # path to composite still image
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


def _best_fourcc() -> tuple:
    """Return (fourcc, ext) for the best available H.264-compatible codec."""
    if platform.system() == "Darwin":
        return cv2.VideoWriter_fourcc(*"avc1"), ".mp4"
    # Linux/Windows: write raw mp4v then re-encode with ffmpeg
    return cv2.VideoWriter_fourcc(*"mp4v"), ".mp4"


def _reencode_h264(src: Path, dst: Path) -> None:
    """Re-encode temp video to H.264 via FFmpeg (non-macOS), or just rename on macOS."""
    if not src.exists() or src.stat().st_size == 0:
        logger.warning(f"[Analyzer] Temp file missing or empty: {src.name}, skipping")
        return
    if platform.system() == "Darwin":
        # avc1 already produces H.264 — just rename
        src.rename(dst)
        return
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-movflags", "+faststart",
             str(dst)],
            check=True,
            capture_output=True,
        )
        src.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning(f"[Analyzer] FFmpeg re-encode failed ({exc}), keeping as-is")
        if src.exists():
            src.rename(dst)


def _stabilize_frame(
    frame_gray: np.ndarray,
    ref_gray_f32: np.ndarray,
) -> Tuple[np.ndarray, Tuple[float, float]]:
    """
    Translate ``frame_gray`` to align with the reference via phase correlation
    (FFT-based — fast, no feature matching needed).

    Returns ``(stabilized_gray, (dx, dy))``.  If the detected shift is
    unreasonably large (> 50 px) the original frame is returned unchanged so
    a single bad frame cannot corrupt the whole sequence.
    """
    frame_f32 = frame_gray.astype(np.float32)
    (dx, dy), _ = cv2.phaseCorrelate(ref_gray_f32, frame_f32)
    if abs(dx) > 50 or abs(dy) > 50:
        return frame_gray, (0.0, 0.0)
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    h, w = frame_gray.shape
    stabilized = cv2.warpAffine(
        frame_gray, M, (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    return stabilized, (dx, dy)


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
    diff_threshold: int = None,
    min_blob_pixels: int = None,
    disk_margin_pct: float = None,
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
    diff_threshold : int | None
        Override DIFF_THRESHOLD (pixel intensity difference, default 8).
    min_blob_pixels : int | None
        Override MIN_BLOB_PIXELS (minimum blob area, default 3).
    disk_margin_pct : float | None
        Override DISK_MARGIN_PCT (fraction of radius to trim, default 0.05).

    Returns
    -------
    AnalysisResult
        Detection metadata. Also written as ``<video>_analysis.json``.
    """
    from datetime import datetime, timezone

    # Resolve per-call overrides
    _diff_threshold = diff_threshold if diff_threshold is not None else DIFF_THRESHOLD
    _min_blob_pixels = min_blob_pixels if min_blob_pixels is not None else MIN_BLOB_PIXELS
    _disk_margin_pct = disk_margin_pct if disk_margin_pct is not None else DISK_MARGIN_PCT

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

    # Refuse to re-analyze an already-analyzed file (jpg output, not a video)
    if path.stem.startswith("analyzed_"):
        return AnalysisResult(
            source_file=str(path),
            duration_seconds=0,
            fps=0,
            frame_count=0,
            disk_detected=False,
            disk_cx=None, disk_cy=None, disk_radius=None,
            error=f"File appears already analyzed: {path.name}",
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
    logger.info(f"[Analyzer] Params: diff_threshold={_diff_threshold}, min_blob_pixels={_min_blob_pixels}, disk_margin={_disk_margin_pct:.0%}")

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
    ref_buffer_bgr: deque = deque(maxlen=ref_window)  # BGR frames for composite bg
    reference = None          # frozen once buffer is full (grayscale median)
    ref_gray_f32 = None       # float32 version for phase-correlation stabilization
    ref_bgr_frame = None      # color frame from reference window (composite background)
    mask = _disk_mask((h, w), disk_cx, disk_cy, disk_radius, _disk_margin_pct)

    # ── Output writer — write to temp file, re-encode to H.264 via FFmpeg ────
    out = None
    base_stem = path.stem.replace("_analyzed", "")  # always derive from original name
    temp_path = path.with_name(base_stem + "_analyzed_tmp.mp4")
    out_path   = path.with_name(base_stem + "_analyzed.mp4")
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.unlink(missing_ok=True)  # remove any stale tmp from previous failed run
    if output_annotated:
        fourcc, _ = _best_fourcc()
        out = cv2.VideoWriter(str(temp_path), fourcc, fps, (w, h))

    # ── Detection helper (shared by reference-window scan & main scan) ───────
    ref_blur_cached = [None]  # mutable container so inner fn can cache
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))

    def _detect_blobs_in_frame(gray: np.ndarray, fidx: int) -> List[BlobDetection]:
        """Run blob detection on a single stabilized grayscale frame."""
        gray_s, _ = _stabilize_frame(gray, ref_gray_f32)
        gray_blur = cv2.GaussianBlur(gray_s, (5, 5), 0)
        if ref_blur_cached[0] is None:
            ref_blur_cached[0] = cv2.GaussianBlur(reference, (5, 5), 0)
        diff = cv2.absdiff(gray_blur, ref_blur_cached[0])
        diff_masked = cv2.bitwise_and(diff, diff, mask=mask)
        _, binary = cv2.threshold(diff_masked, _diff_threshold, 255, cv2.THRESH_BINARY)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
            binary, connectivity=8
        )
        blobs: List[BlobDetection] = []
        for lbl in range(1, num_labels):
            area = int(stats[lbl, cv2.CC_STAT_AREA])
            if area < _min_blob_pixels:
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
            blobs.append(BlobDetection(
                frame_index=fidx,
                time_seconds=round(fidx / fps, 3),
                x=bcx, y=bcy,
                width=bw, height=bh,
                area_px=area,
                aspect_ratio=round(ar, 2),
                disk_x_norm=round(dx_norm, 3),
                disk_y_norm=round(dy_norm, 3),
                confidence=conf,
            ))
        return blobs

    # ── Main detection loop ───────────────────────────────────────────────────
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
            ref_buffer_bgr.append(frame)
            if len(ref_buffer) >= ref_window:
                reference = np.median(np.stack(ref_buffer), axis=0).astype(np.uint8)
                ref_gray_f32 = reference.astype(np.float32)
                ref_bgr_frame = list(ref_buffer_bgr)[len(ref_buffer_bgr) // 2].copy()
                logger.info(f"[Analyzer] Reference locked at frame {frame_idx}")
                # Scan the reference-window frames we just collected
                for ri, ref_gray_frame in enumerate(ref_buffer):
                    detections.extend(_detect_blobs_in_frame(ref_gray_frame, ri))
            frame_idx += 1
            continue

        gray_s, _ = _stabilize_frame(gray, ref_gray_f32)
        detections.extend(_detect_blobs_in_frame(gray, frame_idx))

        frame_idx += 1

        if progress_cb and frame_idx % 30 == 0:
            progress_cb(frame_idx / max(1, total_frames) * 0.7)

    cap.release()

    # ── Filter out static blobs (sunspots) ───────────────────────────────────
    # Sunspots appear at the same position across many frames.  A real transit
    # moves across the disk.  Cluster detections by spatial proximity and mark
    # clusters present in >50% of detection frames as static.
    detections = _filter_static_blobs(detections, proximity_px=30)
    moving_detections = [d for d in detections if not d.is_static]
    n_static = sum(1 for d in detections if d.is_static)

    # ── Filter moving detections for transit coherence ──────────────────────
    # A real transit traces a roughly linear path in under ~2 seconds.
    # Anything that hangs around the same spot for many seconds is shimmer.
    moving_detections = _filter_transit_coherence(moving_detections, fps)
    if moving_detections:
        logger.info(f"[Analyzer] After coherence filter: {len(moving_detections)} moving detections")
    else:
        logger.info("[Analyzer] No coherent transit paths found")

    # Update the master detections list: mark non-coherent moving blobs as static
    # so the composite only draws confirmed transit silhouettes.
    coherent_ids = {id(d) for d in moving_detections}
    for d in detections:
        if not d.is_static and id(d) not in coherent_ids:
            d.is_static = True

    # ── Group detections into transit events ───────────────────────────────────
    transit_events = _group_detections(moving_detections, fps)

    # ── Composite still image (replaces annotated video) ─────────────────────
    # One background frame with transit positions overlaid.
    composite_path = path.with_name("analyzed_" + base_stem + ".jpg")
    composite_path.parent.mkdir(parents=True, exist_ok=True)
    composite_image_str = None
    if output_annotated:
        composite_image_str = _write_composite_image(
            path, composite_path, detections, disk_cx, disk_cy, disk_radius,
            progress_cb, bg_frame=ref_bgr_frame,
            reference_gray=reference, ref_gray_f32=ref_gray_f32,
        )
        if composite_image_str:
            logger.info(f"[Analyzer] Composite image → {composite_path.name}")
        else:
            logger.warning("[Analyzer] Composite image write failed")

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
        composite_image=composite_image_str,
        analyzed_at=datetime.now(timezone.utc).isoformat(),
    )

    # Write JSON sidecar alongside the composite image
    sidecar = path.with_name("analyzed_" + base_stem + "_analysis.json")
    _write_sidecar(result, sidecar)
    logger.info(
        f"[Analyzer] Done: {len(transit_events)} event(s), "
        f"{len(detections)} blob detection(s) → {sidecar.name}"
    )
    return result


STATIC_COLOR = (140, 140, 140)  # gray for sunspots/static features
TRANSIT_TINT  = np.array([40, 40, 200], dtype=np.uint8)  # reddish tint for transit silhouettes


def _write_composite_image(
    src: Path,
    dst: Path,
    detections: List[BlobDetection],
    disk_cx: Optional[int],
    disk_cy: Optional[int],
    disk_radius: Optional[int],
    progress_cb=None,
    bg_frame: Optional[np.ndarray] = None,
    reference_gray: Optional[np.ndarray] = None,
    ref_gray_f32: Optional[np.ndarray] = None,
) -> Optional[str]:
    """
    Build a composite still image showing transit silhouettes and sunspots.

    For each transit frame, the actual darkened pixels (object silhouette)
    are extracted and alpha-blended onto the background.  Small fast movers
    appear as a trail of dark dots; large targets produce dramatic time-lapse
    overlays.  Sunspots get one grey circle per cluster.

    Returns the output path string on success, None on failure.
    """
    if bg_frame is not None:
        canvas = bg_frame.copy()
    else:
        cap = cv2.VideoCapture(str(src))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        mid = max(0, total_frames // 2)
        cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
        ok, canvas = cap.read()
        cap.release()
        if not ok or canvas is None:
            logger.error("[Analyzer] Could not read background frame for composite image")
            return None

    h, w = canvas.shape[:2]
    if progress_cb:
        progress_cb(0.75)

    # Separate static (sunspot) and transit detections
    static_dets = [d for d in detections if d.is_static]
    transit_dets = [d for d in detections if not d.is_static]

    # ── Alpha-blend transit silhouettes from source frames ────────────────
    if transit_dets and reference_gray is not None:
        # Build a set of unique frame indices that contain transit detections
        from collections import defaultdict
        frames_needed: dict = defaultdict(list)
        for d in transit_dets:
            frames_needed[d.frame_index].append(d)

        cap = cv2.VideoCapture(str(src))
        frame_idx = 0
        blended_count = 0

        while True:
            ok, frame = cap.read()
            if not ok:
                break

            if frame_idx in frames_needed:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                # Stabilize this frame same as during detection
                if ref_gray_f32 is not None:
                    gray, _ = _stabilize_frame(gray, ref_gray_f32)

                for det in frames_needed[frame_idx]:
                    # Extract bounding box around this detection (with padding)
                    pad = max(det.width, det.height, 8)
                    x1 = max(0, det.x - det.width // 2 - pad)
                    y1 = max(0, det.y - det.height // 2 - pad)
                    x2 = min(w, det.x + det.width // 2 + pad)
                    y2 = min(h, det.y + det.height // 2 + pad)

                    # Diff patch: where this frame is darker than the reference
                    ref_patch = reference_gray[y1:y2, x1:x2].astype(np.int16)
                    cur_patch = gray[y1:y2, x1:x2].astype(np.int16)
                    darkening = np.clip(ref_patch - cur_patch, 0, 255).astype(np.uint8)

                    # Threshold to get just the object silhouette
                    _, sil_mask = cv2.threshold(darkening, 10, 255, cv2.THRESH_BINARY)

                    if sil_mask.sum() == 0:
                        # Fallback: draw a small red marker
                        cv2.circle(canvas, (det.x, det.y), max(3, det.width // 3),
                                   (0, 0, 220), -1)
                        continue

                    # Darken + tint the canvas where the silhouette is
                    roi = canvas[y1:y2, x1:x2]
                    alpha = (sil_mask.astype(np.float32) / 255.0)[:, :, np.newaxis]
                    # Blend: make it darker and slightly red
                    darkened = (roi.astype(np.float32) * 0.3).astype(np.uint8)
                    tinted = cv2.addWeighted(darkened, 0.7, np.full_like(roi, TRANSIT_TINT), 0.3, 0)
                    canvas[y1:y2, x1:x2] = (roi * (1 - alpha) + tinted * alpha).astype(np.uint8)
                    blended_count += 1

            frame_idx += 1
            if progress_cb and frame_idx % 60 == 0:
                progress_cb(0.75 + 0.15 * min(1.0, frame_idx / max(1, cap.get(cv2.CAP_PROP_FRAME_COUNT))))

        cap.release()
        if blended_count:
            logger.info(f"[Analyzer] Composited {blended_count} transit silhouettes from {len(frames_needed)} frames")

    elif transit_dets:
        # No reference available — fall back to red dot markers
        for d in transit_dets:
            cv2.circle(canvas, (d.x, d.y), max(4, d.width // 3), (0, 0, 220), -1)

    # ── Draw one bounding circle per transit track ──────────────────────
    if transit_dets:
        import math
        # Group transit detections into tracks (consecutive frames, near each other)
        tdets = sorted(transit_dets, key=lambda d: d.frame_index)
        tracks: list = [[tdets[0]]]
        for d in tdets[1:]:
            prev = tracks[-1][-1]
            gap = d.frame_index - prev.frame_index
            dist = math.hypot(d.x - prev.x, d.y - prev.y)
            if gap <= 5 and dist < 200:
                tracks[-1].append(d)
            else:
                tracks.append([d])

        for track in tracks:
            xs = [d.x for d in track]
            ys = [d.y for d in track]
            tcx = int(sum(xs) / len(xs))
            tcy = int(sum(ys) / len(ys))
            spread = max(max(xs) - min(xs), max(ys) - min(ys))
            tr = max(20, spread // 2 + 15)
            cv2.circle(canvas, (tcx, tcy), tr, (0, 0, 255), 2)
    if static_dets:
        PROX = 30
        used: set = set()
        for i, sd in enumerate(static_dets):
            if i in used:
                continue
            cluster = [sd]
            used.add(i)
            for j in range(i + 1, len(static_dets)):
                if j in used:
                    continue
                od = static_dets[j]
                if abs(sd.x - od.x) <= PROX and abs(sd.y - od.y) <= PROX:
                    cluster.append(od)
                    used.add(j)
            cx = int(sum(c.x for c in cluster) / len(cluster))
            cy = int(sum(c.y for c in cluster) / len(cluster))
            r = max(12, int(max(max(c.width, c.height) for c in cluster)) + 6)
            cv2.circle(canvas, (cx, cy), r, STATIC_COLOR, 2)

    # ── Draw disk boundary (yellow) LAST so it's on top ──────────────────
    if disk_cx is not None and disk_cy is not None and disk_radius is not None:
        cv2.circle(canvas, (disk_cx, disk_cy), disk_radius, (0, 255, 255), 2)

    if progress_cb:
        progress_cb(0.95)

    ok = cv2.imwrite(str(dst), canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        logger.error(f"[Analyzer] imwrite failed: {dst}")
        return None

    if progress_cb:
        progress_cb(1.0)

    return str(dst)


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
    fourcc, _ = _best_fourcc()
    dst.parent.mkdir(parents=True, exist_ok=True)
    out = cv2.VideoWriter(str(dst), fourcc, fps, (w, h))
    if not out.isOpened():
        logger.error(f"[Analyzer] VideoWriter failed to open: {dst}")
        cap.release()
        return
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
                    color = (0, 0, 255)  # red
                    label = f"T {d.area_px}px"  # T = transit
                    thickness = 3
                half_w = max(d.width // 2 + 4, 8)
                half_h = max(d.height // 2 + 4, 8)
                cv2.ellipse(annotated, (d.x, d.y), (half_w, half_h),
                            0, 0, 360, color, thickness)
                cv2.putText(annotated, label,
                            (d.x - d.width // 2, max(12, d.y - d.height // 2 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)

            # Disk outline
            cv2.circle(annotated, (disk_cx, disk_cy), disk_radius, (0, 255, 255), 2)

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

    # Mark clusters that appear in >25% of detection-containing frames as static.
    # (50% was too lenient — sunspots with intermittent detection escaped.)
    threshold = n_det_frames * 0.25
    for cluster in clusters:
        unique_frames = set(detections[i].frame_index for i in cluster)
        if len(unique_frames) > threshold:
            for i in cluster:
                detections[i].is_static = True

    return detections


def _filter_transit_coherence(
    detections: List[BlobDetection],
    fps: float,
    max_duration_sec: float = 3.0,
    min_travel_px: float = 40.0,
    min_speed_px_s: float = 80.0,
    max_link_px: float = 150.0,
) -> List[BlobDetection]:
    """Keep only detections that form coherent transit-like paths.

    A real transit is ONE object crossing the disk in 0.1–3 s at high speed.
    This filter builds individual object tracks by linking the nearest blob
    in consecutive frames, then evaluates each track for transit-like motion.

    Algorithm:
      1. Group moving detections into temporal runs (≤0.5 s gap).
      2. Within each run, build object tracks by greedy nearest-neighbor
         linking across frames (max ``max_link_px`` per frame step).
      3. Evaluate each track: must travel ≥40 px, speed ≥80 px/s,
         and follow a roughly linear path.
      4. Return only detections that belong to qualifying tracks.
    """
    if not detections:
        return []

    import math
    from collections import defaultdict

    dets = sorted(detections, key=lambda d: (d.time_seconds, d.x))

    # ── 1. Temporal runs ────────────────────────────────────────────────
    runs: List[List[BlobDetection]] = [[dets[0]]]
    for d in dets[1:]:
        if d.time_seconds - runs[-1][-1].time_seconds <= 0.5:
            runs[-1].append(d)
        else:
            runs.append([d])

    kept: List[BlobDetection] = []

    for run in runs:
        duration = run[-1].time_seconds - run[0].time_seconds
        if duration > max_duration_sec:
            continue

        # ── 2. Build per-frame blob lists ───────────────────────────────
        by_frame: dict = defaultdict(list)
        for d in run:
            by_frame[d.frame_index].append(d)
        frame_ids = sorted(by_frame.keys())

        if len(frame_ids) < 2:
            # Single-frame: keep only large blobs (likely real object)
            if run[0].area_px >= 200:
                kept.extend(run)
            continue

        # ── 3. Greedy nearest-neighbor tracking ─────────────────────────
        # Each "track" is a list of BlobDetections, one per frame.
        # Start a track from every blob in the first frame, then extend
        # greedily.  Blobs not linked to any track start new tracks.
        tracks: List[List[BlobDetection]] = []
        used_ids: set = set()  # id(det) of already-assigned detections

        # Seed tracks from the first frame
        for d in by_frame[frame_ids[0]]:
            tracks.append([d])
            used_ids.add(id(d))

        for fi in frame_ids[1:]:
            candidates = by_frame[fi]
            # For each existing track, try to extend with nearest candidate
            claimed: set = set()
            for track in tracks:
                tail = track[-1]
                best_d = None
                best_dist = max_link_px
                for c in candidates:
                    if id(c) in claimed:
                        continue
                    dist = math.hypot(c.x - tail.x, c.y - tail.y)
                    if dist < best_dist:
                        best_dist = dist
                        best_d = c
                if best_d is not None:
                    track.append(best_d)
                    claimed.add(id(best_d))
                    used_ids.add(id(best_d))
            # Start new tracks from unclaimed blobs
            for c in candidates:
                if id(c) not in claimed:
                    tracks.append([c])
                    used_ids.add(id(c))

        # ── 4. Evaluate each track ──────────────────────────────────────
        for track in tracks:
            if len(track) < 3:
                continue  # need ≥3 points to confirm a path

            t_dur = track[-1].time_seconds - track[0].time_seconds
            if t_dur > max_duration_sec or t_dur <= 0:
                continue

            # Travel (3-frame averaged endpoints)
            n = min(3, len(track))
            cx0 = sum(d.x for d in track[:n]) / n
            cy0 = sum(d.y for d in track[:n]) / n
            cx1 = sum(d.x for d in track[-n:]) / n
            cy1 = sum(d.y for d in track[-n:]) / n
            travel = math.hypot(cx1 - cx0, cy1 - cy0)

            if travel < min_travel_px:
                continue

            speed = travel / t_dur
            if speed < min_speed_px_s:
                continue

            # Linearity: max deviation from straight line < 40% of travel
            if travel > 10 and len(track) > 3:
                vx, vy = cx1 - cx0, cy1 - cy0
                vlen = math.hypot(vx, vy)
                nx, ny = -vy / vlen, vx / vlen
                max_dev = max(
                    abs((d.x - cx0) * nx + (d.y - cy0) * ny) for d in track
                )
                if max_dev > travel * 0.4:
                    continue

            # This track is a real transit — keep all its detections
            kept.extend(track)

    return kept


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
        "composite_image":  result.composite_image,
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
