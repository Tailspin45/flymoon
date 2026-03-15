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
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from src import logger
from src.constants import get_ffmpeg_path

FFMPEG = get_ffmpeg_path() or "ffmpeg"

# ── Default tunable parameters (can be overridden per-call) ───────────────────
REFERENCE_WINDOW = 90  # frames in rolling reference (≈3 s at 30 fps)
MIN_BLOB_PIXELS = 20  # ignore tiny noise; a real transit blob is ≥20 px²
DIFF_THRESHOLD = (
    15  # pixel intensity difference — tuned for post-stabilization noise floor
)
DISK_MARGIN_PCT = 0.12  # fraction of radius to trim from limb (atmosphere margin)


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class BlobDetection:
    frame_index: int
    time_seconds: float
    x: int  # bounding-box centre x
    y: int  # bounding-box centre y
    width: int
    height: int
    area_px: int
    aspect_ratio: float  # width/height — >2 suggests elongated (aircraft)
    disk_x_norm: float  # x relative to disk centre, normalised by radius (-1..1)
    disk_y_norm: float
    confidence: str  # "high" | "medium" | "low"
    is_static: bool = False  # True = likely sunspot/static feature (filtered out)


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
    transit_positions: int = 0  # unique frames with transit detections (for UI slider)
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
        if cv2.contourArea(largest) > (min_r**2 * np.pi):
            (cx, cy), radius = cv2.minEnclosingCircle(largest)
            return int(cx), int(cy), int(radius)
    return None


def _disk_mask(
    shape: Tuple[int, int],
    cx: int,
    cy: int,
    radius: int,
    margin_pct: float = DISK_MARGIN_PCT,
) -> np.ndarray:
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
            [
                FFMPEG,
                "-y",
                "-i",
                str(src),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-movflags",
                "+faststart",
                str(dst),
            ],
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
        frame_gray,
        M,
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    return stabilized, (dx, dy)


def _confidence(blob_area: int, disk_radius: int) -> str:
    frac = blob_area / max(1, np.pi * disk_radius**2)
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
    target: str = "auto",
    max_positions: int = None,
    min_travel_px: float = None,
    min_speed_px_s: float = None,
    static_threshold_pct: float = None,
    apply_static_filter: bool = None,
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
        Override DIFF_THRESHOLD (pixel intensity difference, default 15).
    min_blob_pixels : int | None
        Override MIN_BLOB_PIXELS (minimum blob area, default 20).
    disk_margin_pct : float | None
        Override DISK_MARGIN_PCT (fraction of radius to trim, default 0.12).
    max_positions : int | None
        Maximum number of silhouette overlay positions in the composite.
        None = show all detected positions.
    min_travel_px : float | None
        Override minimum coherent track travel distance in pixels.
    min_speed_px_s : float | None
        Override minimum coherent track speed in pixels/second.
    static_threshold_pct : float | None
        Override static-feature threshold used by static blob filter.
    apply_static_filter : bool | None
        Force-enable/disable static blob filtering regardless of target mode.

    Returns
    -------
    AnalysisResult
        Detection metadata. Also written as ``<video>_analysis.json``.
    """
    from datetime import datetime, timezone

    # Resolve per-call overrides
    _diff_threshold = diff_threshold if diff_threshold is not None else DIFF_THRESHOLD
    _min_blob_pixels = (
        min_blob_pixels if min_blob_pixels is not None else MIN_BLOB_PIXELS
    )
    _disk_margin_pct = (
        disk_margin_pct if disk_margin_pct is not None else DISK_MARGIN_PCT
    )

    # Moon-specific overrides
    is_moon = str(target).lower() == "moon"
    if is_moon:
        if diff_threshold is None:
            _diff_threshold = max(8, int(_diff_threshold * 0.75))
    _min_travel_px = (
        float(min_travel_px)
        if min_travel_px is not None
        else (20.0 if is_moon else 25.0)
    )
    _min_speed_px_s = (
        float(min_speed_px_s)
        if min_speed_px_s is not None
        else (40.0 if is_moon else 50.0)
    )
    _static_threshold_pct = (
        float(static_threshold_pct)
        if static_threshold_pct is not None
        else (0.80 if is_moon else 0.25)
    )
    _apply_static_filter = (
        bool(apply_static_filter) if apply_static_filter is not None else (not is_moon)
    )

    path = Path(video_path)
    if not path.exists():
        return AnalysisResult(
            source_file=str(path),
            duration_seconds=0,
            fps=0,
            frame_count=0,
            disk_detected=False,
            disk_cx=None,
            disk_cy=None,
            disk_radius=None,
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
            disk_cx=None,
            disk_cy=None,
            disk_radius=None,
            error=f"File appears already analyzed: {path.name}",
            analyzed_at=datetime.now(timezone.utc).isoformat(),
        )

    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = total_frames / fps

    # For short clips, use fewer reference frames (min 10, or half the clip).
    # Moon FTF mode only needs a small reference for stabilisation — keep it
    # short so the transit isn't consumed by the reference window.
    if is_moon:
        ref_window = min(20, max(10, total_frames // 4))
    else:
        ref_window = min(REFERENCE_WINDOW, max(10, total_frames // 2))

    logger.info(
        f"[Analyzer] {path.name}: {total_frames} frames @ {fps:.1f} fps, {w}x{h}, ref_window={ref_window}"
    )
    logger.info(
        f"[Analyzer] Params: diff_threshold={_diff_threshold}, min_blob_pixels={_min_blob_pixels}, disk_margin={_disk_margin_pct:.0%}"
    )

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
    reference = None  # frozen once buffer is full (grayscale median)
    ref_gray_f32 = None  # float32 version for phase-correlation stabilization
    ref_bgr_frame = None  # color frame from reference window (composite background)
    mask = _disk_mask((h, w), disk_cx, disk_cy, disk_radius, _disk_margin_pct)

    # ── Output writer — write to temp file, re-encode to H.264 via FFmpeg ────
    base_stem = path.stem.replace("_analyzed", "")  # always derive from original name
    temp_path = path.with_name(base_stem + "_analyzed_tmp.mp4")
    path.with_name(base_stem + "_analyzed.mp4")
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.unlink(missing_ok=True)  # remove any stale tmp from previous failed run
    if output_annotated:
        fourcc, _ = _best_fourcc()
        cv2.VideoWriter(str(temp_path), fourcc, fps, (w, h))

    # ── Detection helpers ───────────────────────────────────────────────────
    ref_blur_cached = [None]  # mutable container so inner fn can cache
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
    # For moon FTF mode, raise min blob size to ignore texture jitter
    _ftf_min_blob = max(_min_blob_pixels, 30) if is_moon else _min_blob_pixels
    # FTF uses a gentler threshold since frame-to-frame changes are smaller
    _ftf_threshold = 15

    def _extract_blobs(
        binary: np.ndarray, fidx: int, min_px: int
    ) -> List[BlobDetection]:
        """Extract BlobDetections from a thresholded binary image."""
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
            binary, connectivity=8
        )
        blobs: List[BlobDetection] = []
        for lbl in range(1, num_labels):
            area = int(stats[lbl, cv2.CC_STAT_AREA])
            if area < min_px:
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
            blobs.append(
                BlobDetection(
                    frame_index=fidx,
                    time_seconds=round(fidx / fps, 3),
                    x=bcx,
                    y=bcy,
                    width=bw,
                    height=bh,
                    area_px=area,
                    aspect_ratio=round(ar, 2),
                    disk_x_norm=round(dx_norm, 3),
                    disk_y_norm=round(dy_norm, 3),
                    confidence=conf,
                )
            )
        return blobs

    def _detect_blobs_in_frame(gray: np.ndarray, fidx: int) -> List[BlobDetection]:
        """Detect blobs via reference-frame diff (solar mode)."""
        gray_s, _ = _stabilize_frame(gray, ref_gray_f32)
        gray_blur = cv2.GaussianBlur(gray_s, (5, 5), 0)
        if ref_blur_cached[0] is None:
            ref_blur_cached[0] = cv2.GaussianBlur(reference, (5, 5), 0)
        diff = cv2.absdiff(gray_blur, ref_blur_cached[0])
        diff_masked = cv2.bitwise_and(diff, diff, mask=mask)
        _, binary = cv2.threshold(diff_masked, _diff_threshold, 255, cv2.THRESH_BINARY)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
        return _extract_blobs(binary, fidx, _min_blob_pixels)

    def _detect_blobs_ftf(
        cur_blur: np.ndarray, prev_blur: np.ndarray, fidx: int
    ) -> List[BlobDetection]:
        """Detect blobs via frame-to-frame diff (lunar mode).

        Compares consecutive frames instead of a reference frame, which
        eliminates static lunar features (craters, maria) and isolates
        objects that actually moved between frames.
        """
        diff = cv2.absdiff(cur_blur, prev_blur)
        diff_masked = cv2.bitwise_and(diff, diff, mask=mask)
        _, binary = cv2.threshold(diff_masked, _ftf_threshold, 255, cv2.THRESH_BINARY)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
        return _extract_blobs(binary, fidx, _ftf_min_blob)

    # ── Main detection loop ───────────────────────────────────────────────────
    detections: List[BlobDetection] = []
    frame_idx = 0
    prev_blur_ftf = None  # previous stabilized+blurred frame for FTF mode

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
                if not is_moon:
                    # Scan reference-window frames (solar only — moon uses FTF)
                    for ri, ref_gray_frame in enumerate(ref_buffer):
                        detections.extend(_detect_blobs_in_frame(ref_gray_frame, ri))
                else:
                    # Prime the FTF previous-frame buffer
                    last_ref = ref_buffer[-1]
                    last_s, _ = _stabilize_frame(last_ref, ref_gray_f32)
                    prev_blur_ftf = cv2.GaussianBlur(last_s, (5, 5), 0)
            frame_idx += 1
            continue

        gray_s, _ = _stabilize_frame(gray, ref_gray_f32)

        if is_moon:
            # Frame-to-frame diff: eliminates static craters, isolates moving objects
            cur_blur = cv2.GaussianBlur(gray_s, (5, 5), 0)
            if prev_blur_ftf is not None:
                detections.extend(_detect_blobs_ftf(cur_blur, prev_blur_ftf, frame_idx))
            prev_blur_ftf = cur_blur
        else:
            detections.extend(_detect_blobs_in_frame(gray, frame_idx))

        frame_idx += 1

        if progress_cb and frame_idx % 30 == 0:
            progress_cb(frame_idx / max(1, total_frames) * 0.7)

    cap.release()

    # ── Filter out static blobs (sunspots / craters) ───────────────────────
    # In reference-diff mode (solar), sunspots appear at the same position
    # across many frames.  In FTF mode (lunar), static features are already
    # eliminated by the frame-to-frame approach, so skip the static filter.
    if _apply_static_filter:
        detections = _filter_static_blobs(
            detections, proximity_px=30, static_threshold_pct=_static_threshold_pct
        )
    moving_detections = [d for d in detections if not d.is_static]
    sum(1 for d in detections if d.is_static)

    # ── Filter moving detections for transit coherence ──────────────────────
    # A real transit traces a roughly linear path in under ~2 seconds.
    # Anything that hangs around the same spot for many seconds is shimmer.
    if is_moon:
        # FTF blobs represent change-points, not the object centroid, so the
        # standard nearest-neighbour tracker produces noisy tracks.  Use a
        # simpler dominant-blob strategy that is robust to the dual-signal
        # nature of frame-to-frame differencing.
        moving_detections = _filter_transit_coherence_ftf(
            moving_detections,
            fps,
            min_travel_px=_min_travel_px,
            min_speed_px_s=_min_speed_px_s,
        )
    else:
        moving_detections = _filter_transit_coherence(
            moving_detections,
            fps,
            min_travel_px=_min_travel_px,
            min_speed_px_s=_min_speed_px_s,
        )
    if moving_detections:
        logger.info(
            f"[Analyzer] After coherence filter: {len(moving_detections)} moving detections"
        )
    else:
        logger.info("[Analyzer] No coherent transit paths found")

    # Update the master detections list: mark non-coherent moving blobs as static
    # so the composite only draws confirmed transit silhouettes.
    coherent_ids = {id(d) for d in moving_detections}
    for d in detections:
        if not d.is_static and id(d) not in coherent_ids:
            d.is_static = True

    # ── Group detections into transit events ───────────────────────────────────
    # Use actual frames read rather than metadata-reported total to avoid
    # timestamps exceeding the real video duration (OpenCV can over-report frames).
    actual_duration = frame_idx / fps if fps > 0 else duration
    for d in detections:
        if d.time_seconds > actual_duration:
            d.time_seconds = actual_duration
    transit_events = _group_detections(moving_detections, fps)

    # ── Composite still image (replaces annotated video) ─────────────────────
    # One background frame with transit positions overlaid.
    composite_path = path.with_name("analyzed_" + base_stem + ".jpg")
    composite_path.parent.mkdir(parents=True, exist_ok=True)
    composite_image_str = None

    # Choose a clean background frame that doesn't contain the transit.
    # The ref_bgr_frame (middle of reference buffer) can contain the aircraft
    # if the transit occurs early.  Pick a frame from the existing reference
    # buffer that is furthest from any transit detection — no extra video
    # reads needed, and drift is zero since it's from the same time window.
    bg_for_composite = ref_bgr_frame
    if moving_detections and ref_bgr_frame is not None:
        transit_frame_set = {d.frame_index for d in moving_detections}
        buf_len = len(ref_buffer_bgr)
        if buf_len > 0:
            # Score each buffer frame by minimum distance to any transit frame
            best_idx = 0
            best_dist = -1
            buf_list = list(ref_buffer_bgr)
            for bi in range(buf_len):
                min_d = min(abs(bi - tf) for tf in transit_frame_set)
                if min_d > best_dist:
                    best_dist = min_d
                    best_idx = bi
            if best_dist > 0:
                bg_for_composite = buf_list[best_idx].copy()

    if output_annotated:
        composite_image_str = _write_composite_image(
            path,
            composite_path,
            detections,
            disk_cx,
            disk_cy,
            disk_radius,
            progress_cb,
            bg_frame=bg_for_composite,
            reference_gray=reference,
            ref_gray_f32=ref_gray_f32,
            is_moon=is_moon,
            max_positions=max_positions,
        )
        if composite_image_str:
            logger.info(f"[Analyzer] Composite image → {composite_path.name}")
        else:
            logger.warning("[Analyzer] Composite image write failed")

    # Count unique frames with transit detections (for UI slider range)
    _transit_frame_set = set(d.frame_index for d in moving_detections)
    _transit_positions = len(_transit_frame_set)

    result = AnalysisResult(
        source_file=str(path),
        duration_seconds=round(actual_duration, 2),
        fps=round(fps, 2),
        frame_count=frame_idx,
        disk_detected=disk_detected,
        disk_cx=disk_cx,
        disk_cy=disk_cy,
        disk_radius=disk_radius,
        detections=detections,
        transit_events=transit_events,
        transit_positions=_transit_positions,
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
TRANSIT_TINT = np.array(
    [40, 40, 200], dtype=np.uint8
)  # reddish tint for transit silhouettes


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
    is_moon: bool = False,
    max_positions: Optional[int] = None,
) -> Optional[str]:
    """
    Build a composite still image showing transit silhouettes and sunspots/craters.

    For each transit frame, the actual darkened pixels (object silhouette)
    are extracted and alpha-blended onto the background.  Sunspots (sun) or
    craters (moon) are detected from the reference frame; when is_moon=True
    those static features are NOT drawn on the composite.

    Parameters
    ----------
    max_positions : int | None
        Maximum number of silhouette positions to overlay.  When set,
        frames are evenly sampled across the detection span.  None = all.

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
            logger.error(
                "[Analyzer] Could not read background frame for composite image"
            )
            return None

    h, w = canvas.shape[:2]
    if progress_cb:
        progress_cb(0.75)

    # Separate static (sunspot) and transit detections
    [d for d in detections if d.is_static]
    transit_dets = [d for d in detections if not d.is_static]
    # Solar annotation circles are collected during the render loop and drawn
    # last (after disk masking) so they are never occluded.
    solar_circles: list = []  # list of (cx, cy, r)

    # ── Alpha-blend transit silhouettes from source frames ────────────────
    if transit_dets and reference_gray is not None:
        # Build a set of unique frame indices that contain transit detections
        from collections import defaultdict

        frames_needed: dict = defaultdict(list)
        for d in transit_dets:
            frames_needed[d.frame_index].append(d)

        # For moon: FTF produces many blobs per frame (leading/trailing edges).
        # Keep only the largest detection per frame to avoid circle clutter.
        if is_moon:
            for fi in frames_needed:
                dets = frames_needed[fi]
                if len(dets) > 1:
                    dets.sort(key=lambda d: d.width * d.height, reverse=True)
                    frames_needed[fi] = [dets[0]]

        # ── Pre-qualify frames: trim edge positions and check silhouette ──
        # Moon: edge frames often show partial aircraft entering/leaving the
        # field — trim ~15% from each end.  Sun: the aircraft can touch the
        # limb, so keep ALL detection frames (no edge trimming).
        sorted_frame_keys = sorted(frames_needed.keys())
        if is_moon and len(sorted_frame_keys) > 4:
            trim = max(1, len(sorted_frame_keys) // 7)  # ~15%
            sorted_frame_keys = sorted_frame_keys[trim:-trim]

        # Quick silhouette-quality scan: read video once to score each frame
        good_frame_keys = []
        if reference_gray is not None and sorted_frame_keys:
            _cap_pre = cv2.VideoCapture(str(src))
            _fi = 0
            _key_set = set(sorted_frame_keys)
            while True:
                _ok, _frm = _cap_pre.read()
                if not _ok:
                    break
                if _fi in _key_set:
                    _gr = cv2.cvtColor(_frm, cv2.COLOR_BGR2GRAY)
                    if ref_gray_f32 is not None:
                        _gr, _ = _stabilize_frame(_gr, ref_gray_f32)
                    det = frames_needed[_fi][0]
                    pad = max(det.width, det.height, 8)
                    _h, _w = _gr.shape
                    x1 = max(0, det.x - det.width // 2 - pad)
                    y1 = max(0, det.y - det.height // 2 - pad)
                    x2 = min(_w, det.x + det.width // 2 + pad)
                    y2 = min(_h, det.y + det.height // 2 + pad)
                    ref_p = reference_gray[y1:y2, x1:x2].astype(np.int16)
                    cur_p = _gr[y1:y2, x1:x2].astype(np.int16)
                    if is_moon:
                        diff_p = np.abs(ref_p - cur_p).astype(np.uint8)
                    else:
                        diff_p = np.clip(ref_p - cur_p, 0, 255).astype(np.uint8)
                    _, _sil = cv2.threshold(
                        diff_p, 12 if is_moon else 10, 255, cv2.THRESH_BINARY
                    )
                    if _sil.sum() > 0:
                        good_frame_keys.append(_fi)
                _fi += 1
            _cap_pre.release()
        else:
            good_frame_keys = sorted_frame_keys

        # Subsample from good frames to exactly max_positions if requested.
        # Special case: max_positions=1 picks the middle frame.
        if max_positions and 0 < max_positions < len(good_frame_keys):
            if max_positions == 1:
                mid = good_frame_keys[len(good_frame_keys) // 2]
                good_frame_keys = [mid]
            else:
                step = (len(good_frame_keys) - 1) / (max_positions - 1)
                good_frame_keys = list(
                    dict.fromkeys(
                        good_frame_keys[round(i * step)] for i in range(max_positions)
                    )
                )
        frames_needed = {
            k: frames_needed[k] for k in good_frame_keys if k in frames_needed
        }
        logger.info(
            f"[Analyzer] Composite: {len(frames_needed)} frame(s) selected"
            f" (max_positions={max_positions}, good={len(good_frame_keys)})"
            f" keys={sorted(frames_needed.keys())}"
        )

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
                    # Moon: tight pad prevents stabilisation jitter bleeding
                    # across the crater-textured surface.
                    # Sun: use the full blob half-size as padding so the entire
                    # aircraft silhouette is always captured (aircraft may be
                    # large and detection centre slightly off-centre).
                    if is_moon:
                        pad = min(max(det.width, det.height) // 4 + 4, 20)
                    else:
                        pad = max(det.width, det.height, 8)
                    x1 = max(0, det.x - det.width // 2 - pad)
                    y1 = max(0, det.y - det.height // 2 - pad)
                    x2 = min(w, det.x + det.width // 2 + pad)
                    y2 = min(h, det.y + det.height // 2 + pad)

                    ref_patch = reference_gray[y1:y2, x1:x2].astype(np.int16)
                    cur_patch = gray[y1:y2, x1:x2].astype(np.int16)

                    if is_moon:
                        # Moon: aircraft can be lighter OR darker than
                        # background, so use absolute difference.
                        abs_diff = np.abs(ref_patch - cur_patch).astype(np.uint8)
                        _, sil_mask = cv2.threshold(
                            abs_diff, 18, 255, cv2.THRESH_BINARY
                        )
                        # Erode to remove single-pixel jitter / fringe
                        sil_mask = cv2.erode(sil_mask, np.ones((2, 2), np.uint8))
                        # Moon: small blobs only get a circle; large aircraft
                        # silhouettes are self-evident.
                        blob_size = max(det.width, det.height)
                        if blob_size < 20:
                            r = max(6, blob_size // 2 + 4)
                            cv2.circle(canvas, (det.x, det.y), r, (0, 0, 220), 1)
                    else:
                        # Sun: aircraft is always darker (silhouette)
                        darkening = np.clip(ref_patch - cur_patch, 0, 255).astype(
                            np.uint8
                        )
                        _, sil_mask = cv2.threshold(
                            darkening, 10, 255, cv2.THRESH_BINARY
                        )

                    if sil_mask.sum() == 0:
                        continue

                    # Copy actual source frame pixels where the object is,
                    # so the transit appears as its real silhouette (not
                    # artificially darkened)
                    src_roi = frame[y1:y2, x1:x2]
                    dst_roi = canvas[y1:y2, x1:x2]
                    alpha = (sil_mask.astype(np.float32) / 255.0)[:, :, np.newaxis]
                    canvas[y1:y2, x1:x2] = (
                        dst_roi * (1 - alpha) + src_roi * alpha
                    ).astype(np.uint8)
                    blended_count += 1
                    # Solar: queue circle for later so disk masking can't
                    # remove it and the yellow limb ring can't cover it.
                    if not is_moon:
                        r = max(6, max(det.width, det.height) // 2 + 4)
                        solar_circles.append((det.x, det.y, r))

            frame_idx += 1
            if progress_cb and frame_idx % 60 == 0:
                progress_cb(
                    0.75
                    + 0.15
                    * min(1.0, frame_idx / max(1, cap.get(cv2.CAP_PROP_FRAME_COUNT)))
                )

        cap.release()
        if blended_count:
            logger.info(
                f"[Analyzer] Composited {blended_count} transit silhouettes from {len(frames_needed)} frames"
            )

    elif transit_dets:
        # No reference available — fall back to red outline markers
        for d in transit_dets:
            r = max(6, max(d.width, d.height) // 2 + 4)
            thickness = max(2, r // 6)
            cv2.circle(canvas, (d.x, d.y), r, (0, 0, 220), thickness)

    # ── Sunspots (sun only): detect dark features from CLAHE-enhanced reference ─
    # For moon targets, craters are pervasive and we deliberately skip this step.
    if (
        not is_moon
        and reference_gray is not None
        and disk_cx is not None
        and disk_radius is not None
    ):
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(reference_gray)
        blur_e = cv2.GaussianBlur(enhanced, (3, 3), 0)
        # Use generous margin for sunspot detection (only exclude outermost 2%)
        spot_inner_r = int(disk_radius * 0.98)
        spot_mask = np.zeros(reference_gray.shape[:2], dtype=np.uint8)
        cv2.circle(spot_mask, (disk_cx, disk_cy), spot_inner_r, 255, -1)
        adapt = cv2.adaptiveThreshold(
            blur_e,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            blockSize=51,
            C=6,
        )
        adapt = cv2.bitwise_and(adapt, spot_mask)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        adapt = cv2.morphologyEx(adapt, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(
            adapt, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        # Also confirm each candidate against the raw reference: must be
        # darker than local mean (avoids CLAHE artifacts near limb)
        blur_ref = cv2.GaussianBlur(reference_gray, (5, 5), 0)
        # Build on-disk mask for verification (avoid off-disk dark pixels
        # dragging the local mean down for limb spots)
        verify_mask = np.zeros(reference_gray.shape[:2], dtype=np.uint8)
        cv2.circle(verify_mask, (disk_cx, disk_cy), disk_radius, 255, -1)
        spot_count = 0
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 5:
                continue
            M = cv2.moments(cnt)
            if M["m00"] == 0:
                continue
            sx = int(M["m10"] / M["m00"])
            sy = int(M["m01"] / M["m00"])
            # Verify: spot must be darker than on-disk neighbors
            local_val = int(blur_ref[sy, sx])
            patch_r = 25
            py1, py2 = max(0, sy - patch_r), min(h, sy + patch_r)
            px1, px2 = max(0, sx - patch_r), min(w, sx + patch_r)
            patch = blur_ref[py1:py2, px1:px2]
            mask_patch = verify_mask[py1:py2, px1:px2]
            on_disk = patch[mask_patch > 0]
            if len(on_disk) == 0:
                continue
            local_mean = float(on_disk.mean())
            if local_val >= local_mean - 3:
                continue  # not actually darker than surroundings
            _, _, sw, sh = cv2.boundingRect(cnt)
            sr = max(10, max(sw, sh) // 2 + 6)
            cv2.circle(canvas, (sx, sy), sr, STATIC_COLOR, 2)
            spot_count += 1
        if spot_count:
            logger.info(f"[Analyzer] {spot_count} sunspot(s) detected")

    # ── Mask outside disk to clean black (sun only) ─────────────────────
    # For moon, the aircraft is visible against the dark sky near the limb,
    # so we preserve the area around the disk.
    if (
        not is_moon
        and disk_cx is not None
        and disk_cy is not None
        and disk_radius is not None
    ):
        outside_mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
        cv2.circle(outside_mask, (disk_cx, disk_cy), disk_radius + 2, 255, -1)
        canvas[outside_mask == 0] = 0

    # ── Draw disk boundary (yellow) LAST so it's on top (sun only) ──────
    # For moon, the aircraft silhouette is the star; omitting the limb circle
    # keeps the composite cleaner.
    if (
        not is_moon
        and disk_cx is not None
        and disk_cy is not None
        and disk_radius is not None
    ):
        cv2.circle(canvas, (disk_cx, disk_cy), disk_radius, (0, 255, 255), 2)

    # ── Solar transit annotation circles (drawn last, on top of everything) ─
    # Drawn after disk masking and the limb ring so they are never occluded.
    for cx, cy, r in solar_circles:
        thickness = max(2, r // 6)
        cv2.circle(canvas, (cx, cy), r, (0, 0, 220), thickness)

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
    src: Path,
    dst: Path,
    fps: float,
    w: int,
    h: int,
    total_frames: int,
    detections: List[BlobDetection],
    disk_cx: int,
    disk_cy: int,
    disk_radius: int,
    progress_cb=None,
    progress_offset: float = 0.0,
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
                cv2.ellipse(
                    annotated, (d.x, d.y), (half_w, half_h), 0, 0, 360, color, thickness
                )
                cv2.putText(
                    annotated,
                    label,
                    (d.x - d.width // 2, max(12, d.y - d.height // 2 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    color,
                    2,
                    cv2.LINE_AA,
                )

            # Disk outline
            cv2.circle(annotated, (disk_cx, disk_cy), disk_radius, (0, 255, 255), 2)

        # Timestamp
        ts = f"{frame_idx / fps:.2f}s"
        cv2.putText(
            annotated,
            ts,
            (8, h - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (180, 180, 180),
            1,
            cv2.LINE_AA,
        )

        out.write(annotated)
        frame_idx += 1

        if progress_cb and frame_idx % 30 == 0:
            frac = progress_offset + (1.0 - progress_offset) * (
                frame_idx / max(1, total_frames)
            )
            progress_cb(frac)

    cap.release()
    out.release()


def _filter_static_blobs(
    detections: List[BlobDetection],
    proximity_px: int = 8,
    static_threshold_pct: float = 0.25,
) -> List[BlobDetection]:
    """Mark blobs that stay at the same position across many frames as static.

    Algorithm: group all detections by spatial proximity.  If a spatial cluster
    spans more than `static_threshold_pct` of the frames that contain *any*
    detection, it's a static feature (sunspot, crater, hot-pixel), not a transit.
    Use a higher threshold (e.g. 0.80) for Moon targets where craters are stable
    features that must not bleed into the moving-blob set.
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
            if (
                abs(detections[j].x - d.x) <= proximity_px
                and abs(detections[j].y - d.y) <= proximity_px
            ):
                cluster.append(j)
                assigned.add(j)
        clusters.append(cluster)

    threshold = n_det_frames * static_threshold_pct
    for cluster in clusters:
        unique_frames = set(detections[i].frame_index for i in cluster)
        if len(unique_frames) > threshold:
            for i in cluster:
                detections[i].is_static = True

    return detections


def _filter_transit_coherence_ftf(
    detections: List[BlobDetection],
    fps: float,
    max_duration_sec: float = 4.0,
    min_travel_px: float = 20.0,
    min_speed_px_s: float = 40.0,
) -> List[BlobDetection]:
    """Coherence filter tuned for frame-to-frame (lunar) blob detection.

    FTF blobs represent *change-points* between consecutive frames — they
    appear at both the leading and trailing edges of a moving object.  This
    means a single aircraft produces two blobs per frame, and positions
    jitter between the old and new locations.

    Strategy: take the largest blob per frame as the dominant signal (the
    combined leading+trailing edge blob is always the biggest), then check
    whether these dominant positions trace a coherent path across the disk.
    If they do, keep ALL detections in those frames so the composite can
    render them.
    """
    if not detections:
        return []

    import math
    from collections import defaultdict

    by_frame: dict = defaultdict(list)
    for d in detections:
        by_frame[d.frame_index].append(d)

    if len(by_frame) < 3:
        return []

    # Build dominant-blob track (largest blob per frame)
    frame_ids = sorted(by_frame.keys())
    dominant = []
    for fi in frame_ids:
        best = max(by_frame[fi], key=lambda d: d.area_px)
        dominant.append(best)

    # Split into temporal runs (gap ≤ 2 frames to bridge occasional dropouts)
    runs: list = [[dominant[0]]]
    for d in dominant[1:]:
        prev = runs[-1][-1]
        gap_frames = d.frame_index - prev.frame_index
        if gap_frames <= 3:  # allow up to 2 dropped frames
            runs[-1].append(d)
        else:
            runs.append([d])

    kept_frames: set = set()

    for run in runs:
        if len(run) < 3:
            continue
        t_dur = run[-1].time_seconds - run[0].time_seconds
        if t_dur <= 0 or t_dur > max_duration_sec:
            continue

        # Travel (3-frame averaged endpoints)
        n = min(3, len(run))
        cx0 = sum(d.x for d in run[:n]) / n
        cy0 = sum(d.y for d in run[:n]) / n
        cx1 = sum(d.x for d in run[-n:]) / n
        cy1 = sum(d.y for d in run[-n:]) / n
        travel = math.hypot(cx1 - cx0, cy1 - cy0)

        if travel < min_travel_px:
            continue
        speed = travel / t_dur
        if speed < min_speed_px_s:
            continue

        # Linearity: relaxed to 35% for FTF (dual-edge jitter)
        if travel > 10 and len(run) > 3:
            vx, vy = cx1 - cx0, cy1 - cy0
            vlen = math.hypot(vx, vy)
            if vlen > 0:
                nx, ny = -vy / vlen, vx / vlen
                max_dev = max(abs((d.x - cx0) * nx + (d.y - cy0) * ny) for d in run)
                if max_dev > travel * 0.35:
                    continue

        # This run is a transit — mark all its frames
        for d in run:
            kept_frames.add(d.frame_index)

    # Return ALL detections from qualifying frames
    return [d for d in detections if d.frame_index in kept_frames]


def _filter_transit_coherence(
    detections: List[BlobDetection],
    fps: float,
    max_duration_sec: float = 3.0,
    min_travel_px: float = 40.0,
    min_speed_px_s: float = 80.0,
    max_link_px: float = 150.0,
    max_frame_gap: int = 3,
) -> List[BlobDetection]:
    """Keep only detections that form coherent transit-like paths.

    A real transit is ONE object crossing the disk in 0.1–3 s at high speed.
    This filter builds individual object tracks by linking the nearest blob
    in consecutive frames, then evaluates each track for transit-like motion.

    Algorithm:
      1. Group moving detections into temporal runs (≤0.5 s gap).
      2. Within each run, build object tracks by greedy nearest-neighbor
         linking across frames (max ``max_link_px`` per frame step,
         scaled by frame gap).  Tracks are closed when no blob appears
         within ``max_frame_gap`` frames — this prevents a finished
         transit from absorbing unrelated blobs later.
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
        # NOTE: do NOT check run duration here — a long shimmer stream can
        # contain a short real transit buried inside it.  The per-track
        # duration check (below) enforces max_duration_sec correctly.

        # ── 2. Build per-frame blob lists ───────────────────────────────
        by_frame: dict = defaultdict(list)
        for d in run:
            by_frame[d.frame_index].append(d)
        frame_ids = sorted(by_frame.keys())

        if len(frame_ids) < 2:
            # Single-frame blobs cannot be transits — real aircraft cross
            # the disk over multiple frames at 15fps.
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
            # For each existing track, try to extend with nearest candidate.
            # Skip tracks whose last detection is too far back — the object
            # has left and extending would merge unrelated objects.
            claimed: set = set()
            for track in tracks:
                tail = track[-1]
                frame_gap = fi - tail.frame_index
                if frame_gap > max_frame_gap:
                    continue  # track is stale, don't extend
                # Allow larger link distance when frames are skipped
                link_limit = max_link_px * frame_gap
                best_d = None
                best_dist = link_limit
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
        _rejected = {
            "short": 0,
            "duration": 0,
            "travel": 0,
            "speed": 0,
            "linearity": 0,
            "aspect": 0,
        }
        for track in tracks:
            if len(track) < 3:
                _rejected["short"] += 1
                continue  # need ≥3 points to confirm a path

            t_dur = track[-1].time_seconds - track[0].time_seconds
            if t_dur > max_duration_sec or t_dur <= 0:
                _rejected["duration"] += 1
                continue

            # Travel (3-frame averaged endpoints)
            n = min(3, len(track))
            cx0 = sum(d.x for d in track[:n]) / n
            cy0 = sum(d.y for d in track[:n]) / n
            cx1 = sum(d.x for d in track[-n:]) / n
            cy1 = sum(d.y for d in track[-n:]) / n
            travel = math.hypot(cx1 - cx0, cy1 - cy0)

            if travel < min_travel_px:
                _rejected["travel"] += 1
                continue

            speed = travel / t_dur
            if speed < min_speed_px_s:
                _rejected["speed"] += 1
                continue

            # Linearity: max deviation from straight line < 25% of travel
            if travel > 10 and len(track) > 3:
                vx, vy = cx1 - cx0, cy1 - cy0
                vlen = math.hypot(vx, vy)
                nx, ny = -vy / vlen, vx / vlen
                max_dev = max(abs((d.x - cx0) * nx + (d.y - cy0) * ny) for d in track)
                if max_dev > travel * 0.25:
                    _rejected["linearity"] += 1
                    continue

            # Aspect-ratio guard: reject tracks where the blob itself is
            # highly elongated (scope-slew smear) — but do NOT reject a
            # track just because the *path* is wide, since a real transit
            # can cross mostly horizontally.  Only apply when avg blob
            # aspect ratio is extreme (> 5:1).
            avg_blob_aspect = sum(d.width / max(d.height, 1) for d in track) / len(
                track
            )
            if avg_blob_aspect > 5:
                _rejected["aspect"] += 1
                continue

            # This track is a real transit — keep all its detections
            kept.extend(track)

        if not kept:
            reasons = ", ".join(f"{k}={v}" for k, v in _rejected.items() if v > 0)
            logger.info(
                f"[Analyzer] Coherence: {len(tracks)} tracks rejected " f"({reasons})"
            )

    return kept


def _group_detections(
    detections: List[BlobDetection], fps: float, gap_seconds: float = 0.5
) -> List[dict]:
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
    t_end = blobs[-1].time_seconds
    best = max(blobs, key=lambda b: b.area_px)
    confs = [b.confidence for b in blobs]
    overall = "high" if "high" in confs else ("medium" if "medium" in confs else "low")
    # Estimate direction from first→last blob centroid
    dx = blobs[-1].disk_x_norm - blobs[0].disk_x_norm
    dy = blobs[-1].disk_y_norm - blobs[0].disk_y_norm
    import math

    heading_deg = round(math.degrees(math.atan2(dy, dx)), 1) if len(blobs) > 1 else None
    speed_norm = (
        round(math.hypot(dx, dy) / max(0.001, t_end - t_start), 3)
        if t_end > t_start
        else None
    )
    return {
        "start_seconds": t_start,
        "end_seconds": t_end,
        "duration_ms": round((t_end - t_start) * 1000),
        "peak_area_px": best.area_px,
        "peak_aspect_ratio": best.aspect_ratio,
        "confidence": overall,
        "heading_deg": heading_deg,
        "speed_norm_per_s": speed_norm,
        "frame_count": len(blobs),
    }


def _write_sidecar(result: AnalysisResult, path: Path):
    data = {
        "source_file": result.source_file,
        "analyzed_at": result.analyzed_at,
        "duration_seconds": result.duration_seconds,
        "fps": result.fps,
        "frame_count": result.frame_count,
        "disk_detected": result.disk_detected,
        "disk_cx": result.disk_cx,
        "disk_cy": result.disk_cy,
        "disk_radius": result.disk_radius,
        "transit_events": result.transit_events,
        "transit_positions": result.transit_positions,
        "detection_count": len(result.detections),
        "composite_image": result.composite_image,
        "error": result.error,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def composite_from_frames(
    video_path: str,
    frame_indices: List[int],
    fps: float = 30.0,
    disk_margin_pct: float = 0.12,
    target: str = "sun",
) -> dict:
    """Build a composite image from user-selected video frames.

    Instead of automatic blob detection, the user manually selects frames
    that contain transit objects.  For each selected frame, the dark
    silhouette (relative to a reference) is extracted and alpha-blended
    onto a clean background.

    Returns dict with keys: composite_image, error, frame_count.
    """
    path = Path(video_path)
    if not path.exists():
        return {"error": f"File not found: {video_path}", "composite_image": None}

    is_moon = str(target).lower() == "moon"
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if total == 0 or w == 0 or h == 0:
        cap.release()
        return {"error": "Could not read video", "composite_image": None}

    # Validate frame indices
    valid_frames = sorted(set(f for f in frame_indices if 0 <= f < total))
    if not valid_frames:
        cap.release()
        return {"error": "No valid frame indices", "composite_image": None}

    # Read all frames sequentially up to the last selected frame.
    # CAP_PROP_POS_FRAMES seeking is unreliable for mp4v-encoded files on
    # macOS (AVFoundation backend), so sequential reading is the only safe
    # approach that guarantees we get the right frame content.
    max_frame_needed = valid_frames[-1]
    ref_count = min(90, valid_frames[0] if valid_frames[0] > 10 else 90)
    ref_stack: List = []
    selected_frames: dict = {}
    frame_idx = 0

    while frame_idx <= max_frame_needed:
        ok, frm = cap.read()
        if not ok:
            break
        if frame_idx < ref_count:
            ref_stack.append(frm.astype(np.float32))
        if frame_idx in valid_frames:
            selected_frames[frame_idx] = frm
        frame_idx += 1

    cap.release()

    if not ref_stack:
        return {"error": "Could not read video frames", "composite_image": None}

    reference = np.median(np.stack(ref_stack), axis=0).astype(np.uint8)
    ref_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)

    # Detect disk
    disk = _detect_disk(reference)
    disk_cx = disk[0] if disk else w // 2
    disk_cy = disk[1] if disk else h // 2
    disk_r = disk[2] if disk else min(h, w) // 3

    # Build disk mask (exclude limb)
    inner_r = max(1, int(disk_r * (1.0 - disk_margin_pct)))
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, (disk_cx, disk_cy), inner_r, 255, -1)

    canvas = reference.copy()
    extracted = 0

    for fi in valid_frames:
        frame = selected_frames.get(fi)
        if frame is None:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Extract darkened pixels (transit silhouette)
        if is_moon:
            diff = np.abs(ref_gray.astype(np.int16) - gray.astype(np.int16)).astype(
                np.uint8
            )
        else:
            diff = np.clip(
                ref_gray.astype(np.int16) - gray.astype(np.int16), 0, 255
            ).astype(np.uint8)

        # Apply disk mask
        diff = cv2.bitwise_and(diff, diff, mask=mask)

        # Threshold to get silhouette
        _, sil = cv2.threshold(diff, 10, 255, cv2.THRESH_BINARY)
        # Cleanup
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        sil = cv2.morphologyEx(sil, cv2.MORPH_OPEN, kernel)

        if sil.sum() == 0:
            continue

        # Alpha-blend the source pixels where silhouette is detected
        alpha = (sil.astype(np.float32) / 255.0)[:, :, np.newaxis]
        canvas = (canvas * (1 - alpha) + frame * alpha).astype(np.uint8)
        extracted += 1

    # Draw disk outline and annotations
    cv2.circle(canvas, (disk_cx, disk_cy), disk_r, (0, 180, 255), 1)
    cv2.circle(canvas, (disk_cx, disk_cy), inner_r, (0, 255, 0), 1)

    # Timestamp annotation
    label = f"{extracted}/{len(valid_frames)} frames, manual composite"
    cv2.putText(
        canvas,
        label,
        (10, h - 15),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (200, 200, 200),
        1,
    )

    # Save
    stem = path.stem.replace("analyzed_", "")
    dst = path.with_name(f"composite_{stem}.jpg")
    cv2.imwrite(str(dst), canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])

    # Relative path for frontend
    rel = str(dst)
    if "static/" in rel:
        rel = rel[rel.index("static/") :]

    logger.info(
        f"[Analyzer] Manual composite: {extracted}/{len(valid_frames)} frames → {dst.name}"
    )

    return {
        "composite_image": rel,
        "frame_count": extracted,
        "total_selected": len(valid_frames),
        "error": None,
    }


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m src.transit_analyzer <video.mp4>")
        sys.exit(1)
    r = analyze_video(sys.argv[1])
    print(
        json.dumps(
            {
                "events": r.transit_events,
                "detections": len(r.detections),
                "disk": {"cx": r.disk_cx, "cy": r.disk_cy, "r": r.disk_radius},
            },
            indent=2,
        )
    )
