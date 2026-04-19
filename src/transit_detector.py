"""
Real-time transit detection from telescope RTSP stream.

Reads the live video feed via ffmpeg, processes frames at ~15 fps on a
160×90 canvas using a dual-signal algorithm (consecutive-frame diff +
disk-weighted reference diff, both with mean-subtraction for scintillation
immunity).

**Disk-aware detection**: Every 2 seconds, Hough circle detection locates
the Sun/Moon disk in the frame.  Signals are computed only within the
inner disk (excluding a configurable limb margin, default 25%), which
eliminates false positives from atmospheric shimmer at the disk edge.
Detection is DISABLED when no disk is found (ratio check cannot be
performed without a real inner/outer split).

**Consecutive-frame gate**: Both signals must exceed their adaptive
thresholds AND the centre ratio must pass for `consec_frames_required`
consecutive frames before a detection is fired.  The counter resets
immediately after firing and on every reconnect.

**Noise density guard**: A 60-second background window tracks the
long-run score baseline.  If the last 3 seconds are >2× that baseline
(scene dominated by random sunspot-like activity), thresholds are raised
proportionally, suppressing false positives during high-activity periods.

**Sensitivity scale**: Runtime multiplier on both thresholds.  <1 = more
sensitive, >1 = stricter.  Exposed as a sidebar slider and via
PATCH /telescope/detect/settings.

When a transit is detected it auto-triggers a full-resolution recording
and optionally enriches the event with FlightAware flight data.
"""

import collections
import json
import math
import os
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Callable, Deque, Dict, List, Optional

import cv2
import numpy as np

from src import logger
from src.flight_data import normalize_aircraft_display_id
from src.constants import get_ffmpeg_path

FFMPEG = get_ffmpeg_path() or "ffmpeg"

# ---------------------------------------------------------------------------
# D1: PyWavelets import — optional; falls back to raw Signal B if unavailable
# ---------------------------------------------------------------------------
try:
    import pywt as _pywt

    _PYWT_AVAILABLE = True
except ImportError:
    _pywt = None  # type: ignore[assignment]
    _PYWT_AVAILABLE = False


def _wavelet_detrend(buf: "collections.deque") -> float:
    """Return wavelet-detrended magnitude of the last sample in *buf*.

    Applies a level-3 sym4 DWT to the buffer, zeroes the approximation
    (slow atmospheric / cloud-edge trend), reconstructs the detail-only
    signal, and returns abs(last sample).  Falls back to raw abs(buf[-1])
    if pywt is unavailable or the buffer is too short.

    At 15 fps, level-3 separates:
      • Approx  (> ~2 s)  — cloud edges, background drift        [removed]
      • Detail  (0.13–2 s)— transit impulse, atmospheric shimmer [kept]
    """
    if not _PYWT_AVAILABLE or len(buf) < 16:
        return float(abs(buf[-1]))
    try:
        arr = np.array(buf, dtype=np.float32)
        # Cap level so pywt never hits boundary-effect territory
        max_lvl = _pywt.dwt_max_level(len(arr), "sym4")
        level = max(1, min(3, max_lvl))
        coeffs = _pywt.wavedec(arr, "sym4", mode="periodization", level=level)
        coeffs[0][:] = 0.0  # zero approximation (slow trend)
        detail = _pywt.waverec(coeffs, "sym4", mode="periodization")
        # abs() converts bipolar detail to positive magnitude
        return float(abs(detail[-1])) if len(detail) > 0 else float(abs(arr[-1]))
    except Exception:
        return float(abs(buf[-1]))


# D2: Matched-filter gate template durations (frames at 30fps)
# Covers 0.2 s (fast crossing) → 4.0 s (pattern-altitude aircraft with seeing gaps)
# 90 and 120 frame templates added for slow aircraft (70-100 frames) where atmospheric
# seeing causes the signal to appear in only ~45-50% of frames.
_MF_TEMPLATES: tuple = (6, 10, 15, 24, 40, 60, 90, 120)
# Graduated hit-rate thresholds: tighter for short templates (noise immunity),
# looser for long templates (tolerates seeing-induced gaps without missing real transits).
# n<=15: 70%, n<=40: 60%, n<=60: 50%, n>60: 45%
_MF_THRESHOLD_FRAC: float = 0.70  # used for n<=15 (default); see _mf_hit_required()


def _mf_hit_required(n: int) -> int:
    """Minimum number of 'triggered' frames to pass the matched-filter gate.

    Graduated thresholds tolerate atmospheric seeing gaps in long transits
    while keeping short templates noise-resistant:
      n<=15:  70%  — fast aircraft, few frames, must be mostly above-threshold
      n<=40:  60%  — medium transit, some seeing gaps expected
      n<=60:  50%  — slow transit with significant gaps
      n> 60:  45%  — pattern-altitude aircraft with heavy seeing (70-100 frame span)
    """
    if n <= 15:
        frac = 0.70
    elif n <= 40:
        frac = 0.60
    elif n <= 60:
        frac = 0.50
    else:
        frac = 0.45
    return max(3, int(frac * n))


# ---------------------------------------------------------------------------
# Detection parameters
# ---------------------------------------------------------------------------
ANALYSIS_WIDTH = 180
ANALYSIS_HEIGHT = 320
ANALYSIS_FPS = 30
FRAME_BYTES = ANALYSIS_WIDTH * ANALYSIS_HEIGHT * 3  # RGB24

# Rolling window for adaptive threshold (seconds of history)
HISTORY_SECONDS = 20
HISTORY_SIZE = ANALYSIS_FPS * HISTORY_SECONDS  # ~600 frames

# Long-run background window for noise density guard (60 seconds)
BG_HISTORY_SECONDS = 60
BG_HISTORY_SIZE = ANALYSIS_FPS * BG_HISTORY_SECONDS  # ~1800 frames

# Cooldown between detections (seconds) — configurable via DETECTION_COOLDOWN env var
DETECTION_COOLDOWN = int(os.getenv("DETECTION_COOLDOWN", "6"))

# Recording duration when transit detected (seconds)
DETECTION_RECORD_DURATION = 10

# Pre-buffer: seconds of video to keep BEFORE detection trigger
PRE_BUFFER_SECONDS = int(os.getenv("DETECTION_PRE_BUFFER", "3"))
# Post-buffer: seconds of video to keep AFTER detection trigger
POST_BUFFER_SECONDS = int(os.getenv("DETECTION_POST_BUFFER", "6"))

# --- Phase 1 algorithm parameters ---
# Consecutive frames both signals must exceed threshold before firing.
# At 30 fps, 3 frames = 100 ms — catches even fast wing-tip transits while
# filtering single-frame noise spikes.  The spike detector (see below) handles
# the extreme case of huge signals in 1–2 frames.
CONSEC_FRAMES_REQUIRED = int(os.getenv("CONSEC_FRAMES_REQUIRED", "3"))

# Spike detector: if either signal exceeds this multiple of the adaptive
# threshold in a single frame (within disc), fire immediately.  Large low-
# altitude aircraft can cross the disc in <0.15 s (< 5 frames at 30 fps);
# their silhouette produces an unmistakable amplitude spike that noise and
# atmospheric shimmer never reach.
SPIKE_THRESHOLD_MULTIPLIER = float(os.getenv("DETECTOR_SPIKE_MULT", "4.0"))

# EMA blending factor for reference frame (0 = never update, 1 = full replace)
EMA_ALPHA = 0.02

# Freeze reference updates for this many frames after a detection
REF_FREEZE_FRAMES = ANALYSIS_FPS * 5  # 150 frames = 5 seconds

# Minimum centre-to-edge signal ratio to accept detection.
# 1.5 was too permissive — atmospheric seeing creates disk-wide speckles that
# still pass a weak ratio.  2.5 requires the inner disk to be clearly more
# active than the limb ring, which genuine transits (dark silhouette crossing
# the bright interior) reliably produce.
CENTRE_EDGE_RATIO_MIN = float(os.getenv("CENTRE_EDGE_RATIO_MIN", "2.5"))

# Disc-lost watchdog: emit a warning after this many consecutive frames with no
# disc detected.  At 30 fps, 120 frames = 4 seconds.  Configurable via env.
DISC_LOST_THRESHOLD = int(os.getenv("DISC_LOST_THRESHOLD", "120"))

# Signal trace logging (1fps = every ANALYSIS_FPS frames)
SIGNAL_TRACE_INTERVAL = ANALYSIS_FPS
SIGNAL_TRACE_SECONDS = 60
SIGNAL_TRACE_SIZE = SIGNAL_TRACE_SECONDS  # 1 entry per second

# Disk detection: re-detect every N frames (2s at 15fps)
DISK_DETECT_INTERVAL = ANALYSIS_FPS * 2
# Edge margin: exclude outermost 25% of disk radius from detection.
# 12% was too narrow — solar limb darkening, atmospheric jitter, and seeing
# turbulence concentrate right at the edge and bled into the inner mask.
# 25% creates a wide buffer zone; the solar disk interior is still well-sampled
# and aircraft cross it reliably.
DISK_MARGIN_PCT = float(os.getenv("DETECTOR_DISK_MARGIN", "0.25"))

# ---------------------------------------------------------------------------
# Centroid track consistency parameters
# ---------------------------------------------------------------------------
# Minimum centroid displacement (px at 160×90) to count as directional motion.
# At detection resolution, atmospheric wobble is ~1–2px; real transits move
# ≥3px/frame at typical aircraft speeds.  Set to 0 to disable magnitude gate.
TRACK_MIN_MAG = float(os.getenv("DETECTOR_TRACK_MIN_MAG", "2.0"))

# Minimum fraction of streak frames that must have a positive dot product
# (direction consistent with the previous frame) before firing.
# 0.6 = 60% of consec_frames_required must agree in direction.
# Set to 0.0 to disable the track gate entirely (legacy behaviour).
TRACK_MIN_AGREE_FRAC = float(os.getenv("DETECTOR_TRACK_MIN_AGREE", "0.6"))

# Recording stabilization: enabled by default; disable with DETECTOR_STABILIZE=false
RECORDING_STABILIZE = os.getenv("DETECTOR_STABILIZE", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Maximum translation shift to accept (pixels at full res).
# Atmospheric distortion produces <8 px shifts; larger shifts are mount slippage
# or a genuine pan — clamp hard so the image cannot jump.
RECORDING_STABILIZE_MAX_SHIFT = float(os.getenv("DETECTOR_STABILIZE_MAX_SHIFT", "30"))
# EMA smoothing for the cumulative offset (0=no update, 1=instant).
# 0.7 follows genuine slow drift while smoothing single-frame spikes.
RECORDING_STABILIZE_SMOOTHING = float(os.getenv("DETECTOR_STABILIZE_SMOOTHING", "0.7"))


def _stabilize_frames(
    jpeg_list: list,
    max_shift: float = RECORDING_STABILIZE_MAX_SHIFT,
    smoothing: float = RECORDING_STABILIZE_SMOOTHING,
    ref_count: int = 15,
) -> list:
    """Stabilize a list of JPEG-encoded frames in memory using phase correlation.

    Builds a reference from the average of the first *ref_count* frames
    (the pre-trigger quiet period), then applies a smoothed translation
    warp to each frame so the solar/lunar disk stays locked in place.

    Returns a new list of JPEG bytes.  On any per-frame failure the
    original bytes are passed through unchanged so the recording is
    never lost.
    """
    if not jpeg_list:
        return jpeg_list

    # Decode reference frames and build a mean reference image
    ref_gray: Optional[np.ndarray] = None
    ref_sample_count = 0
    ref_accum: Optional[np.ndarray] = None
    for jpeg in jpeg_list[:ref_count]:
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        g = cv2.GaussianBlur(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(
            np.float32
        )
        if ref_accum is None:
            ref_accum = g.copy()
        else:
            ref_accum += g
        ref_sample_count += 1

    if ref_accum is None or ref_sample_count == 0:
        return jpeg_list  # could not build reference — pass through unchanged
    ref_gray = ref_accum / ref_sample_count

    stabilized = []
    offset_x, offset_y = 0.0, 0.0
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, 92]

    for jpeg in jpeg_list:
        try:
            arr = np.frombuffer(jpeg, dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is None:
                stabilized.append(jpeg)
                continue

            gray = cv2.GaussianBlur(
                cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (5, 5), 0
            ).astype(np.float32)

            (dx, dy), response = cv2.phaseCorrelate(ref_gray, gray)
            if response >= 0.02:
                dx = float(np.clip(dx, -max_shift, max_shift))
                dy = float(np.clip(dy, -max_shift, max_shift))
                # Smooth the offset so single-frame spikes don't cause jumps
                offset_x = smoothing * dx + (1.0 - smoothing) * offset_x
                offset_y = smoothing * dy + (1.0 - smoothing) * offset_y

            if abs(offset_x) > 0.5 or abs(offset_y) > 0.5:
                M = np.float32([[1, 0, offset_x], [0, 1, offset_y]])
                bgr = cv2.warpAffine(
                    bgr,
                    M,
                    (bgr.shape[1], bgr.shape[0]),
                    flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE,
                )

            ok, buf = cv2.imencode(".jpg", bgr, encode_params)
            stabilized.append(buf.tobytes() if ok else jpeg)
        except Exception:
            stabilized.append(jpeg)

    return stabilized


def _build_centre_weight(h: int, w: int) -> np.ndarray:
    """Gaussian-ish centre weight: 1.0 at centre → 0.3 at corners.

    Used as a fallback when no disk has been detected yet.
    """
    cy, cx = h / 2, w / 2
    y = np.arange(h).reshape(-1, 1)
    x = np.arange(w).reshape(1, -1)
    dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / max(cx, cy)
    return np.clip(1.0 - 0.7 * dist, 0.3, 1.0).astype(np.float32)


CENTRE_WEIGHT = _build_centre_weight(ANALYSIS_HEIGHT, ANALYSIS_WIDTH)


def _build_spatial_masks(h: int, w: int) -> tuple:
    """Build boolean masks for centre 50% and outer edge of frame.

    Fallback masks used when no disk has been detected.
    """
    centre = np.zeros((h, w), dtype=bool)
    y1, y2 = h // 4, h * 3 // 4
    x1, x2 = w // 4, w * 3 // 4
    centre[y1:y2, x1:x2] = True
    edge = ~centre
    return centre, edge


CENTRE_MASK, EDGE_MASK = _build_spatial_masks(ANALYSIS_HEIGHT, ANALYSIS_WIDTH)


def _build_center_flux_masks(h: int, w: int) -> tuple:
    """Build fixed central-aperture and surrounding-ring masks.

    These masks are used for a cloud-robust centering cue: core brightness
    relative to the local surround and full frame average.
    """
    y = np.arange(h).reshape(-1, 1)
    x = np.arange(w).reshape(1, -1)
    cy = (h - 1) / 2.0
    cx = (w - 1) / 2.0
    dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)

    r_core = max(4.0, min(h, w) * 0.09)
    r_ring_in = max(r_core + 2.0, r_core * 1.6)
    r_ring_out = max(r_ring_in + 2.0, r_core * 2.7)

    core = dist <= r_core
    ring = (dist >= r_ring_in) & (dist <= r_ring_out)
    return core, ring


CENTER_FLUX_CORE_MASK, CENTER_FLUX_RING_MASK = _build_center_flux_masks(
    ANALYSIS_HEIGHT, ANALYSIS_WIDTH
)


# ---------------------------------------------------------------------------
# Disk detection and disk-aware masks
# ---------------------------------------------------------------------------


def _algebraic_circle_fit(pts: np.ndarray):
    """Algebraic (Bookstein) least-squares circle fit to 2-D point array.

    Solves the linear system  x·D + y·E + F = -(x²+y²)  in the least-squares
    sense.  Unlike minEnclosingCircle, the result extrapolates correctly for
    partial arcs — the fitted center may lie outside the image bounds, which is
    exactly what we need when the solar disc is clipped at the frame edge.

    Returns (cx, cy, r) as floats, or (None, None, None) on failure.
    Requires at least 5 points and a positive radius solution.
    """
    if len(pts) < 5:
        return None, None, None
    x = pts[:, 0].astype(np.float64)
    y = pts[:, 1].astype(np.float64)
    # Centre data for numerical stability.
    x0, y0 = x.mean(), y.mean()
    xc, yc = x - x0, y - y0
    A = np.column_stack([xc, yc, np.ones(len(pts))])
    b = -(xc ** 2 + yc ** 2)
    try:
        result, _, rank, _ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None, None, None
    if rank < 3:
        return None, None, None
    D, E, F = result
    cx = -D / 2.0 + x0
    cy = -E / 2.0 + y0
    r_sq = (D / 2.0) ** 2 + (E / 2.0) ** 2 - F
    if r_sq <= 0.0:
        return None, None, None
    return cx, cy, math.sqrt(r_sq)


def _detect_disk(gray: np.ndarray) -> Optional[tuple]:
    """Find the Sun/Moon disk in a downscaled grayscale frame.

    Returns (cx, cy, radius) or None if no disk found.  The center may lie
    outside the image bounds when the disk is partially clipped at a frame
    edge — callers must tolerate negative or out-of-range cx/cy values.
    """
    h, w = gray.shape[:2]
    blurred = cv2.GaussianBlur(gray, (5, 5), 1)
    min_r = min(h, w) // 8  # ~22 px at 180×320
    max_r = min(h, w) // 2  # ~90 px

    # Try progressively more permissive Hough accumulator thresholds.
    for _param2 in (15, 10, 7):
        circles = cv2.HoughCircles(
            blurred,
            cv2.HOUGH_GRADIENT,
            dp=1.0,
            minDist=min(h, w) // 2,
            param1=30,
            param2=_param2,
            minRadius=min_r,
            maxRadius=max_r,
        )
        if circles is not None:
            break
    if circles is not None:
        # Prefer largest plausible candidate (reflections tend to be smaller).
        candidates = np.round(circles[0]).astype(int)
        candidates = sorted(candidates, key=lambda c: int(c[2]), reverse=True)
        for c in candidates:
            cx, cy, r = int(c[0]), int(c[1]), int(c[2])
            if r < min_r or r > max_r:
                continue
            if cx < 0 or cx >= w or cy < 0 or cy >= h:
                continue
            return cx, cy, r

    # Fallback: threshold bright region then fit a circle algebraically.
    # The algebraic fit correctly extrapolates the disc center when the disc
    # is partially outside the frame; minEnclosingCircle (below) is biased
    # toward the visible arc and should only be used as a last resort.
    # Use Otsu's method to pick threshold automatically; fall back to fixed 160.
    otsu_thresh, _ = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    fixed_thresh = min(160, max(80, int(otsu_thresh * 0.85)))
    _, bright = cv2.threshold(blurred, fixed_thresh, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < np.pi * min_r * min_r:
        return None

    pts = largest.reshape(-1, 2).astype(np.float64)
    cx_f, cy_f, r_f = _algebraic_circle_fit(pts)
    if cx_f is not None and min_r <= r_f <= max_r * 1.5:
        return int(round(cx_f)), int(round(cy_f)), int(round(r_f))

    # Last resort: minEnclosingCircle (biased for edge/corner discs).
    (cx, cy), radius = cv2.minEnclosingCircle(largest)
    return int(cx), int(cy), int(radius)


def _build_disk_masks(
    h: int, w: int, cx: int, cy: int, radius: int, margin_pct: float
) -> tuple:
    """Build circular disk mask and limb mask from detected disk.

    Returns (disk_bool, limb_bool, weight_2d):
      - disk_bool: True inside disk minus margin (where transits happen)
      - limb_bool: True in the excluded limb ring (atmospheric shimmer zone)
      - weight_2d: float32 H×W, 1.0 inside disk → 0.0 outside
    """
    inner_r = max(1, int(radius * (1.0 - margin_pct)))

    # Inner disk mask (where we look for transits)
    disk_u8 = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(disk_u8, (cx, cy), inner_r, 255, -1)
    disk_bool = disk_u8 > 0

    # Limb ring mask (excluded zone — atmospheric shimmer)
    full_u8 = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(full_u8, (cx, cy), radius, 255, -1)
    limb_bool = (full_u8 > 0) & ~disk_bool

    # Smooth weight: 1.0 inside inner disk, falls to 0 at full radius edge
    y = np.arange(h).reshape(-1, 1)
    x = np.arange(w).reshape(1, -1)
    dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2).astype(np.float32)
    weight = np.clip(1.0 - (dist - inner_r) / max(1, radius - inner_r), 0.0, 1.0)
    # Zero everything outside the full disk
    weight[full_u8 == 0] = 0.0

    return disk_bool, limb_bool, weight


class DetectionEvent:
    """A single transit detection event."""

    __slots__ = (
        "timestamp",
        "signal_a",
        "signal_b",
        "threshold_a",
        "threshold_b",
        "recording_file",
        "flight_info",
        "frame_idx",
        "confidence",
        "confidence_score",  # D3: numeric [0,1] probability score
        "centre_ratio",
        "frame_path",
        "diff_path",
        "signal_trace",
        "predicted_flight_id",  # B4: callsign from prediction cross-link
    )

    def __init__(
        self,
        timestamp: datetime,
        signal_a: float,
        signal_b: float,
        threshold_a: float,
        threshold_b: float,
        frame_idx: int,
        confidence: str = "weak",
        confidence_score: float = 0.0,
        centre_ratio: float = 0.0,
    ):
        self.timestamp = timestamp
        self.signal_a = signal_a
        self.signal_b = signal_b
        self.threshold_a = threshold_a
        self.threshold_b = threshold_b
        self.frame_idx = frame_idx
        self.confidence = confidence
        self.confidence_score = confidence_score
        self.centre_ratio = centre_ratio
        self.recording_file: Optional[str] = None
        self.flight_info: Optional[Dict] = None
        self.frame_path: Optional[str] = None
        self.diff_path: Optional[str] = None
        self.signal_trace: Optional[List[Dict]] = None
        self.predicted_flight_id: Optional[str] = None  # B4

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "timestamp": self.timestamp.isoformat(),
            "signal_a": round(self.signal_a, 4),
            "signal_b": round(self.signal_b, 4),
            "threshold_a": round(self.threshold_a, 4),
            "threshold_b": round(self.threshold_b, 4),
            "frame_idx": self.frame_idx,
            "recording_file": self.recording_file,
            "flight_info": self.flight_info,
            "confidence": self.confidence,
            "confidence_score": round(self.confidence_score, 3),  # D3
            "centre_ratio": round(self.centre_ratio, 2),
            "predicted_flight_id": getattr(self, "predicted_flight_id", None),
        }
        if self.frame_path:
            d["frame_path"] = self.frame_path
        if self.diff_path:
            d["diff_path"] = self.diff_path
        if self.signal_trace:
            d["signal_trace"] = self.signal_trace
        return d


class TransitDetector:
    """
    Continuous RTSP stream monitor that detects visual transit events.

    Dual-signal algorithm:
      Signal A – consecutive-frame diff (catches fast aircraft)
      Signal B – centre-weighted reference-frame diff (catches slow/stationary)
    Both use mean-subtraction so global brightness shifts (scintillation) cancel.
    """

    def __init__(
        self,
        rtsp_url: str,
        capture_dir: str = "static/captures",
        on_detection: Optional[Callable[["DetectionEvent"], None]] = None,
        on_status: Optional[Callable[[Dict], None]] = None,
        record_on_detect: bool = True,
        sensitivity_scale: float = 1.0,
    ):
        self.rtsp_url = rtsp_url
        self.capture_dir = capture_dir
        self.on_detection = on_detection
        self.on_status = on_status
        self.record_on_detect = record_on_detect
        self.sensitivity_scale = max(0.1, float(sensitivity_scale))

        # Live-tunable detection parameters (match module-level defaults,
        # can be updated at runtime via update_settings())
        self.disk_margin_pct: float = DISK_MARGIN_PCT
        self.centre_ratio_min: float = CENTRE_EDGE_RATIO_MIN
        self.consec_frames_required: int = CONSEC_FRAMES_REQUIRED
        self.mf_threshold_frac: float = _MF_THRESHOLD_FRAC
        self.cnn_gate_threshold: float = float(
            os.environ.get("CNN_GATE_THRESHOLD", "0.40")
        )

        self._running = False
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Frame state
        self._prev_frame: Optional[np.ndarray] = None
        self._ref_frame: Optional[np.ndarray] = None
        self._frame_idx = 0
        self._current_frame: Optional[np.ndarray] = None
        self._current_diff_b: Optional[np.ndarray] = None

        # Live-tunable track parameters
        self.track_min_mag: float = TRACK_MIN_MAG
        self.track_min_agree_frac: float = TRACK_MIN_AGREE_FRAC

        # Consecutive-frame confirmation counter
        self._consec_above = 0

        # Centroid track state (at detection resolution)
        self._track_centroid_prev: Optional[tuple] = None  # (cx, cy) last frame
        self._track_displacement_prev: Optional[tuple] = None  # (dx, dy) last frame
        self._track_agree_count: int = 0  # frames in current streak with positive dot

        # Freeze reference updates after detection
        self._ref_freeze_until = 0

        # Adaptive threshold history
        self._scores_a: Deque[float] = collections.deque(maxlen=HISTORY_SIZE)
        self._scores_b: Deque[float] = collections.deque(maxlen=HISTORY_SIZE)

        # Long-run background window for noise density guard
        self._bg_scores_a: Deque[float] = collections.deque(maxlen=BG_HISTORY_SIZE)

        # Cooldown
        self._last_detection_time: float = 0

        # Stats
        self._start_time: Optional[float] = None
        self._total_frames = 0
        self._detection_count = 0

        # Event log — bounded deque so append is atomic and never races with readers
        self.events: Deque[DetectionEvent] = collections.deque(maxlen=100)

        # Signal trace ring buffer (1fps, last 60s)
        self._signal_trace: Deque[Dict] = collections.deque(maxlen=SIGNAL_TRACE_SIZE)

        # Disk detection state
        self._disk_cx: Optional[int] = None
        self._disk_cy: Optional[int] = None
        self._disk_radius: Optional[int] = None
        self._disk_mask: Optional[np.ndarray] = None  # bool H×W — inner disk
        self._limb_mask: Optional[np.ndarray] = None  # bool H×W — excluded limb ring
        self._disk_weight: Optional[np.ndarray] = None  # float32 H×W — smooth weight
        self._disk_detected = False
        self._disk_detected_at: float = 0.0  # monotonic, updated each disc cycle

        # Center-flux telemetry (cloud-robust centering cue)
        self._center_flux_core_mean: float = 0.0
        self._center_flux_ring_mean: float = 0.0
        self._center_flux_frame_mean: float = 0.0
        self._center_flux_core_to_ring: float = 1.0
        self._center_flux_core_to_frame: float = 1.0

        # Disc-lost watchdog state
        self._disc_lost_frames: int = 0  # consecutive frames without a disc
        self._disc_lost_warning: bool = False  # True once threshold crossed

        # B4 — Prediction-detection cross-link
        self._primed_events: dict = {}  # {flight_id: primed-event dict}
        self._gate_miss_events: collections.deque = collections.deque(maxlen=50)

        # D1 — Wavelet detrending: raw Signal-B ring buffer for pywt
        _wt_size = 128  # 4.3 s at 30 fps; level-3 sym4 requires >= 32
        self._wt_buf: collections.deque = collections.deque(maxlen=_wt_size)

        # D2 — Matched-filter gate: per-frame triggered boolean history
        self._triggered_buf: collections.deque = collections.deque(
            maxlen=max(_MF_TEMPLATES) + 5
        )

        # E3 — CNN second-stage gate: ring buffer of last CLIP_T analysis frames
        _CNN_CLIP_T = 15
        self._cnn_buf: collections.deque = collections.deque(maxlen=_CNN_CLIP_T)
        self._cnn_gate_threshold: float = (
            0.40  # suppress if CNN transit prob < threshold
        )
        self._cnn_available: Optional[bool] = None  # lazily resolved on first fire

        # Active recording process (for auto-record on detection)
        self._rec_process: Optional[subprocess.Popen] = None
        self._rec_file: Optional[str] = None

        # Unified circular buffer: JPEG-encoded frames from the single
        # 320×180 @ 30fps stream.  Serves as both the pre-trigger recording
        # source and the detection feed (frames are also decoded to numpy).
        self._frame_buffer: Deque[bytes] = collections.deque(
            maxlen=PRE_BUFFER_SECONDS * ANALYSIS_FPS
        )
        self._frame_buffer_total: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        """Start the detection loop. Returns True if started."""
        if self._running:
            logger.warning("[Detector] Already running")
            return False

        self._running = True
        self._start_time = time.monotonic()
        self._total_frames = 0
        self._frame_idx = 0
        self._prev_frame = None
        self._ref_frame = None
        self._current_frame = None
        self._current_diff_b = None
        self._consec_above = 0
        self._ref_freeze_until = 0
        self._scores_a.clear()
        self._scores_b.clear()
        self._signal_trace.clear()
        self._disk_cx = None
        self._disk_cy = None
        self._disk_radius = None
        self._disk_mask = None
        self._limb_mask = None
        self._disk_weight = None
        self._disk_detected = False
        self._disk_detected_at = 0.0
        self._center_flux_core_mean = 0.0
        self._center_flux_ring_mean = 0.0
        self._center_flux_frame_mean = 0.0
        self._center_flux_core_to_ring = 1.0
        self._center_flux_core_to_frame = 1.0
        self._disc_lost_frames = 0
        self._disc_lost_warning = False

        self._frame_buffer.clear()
        self._frame_buffer_total = 0

        self._thread = threading.Thread(
            target=self._reader_loop, name="transit-detector", daemon=True
        )
        self._thread.start()

        logger.info(f"[Detector] Started — reading {self.rtsp_url}")
        self._emit_status("running")
        return True

    def stop(self) -> None:
        """Stop the detection loop and clean up."""
        self._running = False

        if self._process and self._process.poll() is None:
            try:
                self._process.kill()
                self._process.wait(timeout=5)
            except Exception:
                pass
        self._process = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

        logger.info("[Detector] Stopped")
        self._emit_status("stopped")

    def get_status(self) -> Dict[str, Any]:
        """Return current detector status."""
        # Evict expired primed events (safety net for when frame processing is idle)
        _now = time.time()
        for _fid in [
            fid for fid, e in self._primed_events.items() if _now > e["expires_at"]
        ]:
            del self._primed_events[_fid]

        elapsed = 0.0
        fps = 0.0
        if self._start_time and self._running:
            elapsed = time.monotonic() - self._start_time
            fps = self._total_frames / max(elapsed, 0.001)

        return {
            "running": self._running,
            "elapsed_seconds": round(elapsed, 1),
            "total_frames": self._total_frames,
            "fps": round(fps, 1),
            "detections": self._detection_count,
            "recent_events": [e.to_dict() for e in list(self.events)[-10:]],
            "recent_gate_misses": list(self._gate_miss_events)[-10:],
            "recording_active": self._rec_process is not None,
            "disk_detected": self._disk_detected,
            "disk_detected_at": self._disk_detected_at,
            "disc_lost_warning": self._disc_lost_warning,
            "disc_lost_frames": self._disc_lost_frames,
            "analysis_resolution": f"{ANALYSIS_WIDTH}x{ANALYSIS_HEIGHT}@{ANALYSIS_FPS}fps",
            "buffer_frames": len(self._frame_buffer),
            "disk_info": (
                {
                    "cx": self._disk_cx,
                    "cy": self._disk_cy,
                    "radius": self._disk_radius,
                    "margin_pct": self.disk_margin_pct,
                }
                if self._disk_detected
                else None
            ),
            "center_flux": {
                "core_mean": round(self._center_flux_core_mean, 3),
                "ring_mean": round(self._center_flux_ring_mean, 3),
                "frame_mean": round(self._center_flux_frame_mean, 3),
                "core_to_ring": round(self._center_flux_core_to_ring, 4),
                "core_to_frame": round(self._center_flux_core_to_frame, 4),
            },
            "settings": {
                "disk_margin_pct": self.disk_margin_pct,
                "centre_ratio_min": self.centre_ratio_min,
                "consec_frames": self.consec_frames_required,
                "sensitivity_scale": self.sensitivity_scale,
                "spike_threshold_mult": SPIKE_THRESHOLD_MULTIPLIER,
                "track_min_mag": self.track_min_mag,
                "track_min_agree_frac": self.track_min_agree_frac,
                "mf_threshold_frac": self.mf_threshold_frac,
                "cnn_gate_threshold": self.cnn_gate_threshold,
                "cnn_available": bool(self._cnn_available),
                "gates_mode": "soft",
            },
            # B4: active primed prediction windows (flight_id → ETA info)
            "primed_events": [
                {
                    "flight_id": e["flight_id"],
                    "eta_s": round(e["expires_at"] - time.time() - 30, 0),
                    "sep_deg": round(e.get("sep_deg", 0), 2),
                }
                for e in self._primed_events.values()
                if time.time() <= e["expires_at"]
            ],
            "signal_trace": list(self._signal_trace),
        }

    def get_latest_hires_jpeg(self) -> Optional[bytes]:
        """Return the most recent JPEG frame from the unified buffer.

        Returns None if the detector is not running or the buffer is empty.
        """
        if not self._running or not self._frame_buffer:
            return None
        return self._frame_buffer[-1]

    # ------------------------------------------------------------------
    # B4 — Prediction-detection cross-link
    # ------------------------------------------------------------------

    def prime_for_event(
        self,
        eta_s: float,
        flight_id: str,
        sep_deg: float = 0.0,
    ) -> None:
        """Pre-arm the detector for a predicted transit.

        For the duration of the predicted event window the detection
        sensitivity is raised (consecutive-frames gate is halved) and
        post-detection enrichment can match the fired event back to the
        scheduled flight.

        Parameters
        ----------
        eta_s:
            Estimated seconds until mid-transit from now.
        flight_id:
            Callsign or unique identifier of the predicted flight.
        sep_deg:
            Predicted angular separation at closest approach (degrees).
        """
        import time as _time

        flight_id = normalize_aircraft_display_id(flight_id)
        if not flight_id:
            logger.debug("[Detector] prime_for_event: empty id after normalize, skip")
            return

        entry = {
            "flight_id": flight_id,
            "eta_s": eta_s,
            "sep_deg": sep_deg,
            "primed_at": _time.time(),
            "expires_at": _time.time() + eta_s + 30,  # 30 s post-transit grace
        }
        self._primed_events[flight_id] = entry
        logger.info(
            f"[Detector] Primed for {flight_id} in {eta_s:.0f}s "
            f"(sep={sep_deg:.2f}°) — sensitivity raised"
        )

    def get_primed_flight_id(self, event_ts: float) -> Optional[str]:
        """Return the flight_id of any primed event active at *event_ts*, or None."""
        for fid, entry in list(self._primed_events.items()):
            if event_ts <= entry["expires_at"]:
                return fid
            # Expired — clean up
            del self._primed_events[fid]
        return None

    def update_settings(
        self,
        disk_margin_pct: Optional[float] = None,
        centre_ratio_min: Optional[float] = None,
        consec_frames: Optional[int] = None,
        sensitivity_scale: Optional[float] = None,
        track_min_mag: Optional[float] = None,
        track_min_agree_frac: Optional[float] = None,
        mf_threshold_frac: Optional[float] = None,
        cnn_gate_threshold: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Update live detection parameters without restarting the detector.

        margin change forces a disk-mask rebuild on the next disk-detect cycle.
        """
        if disk_margin_pct is not None:
            self.disk_margin_pct = float(max(0.0, min(0.6, disk_margin_pct)))
            # Force disk mask rebuild on next detection cycle
            self._disk_mask = None
            self._limb_mask = None
            self._disk_weight = None
        if centre_ratio_min is not None:
            self.centre_ratio_min = float(max(0.5, min(10.0, centre_ratio_min)))
        if consec_frames is not None:
            self.consec_frames_required = int(max(1, min(30, consec_frames)))
        if sensitivity_scale is not None:
            self.sensitivity_scale = float(max(0.1, min(10.0, sensitivity_scale)))
        if track_min_mag is not None:
            self.track_min_mag = float(max(0.0, min(20.0, track_min_mag)))
        if track_min_agree_frac is not None:
            self.track_min_agree_frac = float(max(0.0, min(1.0, track_min_agree_frac)))
        if mf_threshold_frac is not None:
            self.mf_threshold_frac = float(max(0.3, min(1.0, mf_threshold_frac)))
        if cnn_gate_threshold is not None:
            self.cnn_gate_threshold = float(max(0.0, min(1.0, cnn_gate_threshold)))
        logger.info(
            f"[Detector] Settings updated: margin={self.disk_margin_pct:.0%} "
            f"ratio_min={self.centre_ratio_min} consec={self.consec_frames_required} "
            f"sens={self.sensitivity_scale:.2f} "
            f"track_mag={self.track_min_mag} track_agree={self.track_min_agree_frac:.0%} "
            f"mf_thresh={self.mf_threshold_frac:.0%}"
        )
        return self.get_status()["settings"]

    # ------------------------------------------------------------------
    # Internal: frame reading
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Unified reader: single 320×180 @ 30fps stream for detection + recording buffer."""
        cmd = [
            FFMPEG,
            "-rtsp_transport",
            "tcp",
            "-timeout",
            "10000000",  # 10 s socket timeout (µs)
            "-i",
            self.rtsp_url,
            "-vf",
            f"scale={ANALYSIS_WIDTH}:{ANALYSIS_HEIGHT}",
            "-r",
            str(ANALYSIS_FPS),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-an",
            "pipe:1",
        ]

        reconnect_delay = 2
        max_reconnect_delay = 30
        max_reconnect_attempts = 5
        consecutive_fails = 0
        _last_stream_lost_warn = 0.0
        _encode_params = [cv2.IMWRITE_JPEG_QUALITY, 85]

        while self._running:
            try:
                logger.info(f"[Detector] Launching ffmpeg: {' '.join(cmd)}")
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=FRAME_BYTES * 4,
                )

                got_frames = False
                self._prev_frame = None
                self._consec_above = 0
                self._track_centroid_prev = None
                self._track_displacement_prev = None
                self._track_agree_count = 0

                while self._running:
                    raw = self._process.stdout.read(FRAME_BYTES)
                    if len(raw) < FRAME_BYTES:
                        break

                    if not got_frames:
                        got_frames = True
                        consecutive_fails = 0
                        reconnect_delay = 2

                    frame_u8 = np.frombuffer(raw, dtype=np.uint8).reshape(
                        (ANALYSIS_HEIGHT, ANALYSIS_WIDTH, 3)
                    )

                    # JPEG-encode into circular buffer for recording
                    bgr = cv2.cvtColor(frame_u8, cv2.COLOR_RGB2BGR)
                    ok, jpeg_buf = cv2.imencode(".jpg", bgr, _encode_params)
                    if ok:
                        self._frame_buffer.append(jpeg_buf.tobytes())
                        self._frame_buffer_total += 1

                    frame = frame_u8.astype(np.float32)
                    self._total_frames += 1
                    self._frame_idx += 1
                    self._process_frame(frame)

            except Exception as e:
                logger.error(f"[Detector] Frame reader error: {e}")

            if self._process and self._process.poll() is None:
                try:
                    self._process.kill()
                    self._process.wait(timeout=3)
                except Exception:
                    pass

            if not self._running:
                break

            if not got_frames:
                consecutive_fails += 1
            else:
                consecutive_fails = 0

            if consecutive_fails >= max_reconnect_attempts:
                logger.warning(
                    f"[Detector] Stream unavailable after {max_reconnect_attempts} "
                    "attempts — giving up. Restart detection manually."
                )
                self._emit_status("disconnected")
                self._running = False
                break

            now_ = time.time()
            if now_ - _last_stream_lost_warn >= 60:
                logger.warning(
                    f"[Detector] Stream lost — reconnecting in {reconnect_delay}s "
                    f"(attempt {consecutive_fails}/{max_reconnect_attempts})"
                )
                _last_stream_lost_warn = now_
            else:
                logger.debug(
                    f"[Detector] Stream lost — reconnecting in {reconnect_delay}s"
                )
            self._emit_status("reconnecting")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

        logger.info("[Detector] Reader loop exited")

    # (Hi-res reader removed: unified reader in _reader_loop feeds both
    #  detection and the JPEG circular buffer from a single RTSP stream.)

    # ------------------------------------------------------------------
    # Internal: frame processing (dual-signal detection)
    # ------------------------------------------------------------------

    def _process_frame(self, frame: np.ndarray) -> None:
        """
        Process one 160×90 RGB frame.

        Computes signal A (consecutive diff) and signal B (reference diff).
        Both use mean-subtraction for scintillation immunity.

        When a disk is detected (Sun/Moon), signals are computed only within
        the inner disk (excluding the atmospheric limb margin).  The spatial
        concentration check compares inner-disk signal vs limb-ring signal,
        rejecting atmospheric shimmer that dominates the disk edge.

        Falls back to the original rectangular centre/edge masks if no disk
        is found in the frame.
        """
        self._current_frame = frame
        gray = np.clip(frame.mean(axis=2), 0, 255).astype(np.uint8)

        # --- Center-flux telemetry ---
        frame_mean = float(gray.mean())
        if CENTER_FLUX_CORE_MASK.any():
            core_mean = float(gray[CENTER_FLUX_CORE_MASK].mean())
        else:
            core_mean = frame_mean
        if CENTER_FLUX_RING_MASK.any():
            ring_mean = float(gray[CENTER_FLUX_RING_MASK].mean())
        else:
            ring_mean = frame_mean

        self._center_flux_core_mean = core_mean
        self._center_flux_ring_mean = ring_mean
        self._center_flux_frame_mean = frame_mean
        self._center_flux_core_to_ring = core_mean / max(ring_mean, 1e-3)
        self._center_flux_core_to_frame = core_mean / max(frame_mean, 1e-3)

        # --- Periodic disk detection (every 2s), or immediate rebuild if mask cleared ---
        rebuild_needed = self._disk_mask is None and self._disk_cx is not None
        if self._frame_idx % DISK_DETECT_INTERVAL == 0 or rebuild_needed:
            result = _detect_disk(gray)
            if result is not None:
                cx, cy, r = result
                if not self._disk_detected:
                    logger.info(
                        f"[Detector] Disk found: centre=({cx},{cy}), "
                        f"radius={r}px, margin={self.disk_margin_pct*100:.0f}%"
                    )
                self._disk_cx, self._disk_cy, self._disk_radius = cx, cy, r
                self._disk_mask, self._limb_mask, self._disk_weight = _build_disk_masks(
                    ANALYSIS_HEIGHT, ANALYSIS_WIDTH, cx, cy, r, self.disk_margin_pct
                )
                self._disk_detected = True
                self._disk_detected_at = time.monotonic()
                # Reset disc-lost watchdog when disc is (re-)acquired
                if self._disc_lost_frames > 0 or self._disc_lost_warning:
                    if self._disc_lost_warning:
                        logger.info(
                            "[Detector] Disc re-acquired — clearing disc-lost warning"
                        )
                        self._emit_status("disc_reacquired")
                    self._disc_lost_frames = 0
                    self._disc_lost_warning = False
            elif self._disk_detected:
                logger.debug("[Detector] Disk lost — falling back to rectangular masks")
                self._disk_detected = False

        # --- Disc-lost watchdog ---
        if not self._disk_detected:
            self._disc_lost_frames += 1
            if (
                self._disc_lost_frames >= DISC_LOST_THRESHOLD
                and not self._disc_lost_warning
            ):
                self._disc_lost_warning = True
                logger.warning(
                    f"[Detector] ⚠️ Disc lost for {self._disc_lost_frames} frames "
                    f"({self._disc_lost_frames / ANALYSIS_FPS:.0f}s) — "
                    "telescope may be mispointed or solar tracking is off."
                )
                self._emit_status("disc_lost")
                # Async Telegram alert (non-blocking)
                threading.Thread(
                    target=self._send_disc_lost_alert,
                    daemon=True,
                    name="disc-lost-alert",
                ).start()
        else:
            # Disc is present — keep lost counter at zero between detect intervals
            self._disc_lost_frames = 0

        # Select masks: disk-aware if available, else rectangular fallback
        use_disk = self._disk_detected and self._disk_mask is not None
        inner_mask = self._disk_mask if use_disk else CENTRE_MASK
        outer_mask = self._limb_mask if use_disk else EDGE_MASK
        weight_2d = self._disk_weight if use_disk else CENTRE_WEIGHT

        # --- Signal A: consecutive-frame diff ---
        score_a = 0.0
        if self._prev_frame is not None:
            diff_a = frame - self._prev_frame
            mean_shift = diff_a.mean(axis=(0, 1), keepdims=True)
            diff_a -= mean_shift
            # Mask to inner disk only
            abs_a = np.abs(diff_a).mean(axis=2)
            if inner_mask.any():
                score_a = float(abs_a[inner_mask].mean())
            else:
                score_a = float(abs_a.mean())

        # --- EMA reference blending (replaces hard swap) ---
        if self._ref_frame is None:
            self._ref_frame = frame.copy()
        elif self._frame_idx > self._ref_freeze_until:
            self._ref_frame = (1 - EMA_ALPHA) * self._ref_frame + EMA_ALPHA * frame

        # --- Signal B: disk-weighted reference diff ---
        diff_b = frame - self._ref_frame
        mean_shift_b = diff_b.mean(axis=(0, 1), keepdims=True)
        diff_b -= mean_shift_b
        self._current_diff_b = diff_b
        weighted = np.abs(diff_b) * weight_2d[:, :, np.newaxis]
        score_b_raw = float(weighted.mean())

        # D1 — Wavelet detrending: push raw value, compute detrended magnitude.
        # The wavelet detail signal removes slow drifts (cloud edges, background
        # lighting ramp) that inflate the EMA reference and cause false positives.
        # Falls back to raw score_b if pywt is unavailable.
        self._wt_buf.append(score_b_raw)
        score_b = _wavelet_detrend(self._wt_buf)

        # --- Spatial concentration: inner disk vs limb/edge ---
        abs_diff_gray = np.abs(diff_b).mean(axis=2)  # H×W
        if inner_mask.any():
            inner_score = float(abs_diff_gray[inner_mask].mean())
        else:
            inner_score = float(abs_diff_gray.mean())
        if outer_mask is not None and outer_mask.any():
            outer_score = float(abs_diff_gray[outer_mask].mean())
        else:
            outer_score = 0.001
        centre_ratio = inner_score / max(outer_score, 0.001)

        self._prev_frame = frame.copy()

        # E3 — Buffer grayscale frames for CNN second-stage gate
        self._cnn_buf.append(frame[:, :, 0].copy())  # single channel from RGB24

        # --- Store scores ---
        self._scores_a.append(score_a)
        self._scores_b.append(score_b)
        self._bg_scores_a.append(score_a)

        # --- Centroid of diff activity within inner disk (track consistency) ---
        # Computed at detection resolution (160×90).  The weighted centroid of
        # abs_diff_gray within inner_mask gives the spatial centre of whatever
        # is changing — a real transit moves it consistently; noise scrambles it.
        track_cx, track_cy = None, None
        if inner_mask.any():
            masked_diff = abs_diff_gray.copy()
            masked_diff[~inner_mask] = 0.0
            total_w = masked_diff.sum()
            if total_w > 0:
                ys_idx, xs_idx = np.nonzero(inner_mask)
                weights = masked_diff[inner_mask]
                track_cx = float(np.dot(xs_idx, weights) / total_w)
                track_cy = float(np.dot(ys_idx, weights) / total_w)

        # Update track displacement and direction-agreement counter
        if track_cx is not None and self._track_centroid_prev is not None:
            dx = track_cx - self._track_centroid_prev[0]
            dy = track_cy - self._track_centroid_prev[1]
            mag = (dx * dx + dy * dy) ** 0.5
            if self._track_displacement_prev is not None and mag >= self.track_min_mag:
                pdx, pdy = self._track_displacement_prev
                dot = dx * pdx + dy * pdy
                if dot > 0:
                    self._track_agree_count += 1
                # Note: we do NOT reset _track_agree_count on a negative dot —
                # a single reversed frame during a real transit (e.g. centroid
                # snapped by a seeing spike) should not break the streak.
            self._track_displacement_prev = (dx, dy)
        else:
            self._track_displacement_prev = None

        self._track_centroid_prev = (
            (track_cx, track_cy) if track_cx is not None else None
        )

        # --- Signal trace (1fps) ---
        if self._frame_idx % SIGNAL_TRACE_INTERVAL == 0:
            if len(self._scores_a) >= ANALYSIS_FPS * 3:
                t_a = self._adaptive_threshold(self._scores_a) * self.sensitivity_scale
                t_b = self._adaptive_threshold(self._scores_b) * self.sensitivity_scale
            else:
                t_a = t_b = 0.0
            self._signal_trace.append(
                {
                    "t": round(time.time(), 3),
                    "a": round(score_a, 5),
                    "b": round(score_b, 5),
                    "ta": round(t_a, 5),
                    "tb": round(t_b, 5),
                    "cr": round(centre_ratio, 2),
                    "cf": round(self._center_flux_core_to_frame, 3),
                    "disk": self._disk_detected,
                    "tca": self._track_agree_count,
                }
            )

        # Need enough history for adaptive threshold
        if len(self._scores_a) < ANALYSIS_FPS * 3:  # ~3 seconds warmup
            return

        # --- Adaptive threshold ---
        thresh_a = self._adaptive_threshold(self._scores_a) * self.sensitivity_scale
        thresh_b = self._adaptive_threshold(self._scores_b) * self.sensitivity_scale

        # --- Noise density guard ---
        # If the last 3 seconds are unusually active vs. the 60-second baseline
        # (e.g. scene flooded with random sunspot-like hits), raise thresholds
        # proportionally to suppress the false-positive burst.
        if len(self._bg_scores_a) >= ANALYSIS_FPS * 10:
            bg_median = float(np.median(self._bg_scores_a))
            recent = list(self._scores_a)[-ANALYSIS_FPS * 3 :]
            recent_median = float(np.median(recent)) if recent else 0.0
            noise_factor = max(1.0, (recent_median / max(bg_median, 1e-6)) * 0.5)
        else:
            noise_factor = 1.0
        thresh_a *= noise_factor
        thresh_b *= noise_factor

        # --- Disc gate with grace period ---
        # A large aircraft transiting the disc can cause Hough to lose the circle.
        # Allow detection to continue for a grace period after the last successful
        # disc detection so the transit itself doesn't blind the detector.
        _DISC_GRACE_FRAMES = ANALYSIS_FPS * 3  # 3 seconds
        disc_ok = self._disk_detected or (
            self._disc_lost_frames < _DISC_GRACE_FRAMES
            and self._disk_mask is not None
        )
        if not disc_ok:
            self._consec_above = 0
            return

        # Centre ratio is SOFT — affects confidence, never blocks trigger
        ratio_ok = centre_ratio >= self.centre_ratio_min

        # Hard inner-disc guard: score_a must clear its own threshold before
        # score_b alone can drive consec/MF accumulation.  score_b tracks an EMA
        # diff that is prone to limb scintillation; without this guard a sub-
        # threshold score_a (e.g. 0.38 vs thresh 0.51) combined with a score_b
        # barely above its floor (0.5 floor) fires a false cooldown that then
        # blocks the real transit detection that follows.
        inner_active = score_a >= thresh_a

        # --- Spike detector: single-frame extreme amplitude → immediate fire ---
        spike_a = score_a > thresh_a * SPIKE_THRESHOLD_MULTIPLIER
        spike_b = score_b > thresh_b * SPIKE_THRESHOLD_MULTIPLIER
        # Use disc_ok (includes 3-second grace period) rather than the raw
        # disk_detected flag.  A large low-altitude aircraft crossing the disk
        # can disrupt Hough circle detection for 1-3 frames — exactly when the
        # amplitude spike is strongest and when we most need the spike gate.
        spike_gate = (spike_a or spike_b) and disc_ok and inner_active

        # --- Consecutive-frame confirmation ---
        # OR logic: fire if EITHER signal exceeds threshold (within disc).
        # Centre ratio is a soft confidence modifier, not a trigger gate.
        triggered = score_a > thresh_a or score_b > thresh_b

        if triggered and inner_active:
            self._consec_above += 1
        else:
            self._consec_above = 0
            self._track_agree_count = 0

        # D2 — Matched-filter gate
        # Templates are checked shortest-first; first match wins.
        # Hit-rate thresholds are graduated via _mf_hit_required() so that
        # long-template slots tolerate atmospheric seeing gaps without lowering
        # the bar for short noisy bursts.
        self._triggered_buf.append(triggered and inner_active)
        mf_gate = False
        mf_duration_f = 0
        buf_list = list(self._triggered_buf)
        for _n in _MF_TEMPLATES:
            if len(buf_list) >= _n:
                _n_hit = sum(buf_list[-_n:])
                if _n_hit >= _mf_hit_required(_n):
                    mf_gate = True
                    mf_duration_f = _n
                    break

        # B4: raise sensitivity during a primed prediction window
        _now = time.time()
        _active_prime = next(
            (e for e in self._primed_events.values() if _now <= e["expires_at"]),
            None,
        )
        for _fid in [
            fid for fid, e in self._primed_events.items() if _now > e["expires_at"]
        ]:
            _exp = self._primed_events[_fid]
            _snr_b = round(score_b / max(thresh_b, 0.001), 3)
            _mf_hits = sum(list(self._triggered_buf)[-30:])
            self._gate_miss_events.append({
                "flight_id": _fid,
                "sep_deg": _exp.get("sep_deg"),
                "missed_at": datetime.now().isoformat(),
                "last_snr_b": _snr_b,
                "last_mf_hits_30f": int(_mf_hits),
                "last_consec": self._consec_above,
            })
            logger.info(
                "[Detector] Gate miss: primed window for %s expired without detection "
                "(sep=%.2f° snr_b=%.3f mf_hits_30f=%d consec=%d)",
                _fid, _exp.get("sep_deg", 0), _snr_b, _mf_hits, self._consec_above,
            )
            del self._primed_events[_fid]

        effective_consec = (
            max(1, self.consec_frames_required // 2)
            if _active_prime
            else self.consec_frames_required
        )

        consec_gate = self._consec_above >= effective_consec

        # --- Decide whether to fire ---
        should_fire = spike_gate or consec_gate or mf_gate
        if not should_fire:
            return

        # Hard centre-ratio gate for long MF templates only (n > 60 frames).
        # Sustained limb scintillation can accumulate enough triggered frames to
        # pass a 90- or 120-frame template at 45% hit rate.  A genuine transit
        # always produces inner-disk signal stronger than limb signal (ratio > 1.0);
        # sustained limb shimmer produces the opposite.  The spike and consec gates
        # are exempt — their own amplitude requirements are already discriminating.
        if mf_gate and not consec_gate and not spike_gate and mf_duration_f > 60:
            if centre_ratio < 1.0:
                logger.info(
                    "[Detector] Long-MF hard centre-ratio gate rejected "
                    "(ratio=%.2f < 1.0, n=%d frames) — likely limb scintillation",
                    centre_ratio, mf_duration_f,
                )
                return

        # Track gate is now SOFT: it affects confidence, never blocks recording.
        min_agree = int(effective_consec * self.track_min_agree_frac)
        if mf_gate and not consec_gate:
            min_agree = max(1, min_agree // 2)
        track_ok = self._track_agree_count >= min_agree

        if not track_ok and not spike_gate:
            logger.info(
                f"[Detector] Track gate soft-fail (recording anyway): "
                f"agree={self._track_agree_count} need={min_agree}"
            )

        # Reset counters
        if consec_gate:
            self._consec_above = 0
            self._track_agree_count = 0
        if mf_gate or spike_gate:
            self._triggered_buf.clear()

        # Cooldown — log suppressed triggers once per cooldown period for forensic evidence
        now = time.time()
        if now - self._last_detection_time < DETECTION_COOLDOWN:
            if not getattr(self, '_cooldown_logged', False):
                self._cooldown_logged = True
                logger.debug(
                    "[Detector] COOLDOWN suppressed trigger "
                    "(gate=%s score_a=%.4f thresh_a=%.4f score_b=%.4f thresh_b=%.4f "
                    "since_last=%.1fs cooldown=%ds)",
                    "spike" if spike_gate else ("consec" if consec_gate else "mf"),
                    score_a, thresh_a, score_b, thresh_b,
                    now - self._last_detection_time, DETECTION_COOLDOWN,
                )
            return
        self._last_detection_time = now
        self._cooldown_logged = False

        _raw_prime = _active_prime["flight_id"] if _active_prime else ""
        predicted_fid = normalize_aircraft_display_id(_raw_prime) or None
        self._ref_freeze_until = self._frame_idx + REF_FREEZE_FRAMES

        # Determine gate type for logging/sidecar
        if spike_gate:
            gate_type = "spike"
        elif consec_gate:
            gate_type = "consec"
        else:
            gate_type = "mf"

        self._fire_detection(
            score_a,
            score_b,
            thresh_a,
            thresh_b,
            centre_ratio,
            predicted_flight_id=predicted_fid,
            mf_gate=mf_gate,
            mf_duration_f=mf_duration_f,
            effective_consec=effective_consec,
            spike_gate=spike_gate,
            track_ok=track_ok,
        )

    @staticmethod
    def _adaptive_threshold(scores: Deque[float]) -> float:
        """
        Compute adaptive threshold: median + max(3×MAD, 0.5×median).

        Same formula as the JS gallery scanner.
        """
        arr = np.array(scores)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med)))
        # Floor prevents threshold reaching zero when the stream delivers duplicate
        # frames (static/paused RTSP): all score_a values become 0, median=0, MAD=0,
        # threshold=0, and the spike gate fires on any compression artifact.
        # 0.5 intensity units is below any real atmospheric signal (~2-5 units).
        return max(0.5, med + max(3.0 * mad, 0.5 * med))

    # ------------------------------------------------------------------
    # Internal: detection event handling
    # ------------------------------------------------------------------

    def _fire_detection(
        self,
        score_a: float,
        score_b: float,
        thresh_a: float,
        thresh_b: float,
        centre_ratio: float,
        predicted_flight_id: Optional[str] = None,
        mf_gate: bool = False,
        mf_duration_f: int = 0,
        effective_consec: int = 0,
        spike_gate: bool = False,
        track_ok: bool = True,
    ) -> None:
        """Handle a confirmed transit detection.

        CNN is advisory only — it modifies confidence but never blocks recording.
        Diagnostic save failure is logged but does not suppress the event.
        """

        # E3 — CNN advisory: score confidence, never block
        cnn_confidence: float = 0.0
        cnn_verdict: str = "n/a"
        if self._cnn_available is None:
            try:
                from src.transit_classifier import get_classifier

                self._cnn_available = get_classifier().available
            except Exception:
                self._cnn_available = False

        if self._cnn_available and len(self._cnn_buf) >= 15:
            try:
                from src.transit_classifier import get_classifier

                frames = np.stack(list(self._cnn_buf), axis=0)
                is_transit, cnn_confidence = get_classifier(
                    confidence_threshold=self.cnn_gate_threshold
                ).classify(frames)
                cnn_verdict = f"cnn:{cnn_confidence:.2f}"
                if not is_transit:
                    logger.info(
                        "[Detector] CNN advisory: low confidence %.3f < %.2f "
                        "(recording anyway)",
                        cnn_confidence,
                        self.cnn_gate_threshold,
                    )
            except Exception as _cnn_exc:
                logger.debug("[CNN] advisory error: %s", _cnn_exc)

        self._detection_count += 1
        ts = datetime.now()

        # D3 — Probabilistic confidence score
        snr_a = score_a / max(thresh_a, 0.001)
        snr_b = score_b / max(thresh_b, 0.001)
        snr = max(snr_a, snr_b)  # OR trigger: best signal counts
        ratio_factor = min(centre_ratio / 5.0, 1.0)
        _ec = effective_consec or self.consec_frames_required
        track_factor = min(self._track_agree_count / max(_ec, 1), 1.0)
        _logit = 0.5 * snr + 0.3 * ratio_factor + 0.2 * track_factor - 1.2

        # Soft penalties: reduce confidence, don't block
        if mf_gate and not (self._consec_above >= _ec):
            _logit -= 0.15
        if not track_ok:
            _logit -= 0.3
        if cnn_verdict != "n/a" and cnn_confidence < self.cnn_gate_threshold:
            _logit -= 0.25
        if centre_ratio < self.centre_ratio_min:
            _logit -= 0.2
        if spike_gate:
            _logit += 0.4  # large amplitude spike is strong evidence

        _logit = max(-10.0, min(10.0, _logit))
        confidence_score = round(1.0 / (1.0 + math.exp(-_logit)), 3)

        if snr > 2.0:
            confidence = "strong"
        elif spike_gate or confidence_score >= 0.4:
            confidence = "weak"
        else:
            confidence = "speculative"

        # Determine gate label
        if spike_gate:
            gate_label = "spike"
        elif mf_gate:
            gate_label = f"mf:{mf_duration_f}f"
        else:
            gate_label = "consec"

        event = DetectionEvent(
            timestamp=ts,
            signal_a=score_a,
            signal_b=score_b,
            threshold_a=thresh_a,
            threshold_b=thresh_b,
            frame_idx=self._frame_idx,
            confidence=confidence,
            confidence_score=confidence_score,
            centre_ratio=centre_ratio,
        )
        event.predicted_flight_id = predicted_flight_id

        logger.info(
            f"[Detector] TRANSIT DETECTED at {ts.strftime('%H:%M:%S.%f')[:-3]} "
            f"[{confidence}|score={confidence_score:.2f}] "
            f"(A={score_a:.4f}/{thresh_a:.4f}, B={score_b:.4f}/{thresh_b:.4f}, "
            f"CR={centre_ratio:.2f}, gate={gate_label}"
            + (f", track={'ok' if track_ok else 'soft-fail'}")
            + (f", {cnn_verdict}" if cnn_verdict != "n/a" else "")
            + (f", primed={predicted_flight_id}" if predicted_flight_id else "")
            + ")"
        )

        # Save diagnostic frames — log failure but proceed with recording
        if not self._save_diagnostic_frames(event, ts):
            logger.warning(
                "[Detector] Diagnostic frames unavailable — recording proceeds"
            )

        # Snapshot signal trace
        event.signal_trace = list(self._signal_trace)

        # Auto-record — pass a signal snapshot so the sidecar contains the
        # exact per-frame data used by the live detector (no replay needed).
        if self.record_on_detect:
            _primed = self._primed_events.get(predicted_flight_id or "", {})
            _sig_snapshot = {
                "scores_a": list(self._scores_a),
                "scores_b": list(self._scores_b),
                "thresh_a": float(thresh_a),
                "thresh_b": float(thresh_b),
                "triggered": list(self._triggered_buf),
                "trigger_det_frame": int(self._frame_idx),
                "disc_cx": int(self._disk_cx) if self._disk_cx is not None else None,
                "disc_cy": int(self._disk_cy) if self._disk_cy is not None else None,
                "disc_r": (
                    int(self._disk_radius) if self._disk_radius is not None else None
                ),
                "confidence_score": float(confidence_score),
                "gate_type": ("matched_filter" if mf_gate else "consec"),
                "gate_detail": (
                    f"matched_filter:{mf_duration_f}f" if mf_gate else f"consec:{_ec}f"
                ),
                "cnn_confidence": float(cnn_confidence) if cnn_confidence else None,
                # Prediction cross-link — populated when a primed event was active
                "predicted_flight_id": predicted_flight_id,
                "predicted_sep": _primed.get("sep_deg"),
                "sep_1sigma": None,  # filled by enrichment if available
            }
            rec_file = self._start_detection_recording(
                ts, signal_snapshot=_sig_snapshot
            )
            event.recording_file = rec_file

        # Store event
        self.events.append(event)  # deque(maxlen=100) discards oldest automatically

        # Enrich then log in one thread so CSV sees flight_info (map-style id/type).
        threading.Thread(
            target=self._enrich_then_log_event,
            args=(event,),
            name="detect-enrich-log",
            daemon=True,
        ).start()

        # Auto-extract CNN training clip (non-blocking)
        threading.Thread(
            target=self._save_training_clip,
            args=(ts, confidence),
            name="detect-clip",
            daemon=True,
        ).start()

        # Notify callbacks
        if self.on_detection:
            try:
                self.on_detection(event)
            except Exception as e:
                logger.error(f"[Detector] on_detection callback error: {e}")

        self._emit_status("transit_detected")

    def _save_training_clip(self, ts: datetime, confidence: str) -> None:
        """Save the current 15-frame CNN buffer as a training .npz clip.

        Clips land in data/training/unlabeled/ and get moved to positives/
        or negatives/ when the user labels them in the gallery.
        """
        try:
            if len(self._cnn_buf) < 15:
                return
            clip = np.stack(list(self._cnn_buf)[-15:], axis=0)  # (15, H, W) uint8
            # Resize to CNN training dimensions (160×90)
            resized = np.stack(
                [cv2.resize(f, (90, 160), interpolation=cv2.INTER_AREA) for f in clip],
                axis=0,
            )
            out_dir = os.path.join("data", "training", "unlabeled")
            os.makedirs(out_dir, exist_ok=True)
            fname = f"det_{ts.strftime('%Y%m%d_%H%M%S')}_{confidence}.npz"
            np.savez_compressed(os.path.join(out_dir, fname), clip=resized)
            logger.info(f"[Detector] Training clip saved: {fname}")
        except Exception as exc:
            logger.debug(f"[Detector] Training clip save failed: {exc}")

    def _save_diagnostic_frames(self, event: DetectionEvent, ts: datetime) -> bool:
        """Save trigger frame and diff heatmap as diagnostic JPGs.

        The detection runs at 160×90 for speed.  Diagnostic frames are
        upscaled 4× before saving so they are legible in the gallery,
        and the diff heatmap is labelled to avoid confusion with real
        camera images.  Both include the detector frame index so the user
        can locate the transit in the corresponding MP4.

        Returns True if at least the trigger frame was saved successfully.
        """
        if self._current_frame is None:
            logger.warning("[Detector] No current frame available for diagnostics")
            return False
        try:
            year_month = os.path.join(self.capture_dir, str(ts.year), f"{ts.month:02d}")
            os.makedirs(year_month, exist_ok=True)
            base = f"det_{ts.strftime('%Y%m%d_%H%M%S')}"
            UPSCALE = 4  # 160×90 → 640×360

            # The recording starts ~now, so the transit is near the start.
            # CONSEC_FRAMES_REQUIRED frames ago is when the trigger sequence
            # began, which at ~30fps recording ≈ first few seconds.
            frame_label = f"det frame #{self._frame_idx}"

            # Raw trigger frame (upscaled + labelled with frame number)
            frame_file = os.path.join(year_month, f"{base}_frame.jpg")
            rgb = np.clip(self._current_frame, 0, 255).astype(np.uint8)
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            bgr = cv2.resize(
                bgr,
                (ANALYSIS_WIDTH * UPSCALE, ANALYSIS_HEIGHT * UPSCALE),
                interpolation=cv2.INTER_NEAREST,
            )
            # Annotate with frame number and timestamp
            cv2.putText(
                bgr,
                frame_label,
                (8, 24),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 255),
                1,
            )
            cv2.putText(
                bgr,
                ts.strftime("%H:%M:%S"),
                (8, 48),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (200, 200, 200),
                1,
            )
            cv2.imwrite(frame_file, bgr)
            event.frame_path = frame_file

            # Diff heatmap (upscaled + labelled)
            if self._current_diff_b is not None:
                diff_file = os.path.join(year_month, f"{base}_diff.jpg")
                diff_gray = np.abs(self._current_diff_b).mean(axis=2)
                mx = diff_gray.max()
                if mx > 0:
                    diff_norm = np.clip(diff_gray / mx * 255, 0, 255).astype(np.uint8)
                else:
                    diff_norm = np.zeros_like(diff_gray, dtype=np.uint8)
                heatmap = cv2.applyColorMap(diff_norm, cv2.COLORMAP_JET)
                heatmap = cv2.resize(
                    heatmap,
                    (ANALYSIS_WIDTH * UPSCALE, ANALYSIS_HEIGHT * UPSCALE),
                    interpolation=cv2.INTER_NEAREST,
                )
                # Label so it's obvious this is a diff heatmap, not a camera image
                cv2.putText(
                    heatmap,
                    "DIFF HEATMAP",
                    (8, 24),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (255, 255, 255),
                    2,
                )
                cv2.putText(
                    heatmap,
                    f"{ts.strftime('%H:%M:%S')}  {frame_label}",
                    (8, 50),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (200, 200, 200),
                    1,
                )
                # Draw detected disk outline and margin if available
                if self._disk_detected and self._disk_radius:
                    dcx = self._disk_cx * UPSCALE
                    dcy = self._disk_cy * UPSCALE
                    dr = self._disk_radius * UPSCALE
                    inner_r = max(1, int(dr * (1.0 - self.disk_margin_pct)))
                    # Full disk outline (yellow)
                    cv2.circle(heatmap, (dcx, dcy), dr, (0, 255, 255), 1)
                    # Inner margin boundary (green) — only inside this counts
                    cv2.circle(heatmap, (dcx, dcy), inner_r, (0, 255, 0), 1)
                    cv2.putText(
                        heatmap,
                        f"disk r={self._disk_radius}px margin={self.disk_margin_pct*100:.0f}%",
                        (8, 74),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.45,
                        (0, 255, 0),
                        1,
                    )
                else:
                    cv2.putText(
                        heatmap,
                        "no disk (rect fallback)",
                        (8, 74),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.45,
                        (100, 100, 255),
                        1,
                    )
                cv2.imwrite(diff_file, heatmap)
                event.diff_path = diff_file

            logger.info(f"[Detector] Diagnostic frames saved: {base}")
            return True
        except Exception as e:
            logger.error(f"[Detector] Diagnostic frame save failed: {e}")
            return False

    def _start_detection_recording(
        self, ts: datetime, signal_snapshot: Optional[dict] = None
    ) -> Optional[str]:
        """Save pre-buffer frames + capture post-buffer from the unified circular buffer.

        The single reader loop fills _frame_buffer with JPEG frames at 30fps.
        On detection we:
          1. Snapshot the current buffer (PRE_BUFFER_SECONDS of video)
          2. Continue capturing POST_BUFFER_SECONDS more frames
          3. Write everything to an MP4 via ffmpeg
        """
        if self._rec_process is not None:
            logger.info("[Detector] Recording already active, skipping")
            return self._rec_file

        self._rec_process = True  # type: ignore[assignment]

        year_month = os.path.join(self.capture_dir, str(ts.year), f"{ts.month:02d}")
        os.makedirs(year_month, exist_ok=True)

        filename = f"det_{ts.strftime('%Y%m%d_%H%M%S')}.mp4"
        filepath = os.path.join(year_month, filename)
        self._rec_file = filepath

        _all_pre = list(self._frame_buffer)
        pre_frames = _all_pre  # use full 3-second circular buffer
        pre_count = len(pre_frames)

        logger.info(
            f"[Detector] Recording: {pre_count} tight-pre frames + "
            f"{POST_BUFFER_SECONDS}s post-buffer → {filepath}"
        )

        _captured_signal_snapshot = signal_snapshot

        def _capture_and_write():
            try:
                post_frames = []
                target_post = int(POST_BUFFER_SECONDS * ANALYSIS_FPS)
                deadline = time.monotonic() + POST_BUFFER_SECONDS + 2
                snapshot_count = self._frame_buffer_total

                while len(post_frames) < target_post and time.monotonic() < deadline:
                    new_total = self._frame_buffer_total
                    arrived = new_total - snapshot_count - len(post_frames)
                    if arrived > 0:
                        tail = list(self._frame_buffer)
                        post_frames.extend(tail[-arrived:])
                    time.sleep(0.03)

                all_frames = pre_frames + post_frames
                if not all_frames:
                    logger.warning("[Detector] No frames in buffer for recording")
                    return

                fps = float(ANALYSIS_FPS)

                # Stabilize before encoding so the solar/lunar disk stays
                # locked in place despite atmospheric distortion or mount drift.
                if RECORDING_STABILIZE:
                    try:
                        all_frames = _stabilize_frames(all_frames)
                        logger.info(
                            f"[Detector] Stabilization applied to {len(all_frames)} frames"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[Detector] Stabilization failed, using raw frames: {e}"
                        )

                written = len(all_frames)

                # Pipe JPEG bytes as MJPEG directly into ffmpeg → H.264 MP4.
                # This bypasses cv2.VideoWriter entirely, which on macOS uses
                # AVFoundation and deadlocks when writer.release() is called
                # from a background thread ("waiting to write video data").
                # ffmpeg has no such threading restriction.
                ffmpeg_cmd = [
                    FFMPEG,
                    "-y",
                    "-f",
                    "mjpeg",
                    "-r",
                    str(fps),
                    "-i",
                    "pipe:0",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "23",
                    "-bf",
                    "0",
                    "-g",
                    "30",
                    "-pix_fmt",
                    "yuv420p",
                    "-video_track_timescale",
                    "30000",
                    "-movflags",
                    "+faststart",
                    filepath,
                ]
                proc = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                for jpeg_bytes in all_frames:
                    try:
                        proc.stdin.write(jpeg_bytes)
                    except BrokenPipeError:
                        break
                proc.stdin.close()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    logger.error("[Detector] ffmpeg encode timed out")
                    return

                # Log ffmpeg stderr output
                ffmpeg_stderr = proc.stderr.read() if proc.stderr else b""
                if ffmpeg_stderr:
                    stderr_tail = ffmpeg_stderr.decode(errors="replace")[-500:]
                    if proc.returncode == 0:
                        logger.debug(f"[Detector] ffmpeg stderr (tail): {stderr_tail}")
                    else:
                        logger.error(f"[Detector] ffmpeg stderr (tail): {stderr_tail}")

                if proc.returncode != 0:
                    logger.error(
                        f"[Detector] ffmpeg encode failed (rc={proc.returncode})"
                    )
                    return

                if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                    logger.error(
                        f"[Detector] Recording missing after encode: {filepath}"
                    )
                    return

                logger.info(
                    f"[Detector] Recording saved: {written} frames "
                    f"({pre_count} pre + {written - pre_count} post) "
                    f"= {written/fps:.1f}s → {filepath}"
                )

                self._finalize_recording(
                    filepath, ts, 0, signal_snapshot=_captured_signal_snapshot
                )

            except Exception as e:
                logger.error(f"[Detector] Buffer recording failed: {e}")
            finally:
                self._rec_process = None

        threading.Thread(
            target=_capture_and_write, name="detect-buffer-write", daemon=True
        ).start()

        return filepath

    def _trim_recording(self, filepath: str, min_luma: float = 15.0) -> None:
        """Trim dark / blank leading frames from a freshly encoded det_*.mp4.

        Uses ffprobe to read the mean luminance of each frame.  Walks forward
        until a frame exceeds *min_luma* (default 15/255 — anything brighter
        than near-black counts as the disc appearing).  Re-encodes from that
        point if the trim saves at least 0.5 s.  Falls back silently on any
        error so the original file is always preserved.
        """
        try:
            r = subprocess.run(
                [
                    FFMPEG,
                    "-i",
                    filepath,
                    "-vf",
                    "signalstats",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            # signalstats writes YAVG (mean Y luma) per frame to stderr
            import re as _re

            first_good: Optional[float] = None
            for line in r.stderr.splitlines():
                m_t = _re.search(r"pts_time:([\d.]+)", line)
                m_y = _re.search(r"YAVG:([\d.]+)", line)
                if m_t:
                    cur_t = float(m_t.group(1))
                if m_y and float(m_y.group(1)) >= min_luma:
                    first_good = cur_t
                    break

            if first_good is None or first_good < 0.5:
                return  # nothing to trim

            logger.info(
                "[Detector] Trimming %s dark leading seconds from %s",
                f"{first_good:.2f}",
                os.path.basename(filepath),
            )
            tmp = filepath + ".tmp.mp4"
            subprocess.run(
                [
                    FFMPEG,
                    "-y",
                    "-ss",
                    str(first_good),
                    "-i",
                    filepath,
                    "-c",
                    "copy",
                    tmp,
                ],
                capture_output=True,
                timeout=30,
            )
            if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, filepath)
            else:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except Exception as exc:
            logger.debug("[Detector] _trim_recording skipped: %s", exc)

    def _peak_scene_time(
        self, filepath: str, search_secs: float = 3.0
    ) -> Optional[float]:
        """Return the timestamp (seconds) of the frame with the highest scene-change
        score in the first *search_secs* of *filepath*.

        Uses ffmpeg's ``scdet`` filter together with ``showinfo``.  The frame with the
        maximum score is where the aircraft silhouette produces the biggest pixel
        difference against the disc — i.e. the centre of the transit.

        Returns ``None`` on any failure so the caller can fall back to the first frame.
        """
        import re

        try:
            r = subprocess.run(
                [
                    FFMPEG,
                    "-i",
                    filepath,
                    "-t",
                    str(search_secs),
                    "-vf",
                    "scdet=threshold=2,showinfo",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            # ffmpeg writes filter output to stderr
            output = r.stderr

            best_time: Optional[float] = None
            best_score = 0.0
            current_time = 0.0

            for line in output.splitlines():
                m_t = re.search(r"pts_time:([\d.]+)", line)
                if m_t:
                    current_time = float(m_t.group(1))
                m_s = re.search(r"lavfi\.scd\.score:\s*([\d.]+)", line)
                if m_s:
                    score = float(m_s.group(1))
                    if score > best_score:
                        best_score = score
                        best_time = current_time

            return best_time
        except Exception:
            return None

    def _finalize_recording(
        self,
        filepath: str,
        ts: datetime,
        duration: int,
        signal_snapshot: Optional[dict] = None,
    ) -> None:
        """Generate thumbnail + metadata after buffer recording is written."""
        if not os.path.exists(filepath):
            logger.warning(f"[Detector] Recording file missing: {filepath}")
            return

        # Generate thumbnail at the frame where the aircraft is most visible
        # (peak scene-change score = maximum pixel difference = aircraft at disc centre).
        # Falls back to first frame if the scene-change scan fails.
        thumb_path = filepath.rsplit(".", 1)[0] + "_thumb.jpg"
        seek_time: Optional[float] = None
        try:
            seek_time = self._peak_scene_time(filepath)
            seek_args = ["-ss", str(seek_time)] if seek_time is not None else []
            if seek_time is not None:
                logger.info(
                    f"[Detector] Thumbnail seek to peak-scene frame at {seek_time:.2f}s"
                )
            subprocess.run(
                [
                    FFMPEG,
                    *seek_args,
                    "-i",
                    filepath,
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    "-q:v",
                    "5",
                    "-y",
                    thumb_path,
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"[Detector] Thumbnail failed: {e}")

        # Save metadata sidecar — includes the live-detector signal data so the
        # viewer can render signal charts and transit marks without any replay.
        meta_path = filepath.rsplit(".", 1)[0] + ".json"
        meta: dict = {
            "timestamp": ts.isoformat(),
            "duration": duration,
            "source": "transit_detection",
            "type": "video",
            "detection": True,
            "peak_time_s": round(seek_time, 3) if seek_time is not None else None,
        }
        if signal_snapshot:
            # Compute per-frame adaptive thresholds relative to the snapshot.
            # The snapshot contains the rolling history at the moment of trigger.
            # We expose the last N scores plus the threshold that was active.
            sa_list = signal_snapshot.get("scores_a", [])
            sb_list = signal_snapshot.get("scores_b", [])
            ta = signal_snapshot.get("thresh_a", 0.0)
            tb = signal_snapshot.get("thresh_b", 0.0)
            triggered = signal_snapshot.get("triggered", [])
            # Build uniform thresh arrays matching score length for the viewer
            meta["signal"] = {
                "scores_a": [round(v, 5) for v in sa_list],
                "scores_b": [round(v, 5) for v in sb_list],
                "thresh_a": round(ta, 5),
                "thresh_b": round(tb, 5),
                "triggered": list(triggered),
                "trigger_det_frame": signal_snapshot.get("trigger_det_frame"),
                "disc_cx": signal_snapshot.get("disc_cx"),
                "disc_cy": signal_snapshot.get("disc_cy"),
                "disc_r": signal_snapshot.get("disc_r"),
                "confidence_score": signal_snapshot.get("confidence_score"),
                "gate_type": signal_snapshot.get("gate_type"),
                "gate_detail": signal_snapshot.get("gate_detail"),
                "cnn_confidence": signal_snapshot.get("cnn_confidence"),
                # Peak in video-frame coordinates (transit is ~1s = 30 hires-fps frames in)
                "transit_hires_frame": int(ANALYSIS_FPS * 1.0),  # 1s tight-pre
                "analysis_fps": ANALYSIS_FPS,
            }
            _pred_fid = signal_snapshot.get("predicted_flight_id")
            if _pred_fid or signal_snapshot.get("predicted_sep") is not None:
                meta["prediction"] = {
                    "flight_id": _pred_fid,
                    "predicted_sep_deg": signal_snapshot.get("predicted_sep"),
                    "sep_1sigma_deg": signal_snapshot.get("sep_1sigma"),
                }
        try:
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            logger.warning(f"[Detector] Metadata save failed: {e}")

        logger.info(f"[Detector] Recording finalized: {filepath}")

    # ------------------------------------------------------------------
    # Transit event log (T05)
    # ------------------------------------------------------------------

    def _enrich_then_log_event(self, event: "DetectionEvent") -> None:
        """Run flight enrichment, then append the event log row (avoids empty Flight column)."""
        try:
            self._enrich_event(event)
        except Exception as exc:
            logger.debug(f"[Detector] enrich-before-log: {exc}")
        self._log_event(event)

    def _log_event(self, event: "DetectionEvent") -> None:
        """Write a detection event to the daily transit_events_*.csv log."""
        try:
            from datetime import date as _date

            from src.constants import TRANSIT_EVENTS_LOGFILENAME
            from src.flight_data import log_transit_event

            date_ = _date.today().strftime("%Y%m%d")
            dest = TRANSIT_EVENTS_LOGFILENAME.format(date_=date_)

            flight = event.flight_info or {}
            pred_fid = getattr(event, "predicted_flight_id", None) or ""
            # Look up sep_deg from primed_events if still present
            pred_sep = ""
            if pred_fid and pred_fid in self._primed_events:
                pred_sep = round(self._primed_events[pred_fid].get("sep_deg", 0), 4)
            # Reconstruct notes from the event's stored state
            _n_parts = []
            if pred_fid:
                _n_parts.append("primed")
            _atype = flight.get("aircraft_type", "") or ""
            if _atype == "N/A":
                _atype = ""
            _country = (flight.get("origin_country") or "").strip()
            _det_id = normalize_aircraft_display_id(flight.get("name", "") or "")
            row = {
                "timestamp": event.timestamp.isoformat(),
                "detected_flight_id": _det_id,
                "aircraft_type": _atype,
                "origin_country": _country,
                "predicted_flight_id": normalize_aircraft_display_id(pred_fid)
                if pred_fid
                else "",
                "prediction_sep_deg": pred_sep,
                "detection_confirmed": 1 if _det_id else 0,
                "confidence": event.confidence,
                "confidence_score": getattr(event, "confidence_score", ""),  # D3
                "signal_a": round(event.signal_a, 5),
                "signal_b": round(event.signal_b, 5),
                "centre_ratio": round(event.centre_ratio, 3),
                "notes": ",".join(_n_parts),
            }
            log_transit_event(row, dest)
            logger.debug(f"[Detector] Event logged → {dest}")
        except Exception as exc:
            logger.warning(f"[Detector] Event log write failed: {exc}")

    # ------------------------------------------------------------------
    # FlightAware enrichment
    # ------------------------------------------------------------------

    # Shared enrichment cache: avoid hitting FA for every false-positive detection.
    # Stores (timestamp, parsed_flights) from the most recent bbox query.
    _enrich_cache: Optional[tuple] = None
    _enrich_cache_ttl: float = 120.0  # reuse flight list for 2 minutes

    def _enrich_event(self, event: DetectionEvent) -> None:
        """
        Identify what aircraft was overhead at detection time.

        Prefers OpenSky (free) over FlightAware (paid).  Caches the result
        for _enrich_cache_ttl seconds so bursts of false-positive detections
        don't each trigger a separate API call.
        """
        try:
            lat = float(os.getenv("OBSERVER_LATITUDE", "0"))
            lon = float(os.getenv("OBSERVER_LONGITUDE", "0"))
            if lat == 0 and lon == 0:
                return

            lat_ll = float(os.getenv("LAT_LOWER_LEFT", str(lat - 1)))
            lon_ll = float(os.getenv("LONG_LOWER_LEFT", str(lon - 1)))
            lat_ur = float(os.getenv("LAT_UPPER_RIGHT", str(lat + 1)))
            lon_ur = float(os.getenv("LONG_UPPER_RIGHT", str(lon + 1)))

            now = time.time()

            # ── Try cached flight list first ──
            if (
                TransitDetector._enrich_cache
                and (now - TransitDetector._enrich_cache[0]) < self._enrich_cache_ttl
            ):
                flights = TransitDetector._enrich_cache[1]
                logger.debug(
                    f"[Detector] enrich: reusing cached flight list "
                    f"({len(flights)} aircraft, age {now - TransitDetector._enrich_cache[0]:.0f}s)"
                )
            else:
                # ── Prefer OpenSky (free) for enrichment ──
                flights = self._fetch_flights_for_enrichment(
                    lat_ll, lon_ll, lat_ur, lon_ur
                )
                TransitDetector._enrich_cache = (now, flights)

            if flights:
                # Find the flight closest to the observer's target line-of-sight
                from src.astro import CelestialObject
                from src.constants import ASTRO_EPHEMERIS
                from src.position import geographic_to_altaz, get_my_pos

                my_pos = get_my_pos(
                    lat,
                    lon,
                    float(os.getenv("OBSERVER_ELEVATION", "0")),
                    base_ref=ASTRO_EPHEMERIS["earth"],
                )
                ref_dt = event.timestamp

                best = None
                best_sep = 999.0

                # Get current target (sun or moon) position
                for target_name in ["sun", "moon"]:
                    try:
                        obj = CelestialObject(
                            name=target_name, observer_position=my_pos
                        )
                        obj.update_position(ref_dt)
                        coords = obj.get_coordinates()
                        if coords["altitude"] < 5:
                            continue
                        target_alt = coords["altitude"]
                        target_az = coords["azimuthal"]
                    except Exception:
                        continue

                    for flight in flights:
                        try:
                            from zoneinfo import ZoneInfo

                            from tzlocal import get_localzone_name

                            tz_aware_dt = datetime.now(
                                tz=ZoneInfo(get_localzone_name())
                            )
                            f_alt, f_az = geographic_to_altaz(
                                flight["latitude"],
                                flight["longitude"],
                                flight.get("elevation", 10000),
                                ASTRO_EPHEMERIS["earth"],
                                my_pos,
                                tz_aware_dt,
                            )
                            from src.transit import angular_separation

                            sep = angular_separation(target_alt, target_az, f_alt, f_az)
                            if sep < best_sep:
                                best_sep = sep
                                best = {
                                    "name": normalize_aircraft_display_id(
                                        flight.get("name", "") or ""
                                    ),
                                    "aircraft_type": flight.get("aircraft_type", ""),
                                    "origin": flight.get("origin", ""),
                                    "destination": flight.get("destination", ""),
                                    "origin_country": flight.get("origin_country")
                                    or "",
                                    "elevation_feet": flight.get("elevation_feet", 0),
                                    "separation_deg": round(sep, 2),
                                    "target": target_name,
                                }
                        except Exception:
                            continue

                if best and best_sep < 10.0:
                    event.flight_info = best
                    logger.info(
                        f"[Detector] Enriched: {best['name']} "
                        f"({best['aircraft_type']}) sep={best_sep:.1f}°"
                    )

        except Exception as e:
            logger.warning(f"[Detector] Enrichment failed: {e}")

    @staticmethod
    def _fetch_flights_for_enrichment(
        lat_ll: float, lon_ll: float, lat_ur: float, lon_ur: float
    ) -> list:
        """Fetch aircraft list for enrichment — prefers OpenSky (free) over FA (paid).

        Enrichment strategy (T04):
          1. Use the latest cached OpenSky snapshot from any bbox (set by TransitMonitor).
             This is typically only 10–30 s old and covers the full corridor.
          2. If the snapshot is too old (>90 s) or empty, issue a fresh OpenSky bbox query.
          3. Fall back to FlightAware if OpenSky is unavailable.
        """
        # 1a) Try the pre-cached wide-corridor snapshot first (avoids a new API call)
        try:
            from src.opensky import get_latest_snapshot

            snapshot = get_latest_snapshot(max_age_s=90.0)
            if snapshot:
                flights = []
                for callsign, pos in snapshot.items():
                    if pos.get("on_ground"):
                        continue
                    lat, lon = pos.get("lat"), pos.get("lon")
                    if lat is None or lon is None:
                        continue
                    flights.append(
                        {
                            "name": normalize_aircraft_display_id(callsign),
                            "latitude": lat,
                            "longitude": lon,
                            "elevation": pos.get("altitude_m") or 10000,
                            "elevation_feet": int(
                                (pos.get("altitude_m") or 10000) / 0.3048
                            ),
                            "aircraft_type": "",
                            "origin": "",
                            "destination": "",
                            "origin_country": pos.get("origin_country") or "",
                        }
                    )
                if flights:
                    logger.debug(
                        f"[Detector] enrich: using pre-cached snapshot "
                        f"({len(flights)} aircraft)"
                    )
                    return flights
        except Exception as exc:
            logger.debug(f"[Detector] snapshot enrichment failed: {exc}")

        # 1b) Fall back to a fresh OpenSky bbox query (free, ~60s cache built-in)
        try:
            from src.opensky import fetch_opensky_positions

            os_data = fetch_opensky_positions(lat_ll, lon_ll, lat_ur, lon_ur)
            if os_data:
                flights = []
                for callsign, pos in os_data.items():
                    if pos.get("on_ground"):
                        continue
                    lat, lon = pos.get("lat"), pos.get("lon")
                    if lat is None or lon is None:
                        continue
                    flights.append(
                        {
                            "name": normalize_aircraft_display_id(callsign),
                            "latitude": lat,
                            "longitude": lon,
                            "elevation": pos.get("altitude_m", pos.get("alt", 10000))
                            or 10000,
                            "elevation_feet": int(
                                (
                                    pos.get("altitude_m", pos.get("alt", 10000))
                                    or 10000
                                )
                                / 0.3048
                            ),
                            "aircraft_type": "",
                            "origin": "",
                            "destination": "",
                            "origin_country": pos.get("origin_country") or "",
                        }
                    )
                if flights:
                    logger.info(
                        f"[Detector] enrich: got {len(flights)} aircraft from OpenSky (free)"
                    )
                    return flights
        except Exception as exc:
            logger.debug(f"[Detector] OpenSky enrichment failed: {exc}")

        # 2) Fall back to FlightAware only if OpenSky returned nothing
        try:
            from src.constants import API_URL, get_aeroapi_key
            from src.flight_data import get_flight_data, parse_fligh_data
            from src.position import AreaBoundingBox

            api_key = get_aeroapi_key()
            if not api_key:
                return []

            bbox = AreaBoundingBox(
                lat_lower_left=lat_ll,
                long_lower_left=lon_ll,
                lat_upper_right=lat_ur,
                long_upper_right=lon_ur,
            )
            raw = get_flight_data(bbox, API_URL, api_key)
            flights = [parse_fligh_data(f) for f in raw.get("flights", [])]
            logger.info(
                f"[Detector] enrich: got {len(flights)} aircraft from FA (OpenSky unavailable)"
            )
            return flights
        except Exception as exc:
            logger.warning(f"[Detector] FA enrichment fallback failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # Status emission
    # ------------------------------------------------------------------

    def _send_disc_lost_alert(self) -> None:
        """Send a Telegram alert when the disc has been lost for too long."""
        try:
            import asyncio

            from src.telegram_notify import send_telegram_simple

            asyncio.run(
                send_telegram_simple(
                    "⚠️ <b>Disc lost</b> — telescope may be mispointed or solar "
                    f"tracking is off ({self._disc_lost_frames // ANALYSIS_FPS}s without disc). "
                    "Check the live preview."
                )
            )
        except Exception as exc:
            logger.debug(f"[Detector] Disc-lost Telegram alert failed: {exc}")

    def _emit_status(self, state: str) -> None:
        """Notify status callback."""
        if self.on_status:
            try:
                self.on_status({"state": state, **self.get_status()})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_detector: Optional[TransitDetector] = None
_detector_lock = threading.Lock()


def get_detector() -> Optional[TransitDetector]:
    """Return the singleton detector (may be None if not started)."""
    return _detector


def start_detector(rtsp_url: str, **kwargs) -> TransitDetector:
    """Create and start the singleton detector."""
    global _detector
    with _detector_lock:
        if _detector and _detector.is_running:
            _detector.stop()
        _detector = TransitDetector(rtsp_url=rtsp_url, **kwargs)
        _detector.start()
    return _detector


def stop_detector() -> None:
    """Stop the singleton detector."""
    global _detector
    with _detector_lock:
        if _detector:
            _detector.stop()
            _detector = None
