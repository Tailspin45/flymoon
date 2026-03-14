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
import os
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Callable, Deque, Dict, List, Optional

import cv2
import numpy as np

from src import logger
from src.constants import get_ffmpeg_path

FFMPEG = get_ffmpeg_path() or "ffmpeg"

# ---------------------------------------------------------------------------
# Detection parameters
# ---------------------------------------------------------------------------
ANALYSIS_WIDTH = 160
ANALYSIS_HEIGHT = 90
ANALYSIS_FPS = 15
FRAME_BYTES = ANALYSIS_WIDTH * ANALYSIS_HEIGHT * 3  # RGB24

# Rolling window for adaptive threshold (seconds of history)
HISTORY_SECONDS = 20
HISTORY_SIZE = ANALYSIS_FPS * HISTORY_SECONDS  # ~300 frames

# Long-run background window for noise density guard (60 seconds)
BG_HISTORY_SECONDS = 60
BG_HISTORY_SIZE = ANALYSIS_FPS * BG_HISTORY_SECONDS  # ~900 frames

# Cooldown between detections (seconds) — configurable via DETECTION_COOLDOWN env var
DETECTION_COOLDOWN = int(os.getenv("DETECTION_COOLDOWN", "30"))

# Recording duration when transit detected (seconds)
DETECTION_RECORD_DURATION = 10

# Pre-buffer: seconds of video to keep BEFORE detection trigger
PRE_BUFFER_SECONDS = int(os.getenv("DETECTION_PRE_BUFFER", "5"))
# Post-buffer: seconds of video to keep AFTER detection trigger
POST_BUFFER_SECONDS = int(os.getenv("DETECTION_POST_BUFFER", "5"))

# --- Phase 1 algorithm parameters ---
# Consecutive frames both signals must exceed threshold before firing.
# At 15 fps, 7 frames ≈ 466 ms — filters insects (<100 ms) and brief
# atmospheric shimmer (typically 1–3 frames) while safely catching aircraft
# transits (0.5–2 s = 8–30 frames).  Configurable via CONSEC_FRAMES_REQUIRED.
CONSEC_FRAMES_REQUIRED = int(os.getenv("CONSEC_FRAMES_REQUIRED", "7"))

# EMA blending factor for reference frame (0 = never update, 1 = full replace)
EMA_ALPHA = 0.02

# Freeze reference updates for this many frames after a detection
REF_FREEZE_FRAMES = ANALYSIS_FPS * 5  # 5 seconds

# Minimum centre-to-edge signal ratio to accept detection.
# 1.5 was too permissive — atmospheric seeing creates disk-wide speckles that
# still pass a weak ratio.  2.5 requires the inner disk to be clearly more
# active than the limb ring, which genuine transits (dark silhouette crossing
# the bright interior) reliably produce.
CENTRE_EDGE_RATIO_MIN = float(os.getenv("CENTRE_EDGE_RATIO_MIN", "2.5"))

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
    "1", "true", "yes", "on"
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
        g = cv2.GaussianBlur(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.float32)
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
                    bgr, M, (bgr.shape[1], bgr.shape[0]),
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


# ---------------------------------------------------------------------------
# Disk detection and disk-aware masks
# ---------------------------------------------------------------------------

def _detect_disk(gray: np.ndarray) -> Optional[tuple]:
    """Find the Sun/Moon disk in a 160×90 grayscale frame.

    Returns (cx, cy, radius) or None if no disk found.
    Uses Hough circle detection with a bright-threshold fallback,
    matching the approach in transit_analyzer.py but tuned for 160×90.
    """
    h, w = gray.shape[:2]
    blurred = cv2.GaussianBlur(gray, (5, 5), 1)
    min_r = min(h, w) // 8   # ~11 px
    max_r = min(h, w) // 2   # ~45 px

    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.0,
        minDist=min(h, w) // 2,
        param1=30,
        param2=15,
        minRadius=min_r,
        maxRadius=max_r,
    )
    if circles is not None:
        c = np.round(circles[0][0]).astype(int)
        return int(c[0]), int(c[1]), int(c[2])

    # Fallback: threshold bright region + min enclosing circle
    _, bright = cv2.threshold(blurred, 180, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        largest = max(contours, key=cv2.contourArea)
        if cv2.contourArea(largest) > np.pi * min_r * min_r:
            (cx, cy), radius = cv2.minEnclosingCircle(largest)
            return int(cx), int(cy), int(radius)

    return None


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
        "centre_ratio",
        "frame_path",
        "diff_path",
        "signal_trace",
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
        centre_ratio: float = 0.0,
    ):
        self.timestamp = timestamp
        self.signal_a = signal_a
        self.signal_b = signal_b
        self.threshold_a = threshold_a
        self.threshold_b = threshold_b
        self.frame_idx = frame_idx
        self.confidence = confidence
        self.centre_ratio = centre_ratio
        self.recording_file: Optional[str] = None
        self.flight_info: Optional[Dict] = None
        self.frame_path: Optional[str] = None
        self.diff_path: Optional[str] = None
        self.signal_trace: Optional[List[Dict]] = None

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
            "centre_ratio": round(self.centre_ratio, 2),
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
        self._track_centroid_prev: Optional[tuple] = None   # (cx, cy) last frame
        self._track_displacement_prev: Optional[tuple] = None  # (dx, dy) last frame
        self._track_agree_count: int = 0   # frames in current streak with positive dot

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

        # Event log (last N events)
        self.events: List[DetectionEvent] = []
        self._max_events = 100

        # Signal trace ring buffer (1fps, last 60s)
        self._signal_trace: Deque[Dict] = collections.deque(maxlen=SIGNAL_TRACE_SIZE)

        # Disk detection state
        self._disk_cx: Optional[int] = None
        self._disk_cy: Optional[int] = None
        self._disk_radius: Optional[int] = None
        self._disk_mask: Optional[np.ndarray] = None   # bool H×W — inner disk
        self._limb_mask: Optional[np.ndarray] = None   # bool H×W — excluded limb ring
        self._disk_weight: Optional[np.ndarray] = None  # float32 H×W — smooth weight
        self._disk_detected = False

        # Active recording process (for auto-record on detection)
        self._rec_process: Optional[subprocess.Popen] = None
        self._rec_file: Optional[str] = None

        # Full-res circular buffer for pre-trigger capture
        self._hires_buffer: Deque[bytes] = collections.deque(
            maxlen=PRE_BUFFER_SECONDS * 30  # ~30fps full-res, JPEG-compressed
        )
        self._hires_fps: float = 30.0
        self._hires_width: int = 0
        self._hires_height: int = 0
        self._hires_process: Optional[subprocess.Popen] = None
        self._hires_thread: Optional[threading.Thread] = None

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

        self._thread = threading.Thread(
            target=self._reader_loop, name="transit-detector", daemon=True
        )
        self._thread.start()

        # Start full-res circular buffer reader alongside detection
        if self.record_on_detect:
            self._hires_buffer.clear()
            self._hires_thread = threading.Thread(
                target=self._hires_reader_loop, name="hires-buffer", daemon=True
            )
            self._hires_thread.start()

        logger.info(f"[Detector] Started — reading {self.rtsp_url}")
        self._emit_status("running")
        return True

    def stop(self) -> None:
        """Stop the detection loop and clean up."""
        self._running = False

        # Kill ffmpeg reader (low-res detection)
        if self._process and self._process.poll() is None:
            try:
                self._process.kill()
                self._process.wait(timeout=5)
            except Exception:
                pass
        self._process = None

        # Kill hi-res buffer reader
        if self._hires_process and self._hires_process.poll() is None:
            try:
                self._hires_process.kill()
                self._hires_process.wait(timeout=5)
            except Exception:
                pass
        self._hires_process = None

        # Wait for threads
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        if self._hires_thread and self._hires_thread.is_alive():
            self._hires_thread.join(timeout=5)
        self._hires_thread = None

        logger.info("[Detector] Stopped")
        self._emit_status("stopped")

    def get_status(self) -> Dict[str, Any]:
        """Return current detector status."""
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
            "recent_events": [e.to_dict() for e in self.events[-10:]],
            "recording_active": self._rec_process is not None,
            "disk_detected": self._disk_detected,
            "disk_info": {
                "cx": self._disk_cx,
                "cy": self._disk_cy,
                "radius": self._disk_radius,
                "margin_pct": self.disk_margin_pct,
            }
            if self._disk_detected
            else None,
            "settings": {
                "disk_margin_pct": self.disk_margin_pct,
                "centre_ratio_min": self.centre_ratio_min,
                "consec_frames": self.consec_frames_required,
                "sensitivity_scale": self.sensitivity_scale,
                "track_min_mag": self.track_min_mag,
                "track_min_agree_frac": self.track_min_agree_frac,
            },
        }

    def update_settings(
        self,
        disk_margin_pct: Optional[float] = None,
        centre_ratio_min: Optional[float] = None,
        consec_frames: Optional[int] = None,
        sensitivity_scale: Optional[float] = None,
        track_min_mag: Optional[float] = None,
        track_min_agree_frac: Optional[float] = None,
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
        logger.info(
            f"[Detector] Settings updated: margin={self.disk_margin_pct:.0%} "
            f"ratio_min={self.centre_ratio_min} consec={self.consec_frames_required} "
            f"sens={self.sensitivity_scale:.2f} "
            f"track_mag={self.track_min_mag} track_agree={self.track_min_agree_frac:.0%}"
        )
        return self.get_status()["settings"]

    # ------------------------------------------------------------------
    # Internal: frame reading
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Main loop: launch ffmpeg, read decoded frames, process each."""
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
            "-an",  # no audio
            "pipe:1",
        ]

        reconnect_delay = 2
        max_reconnect_delay = 30

        while self._running:
            try:
                logger.info(f"[Detector] Launching ffmpeg: {' '.join(cmd)}")
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=FRAME_BYTES * 4,
                )

                reconnect_delay = 2  # reset on successful connect
                # Reset per-stream state so stale counter/frame from the
                # previous session cannot immediately trigger a detection.
                self._prev_frame = None
                self._consec_above = 0
                self._track_centroid_prev = None
                self._track_displacement_prev = None
                self._track_agree_count = 0

                while self._running:
                    raw = self._process.stdout.read(FRAME_BYTES)
                    if len(raw) < FRAME_BYTES:
                        # Stream ended or broken
                        break

                    frame = (
                        np.frombuffer(raw, dtype=np.uint8)
                        .reshape((ANALYSIS_HEIGHT, ANALYSIS_WIDTH, 3))
                        .astype(np.float32)
                    )

                    self._total_frames += 1
                    self._frame_idx += 1
                    self._process_frame(frame)

            except Exception as e:
                logger.error(f"[Detector] Frame reader error: {e}")

            # Clean up process
            if self._process and self._process.poll() is None:
                try:
                    self._process.kill()
                    self._process.wait(timeout=3)
                except Exception:
                    pass

            if not self._running:
                break

            # Reconnect with backoff
            logger.warning(
                f"[Detector] Stream lost — reconnecting in {reconnect_delay}s"
            )
            self._emit_status("reconnecting")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

        logger.info("[Detector] Reader loop exited")

    # ------------------------------------------------------------------
    # Internal: full-resolution circular buffer
    # ------------------------------------------------------------------

    def _hires_reader_loop(self) -> None:
        """Continuously read full-res MJPEG frames into a circular buffer.

        Runs alongside the low-res detection reader. Each frame is stored
        as JPEG bytes in a bounded deque so the last PRE_BUFFER_SECONDS of
        video are always available when a detection fires.
        """
        cmd = [
            FFMPEG,
            "-rtsp_transport", "tcp",
            "-timeout", "10000000",
            "-i", self.rtsp_url,
            "-f", "mjpeg",
            "-q:v", "3",       # high quality JPEG
            "-r", "30",        # 30 fps
            "-an",
            "pipe:1",
        ]

        reconnect_delay = 2

        while self._running:
            try:
                logger.info("[HiRes] Starting full-res buffer reader")
                self._hires_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=1024 * 1024,
                )

                reconnect_delay = 2
                buf = b""
                SOI = b"\xff\xd8"
                EOI = b"\xff\xd9"
                got_dimensions = False

                while self._running:
                    chunk = self._hires_process.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk

                    # Extract complete JPEG frames from the MJPEG stream
                    while True:
                        soi_pos = buf.find(SOI)
                        if soi_pos < 0:
                            buf = b""
                            break
                        eoi_pos = buf.find(EOI, soi_pos + 2)
                        if eoi_pos < 0:
                            # Trim before SOI marker to avoid unbounded growth
                            buf = buf[soi_pos:]
                            break
                        jpeg_data = buf[soi_pos:eoi_pos + 2]
                        buf = buf[eoi_pos + 2:]

                        self._hires_buffer.append(jpeg_data)

                        # Get dimensions from first frame
                        if not got_dimensions:
                            try:
                                arr = np.frombuffer(jpeg_data, dtype=np.uint8)
                                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                                if img is not None:
                                    self._hires_height, self._hires_width = img.shape[:2]
                                    got_dimensions = True
                                    logger.info(
                                        f"[HiRes] Buffer active: {self._hires_width}×{self._hires_height} "
                                        f"capacity={self._hires_buffer.maxlen} frames "
                                        f"({PRE_BUFFER_SECONDS}s)"
                                    )
                            except Exception:
                                pass

            except Exception as e:
                logger.error(f"[HiRes] Buffer reader error: {e}")

            if self._hires_process and self._hires_process.poll() is None:
                try:
                    self._hires_process.kill()
                    self._hires_process.wait(timeout=3)
                except Exception:
                    pass

            if not self._running:
                break

            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30)

        logger.info("[HiRes] Buffer reader exited")

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

        # --- Periodic disk detection (every 2s), or immediate rebuild if mask cleared ---
        rebuild_needed = self._disk_mask is None and self._disk_cx is not None
        if self._frame_idx % DISK_DETECT_INTERVAL == 0 or rebuild_needed:
            gray = np.clip(frame.mean(axis=2), 0, 255).astype(np.uint8)
            result = _detect_disk(gray)
            if result is not None:
                cx, cy, r = result
                if not self._disk_detected:
                    logger.info(
                        f"[Detector] Disk found: centre=({cx},{cy}), "
                        f"radius={r}px, margin={self.disk_margin_pct*100:.0f}%"
                    )
                self._disk_cx, self._disk_cy, self._disk_radius = cx, cy, r
                self._disk_mask, self._limb_mask, self._disk_weight = (
                    _build_disk_masks(
                        ANALYSIS_HEIGHT, ANALYSIS_WIDTH, cx, cy, r, self.disk_margin_pct
                    )
                )
                self._disk_detected = True
            elif self._disk_detected:
                logger.debug("[Detector] Disk lost — falling back to rectangular masks")
                self._disk_detected = False

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
        score_b = float(weighted.mean())

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

        self._track_centroid_prev = (track_cx, track_cy) if track_cx is not None else None

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
            recent = list(self._scores_a)[-ANALYSIS_FPS * 3:]
            recent_median = float(np.median(recent)) if recent else 0.0
            noise_factor = max(1.0, (recent_median / max(bg_median, 1e-6)) * 0.5)
        else:
            noise_factor = 1.0
        thresh_a *= noise_factor
        thresh_b *= noise_factor

        # --- Centre ratio gate ---
        # When no disk has been detected the rectangular "edge" mask covers
        # the black corners of the frame (off-disk), making outer_score ≈ 0
        # and centre_ratio artificially enormous.  Disable detection entirely
        # until the disk is found — a transit cannot occur without a target.
        if not self._disk_detected:
            self._consec_above = 0
            return
        ratio_ok = centre_ratio >= self.centre_ratio_min

        # --- Consecutive-frame confirmation ---
        triggered = score_a > thresh_a and score_b > thresh_b and ratio_ok

        if triggered:
            self._consec_above += 1
        else:
            self._consec_above = 0
            self._track_agree_count = 0  # reset track on any threshold break

        if self._consec_above >= self.consec_frames_required:
            # --- Centroid track gate ---
            # Require that a minimum fraction of streak frames showed consistent
            # directional motion.  Noise produces _track_agree_count ~ 0 even
            # when both signals are spuriously elevated; real transits produce
            # monotonically moving centroids so agree_count climbs reliably.
            min_agree = int(self.consec_frames_required * self.track_min_agree_frac)
            track_ok = self._track_agree_count >= min_agree

            self._consec_above = 0       # reset so next event starts clean
            self._track_agree_count = 0

            if not track_ok:
                logger.debug(
                    f"[Detector] Track gate suppressed: agree={self._track_agree_count} "
                    f"need={min_agree}/{self.consec_frames_required}"
                )
            else:
                now = time.time()
                if now - self._last_detection_time >= DETECTION_COOLDOWN:
                    self._last_detection_time = now
                    # Freeze reference to prevent transit frames corrupting baseline
                    self._ref_freeze_until = self._frame_idx + REF_FREEZE_FRAMES
                    self._fire_detection(score_a, score_b, thresh_a, thresh_b, centre_ratio)

    @staticmethod
    def _adaptive_threshold(scores: Deque[float]) -> float:
        """
        Compute adaptive threshold: median + max(3×MAD, 0.5×median).

        Same formula as the JS gallery scanner.
        """
        arr = np.array(scores)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med)))
        return med + max(3.0 * mad, 0.5 * med)

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
    ) -> None:
        """Handle a confirmed transit detection."""
        self._detection_count += 1
        ts = datetime.now()

        # Confidence based on signal-to-threshold ratio
        sig_ratio = min(score_a / max(thresh_a, 0.001), score_b / max(thresh_b, 0.001))
        confidence = "strong" if sig_ratio > 2.0 else "weak"

        event = DetectionEvent(
            timestamp=ts,
            signal_a=score_a,
            signal_b=score_b,
            threshold_a=thresh_a,
            threshold_b=thresh_b,
            frame_idx=self._frame_idx,
            confidence=confidence,
            centre_ratio=centre_ratio,
        )

        logger.info(
            f"[Detector] 🎯 TRANSIT DETECTED at {ts.strftime('%H:%M:%S.%f')[:-3]} "
            f"[{confidence}] "
            f"(A={score_a:.4f}/{thresh_a:.4f}, B={score_b:.4f}/{thresh_b:.4f}, "
            f"CR={centre_ratio:.2f}, "
            f"consec={self.consec_frames_required}f/{self.consec_frames_required/ANALYSIS_FPS:.0f}ms)"
        )

        # Save diagnostic frames — discard event if we can't produce evidence
        if not self._save_diagnostic_frames(event, ts):
            logger.warning(
                "[Detector] Detection suppressed — no diagnostic frames could be saved"
            )
            return

        # Snapshot signal trace
        event.signal_trace = list(self._signal_trace)

        # Auto-record
        if self.record_on_detect:
            rec_file = self._start_detection_recording(ts)
            event.recording_file = rec_file

        # Store event
        self.events.append(event)
        if len(self.events) > self._max_events:
            self.events = self.events[-self._max_events :]

        # Enrich with flight data (async, non-blocking)
        threading.Thread(
            target=self._enrich_event, args=(event,), name="detect-enrich", daemon=True
        ).start()

        # Notify callbacks
        if self.on_detection:
            try:
                self.on_detection(event)
            except Exception as e:
                logger.error(f"[Detector] on_detection callback error: {e}")

        self._emit_status("transit_detected")

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
            bgr = cv2.resize(bgr, (ANALYSIS_WIDTH * UPSCALE, ANALYSIS_HEIGHT * UPSCALE),
                             interpolation=cv2.INTER_NEAREST)
            # Annotate with frame number and timestamp
            cv2.putText(bgr, frame_label, (8, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
            cv2.putText(bgr, ts.strftime("%H:%M:%S"), (8, 48),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
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
                heatmap = cv2.resize(heatmap, (ANALYSIS_WIDTH * UPSCALE, ANALYSIS_HEIGHT * UPSCALE),
                                     interpolation=cv2.INTER_NEAREST)
                # Label so it's obvious this is a diff heatmap, not a camera image
                cv2.putText(heatmap, "DIFF HEATMAP", (8, 24),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                cv2.putText(heatmap, f"{ts.strftime('%H:%M:%S')}  {frame_label}", (8, 50),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
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
                    cv2.putText(heatmap, f"disk r={self._disk_radius}px margin={self.disk_margin_pct*100:.0f}%",
                                (8, 74), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
                else:
                    cv2.putText(heatmap, "no disk (rect fallback)",
                                (8, 74), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (100, 100, 255), 1)
                cv2.imwrite(diff_file, heatmap)
                event.diff_path = diff_file

            logger.info(f"[Detector] Diagnostic frames saved: {base}")
            return True
        except Exception as e:
            logger.error(f"[Detector] Diagnostic frame save failed: {e}")
            return False

    def _start_detection_recording(self, ts: datetime) -> Optional[str]:
        """Save pre-buffer frames + capture post-buffer from the circular buffer.

        The hi-res reader continuously fills _hires_buffer with JPEG frames.
        On detection we:
          1. Snapshot the current buffer (PRE_BUFFER_SECONDS of video)
          2. Continue capturing POST_BUFFER_SECONDS more frames
          3. Write everything to an MP4 via cv2.VideoWriter
        This guarantees the transit frame is IN the video.
        """
        # Don't start if one is already running
        if self._rec_process is not None:
            logger.info("[Detector] Recording already active, skipping")
            return self._rec_file

        # Use a sentinel to block concurrent recordings
        self._rec_process = True  # type: ignore[assignment]

        year_month = os.path.join(self.capture_dir, str(ts.year), f"{ts.month:02d}")
        os.makedirs(year_month, exist_ok=True)

        filename = f"det_{ts.strftime('%Y%m%d_%H%M%S')}.mp4"
        filepath = os.path.join(year_month, filename)
        self._rec_file = filepath

        # Snapshot the pre-buffer
        pre_frames = list(self._hires_buffer)
        pre_count = len(pre_frames)

        logger.info(
            f"[Detector] Recording: {pre_count} pre-buffer frames + "
            f"{POST_BUFFER_SECONDS}s post-buffer → {filepath}"
        )

        # Collect post-buffer in a background thread
        def _capture_and_write():
            try:
                # Collect post-buffer frames
                post_frames = []
                target_post = int(POST_BUFFER_SECONDS * 30)
                deadline = time.monotonic() + POST_BUFFER_SECONDS + 2
                buf_snapshot_len = len(self._hires_buffer)

                while len(post_frames) < target_post and time.monotonic() < deadline:
                    current_len = len(self._hires_buffer)
                    if current_len > buf_snapshot_len:
                        # New frames arrived — grab them
                        new_frames = list(self._hires_buffer)[-( current_len - buf_snapshot_len):]
                        post_frames.extend(new_frames)
                        buf_snapshot_len = current_len
                    time.sleep(0.03)

                all_frames = pre_frames + post_frames
                if not all_frames:
                    logger.warning("[Detector] No frames in buffer for recording")
                    return

                fps = 30.0

                # Stabilize before encoding so the solar/lunar disk stays
                # locked in place despite atmospheric distortion or mount drift.
                if RECORDING_STABILIZE:
                    try:
                        all_frames = _stabilize_frames(all_frames)
                        logger.info(
                            f"[Detector] Stabilization applied to {len(all_frames)} frames"
                        )
                    except Exception as e:
                        logger.warning(f"[Detector] Stabilization failed, using raw frames: {e}")

                written = len(all_frames)

                # Pipe JPEG bytes as MJPEG directly into ffmpeg → H.264 MP4.
                # This bypasses cv2.VideoWriter entirely, which on macOS uses
                # AVFoundation and deadlocks when writer.release() is called
                # from a background thread ("waiting to write video data").
                # ffmpeg has no such threading restriction.
                ffmpeg_cmd = [
                    FFMPEG, "-y",
                    "-f", "mjpeg",
                    "-r", str(fps),
                    "-i", "pipe:0",
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "23",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    filepath,
                ]
                proc = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
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

                self._finalize_recording(filepath, ts, 0)

            except Exception as e:
                logger.error(f"[Detector] Buffer recording failed: {e}")
            finally:
                self._rec_process = None

        threading.Thread(
            target=_capture_and_write, name="detect-buffer-write", daemon=True
        ).start()

        return filepath

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

    def _finalize_recording(self, filepath: str, ts: datetime, duration: int) -> None:
        """Generate thumbnail + metadata after buffer recording is written."""
        if not os.path.exists(filepath):
            logger.warning(f"[Detector] Recording file missing: {filepath}")
            return

        # Generate thumbnail at the frame where the aircraft is most visible
        # (peak scene-change score = maximum pixel difference = aircraft at disc centre).
        # Falls back to first frame if the scene-change scan fails.
        thumb_path = filepath.rsplit(".", 1)[0] + "_thumb.jpg"
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

        # Save metadata
        meta_path = filepath.rsplit(".", 1)[0] + ".json"
        meta = {
            "timestamp": ts.isoformat(),
            "duration": duration,
            "source": "transit_detection",
            "type": "video",
            "detection": True,
        }
        try:
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            logger.warning(f"[Detector] Metadata save failed: {e}")

        logger.info(f"[Detector] Recording finalized: {filepath}")

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
                                    "name": flight.get("name", "Unknown"),
                                    "aircraft_type": flight.get("aircraft_type", ""),
                                    "origin": flight.get("origin", ""),
                                    "destination": flight.get("destination", ""),
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
        """Fetch aircraft list for enrichment — prefers OpenSky (free) over FA (paid)."""
        # 1) Try OpenSky first (free, ~60s cache built-in)
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
                            "name": callsign.strip(),
                            "latitude": lat,
                            "longitude": lon,
                            "elevation": pos.get("alt", 10000) or 10000,
                            "elevation_feet": int(
                                (pos.get("alt", 10000) or 10000) / 0.3048
                            ),
                            "aircraft_type": "",
                            "origin": "",
                            "destination": "",
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
