"""
Real-time transit detection from telescope RTSP stream.

Reads the live video feed via ffmpeg, processes frames at ~15 fps on a
160×90 canvas using the same dual-signal algorithm as the gallery scanner
(consecutive-frame diff + centre-weighted reference diff, both with
mean-subtraction for scintillation immunity).

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

# Cooldown between detections (seconds) — configurable via DETECTION_COOLDOWN env var
DETECTION_COOLDOWN = int(os.getenv("DETECTION_COOLDOWN", "30"))

# Recording duration when transit detected (seconds)
DETECTION_RECORD_DURATION = 10

# --- Phase 1 algorithm parameters ---
# Consecutive frames both signals must exceed threshold before firing.
# At 15 fps, 5 frames ≈ 333 ms — filters insects (<100 ms) while catching
# aircraft transits (0.5–2 s).  Configurable via CONSEC_FRAMES_REQUIRED env var.
CONSEC_FRAMES_REQUIRED = int(os.getenv("CONSEC_FRAMES_REQUIRED", "5"))

# EMA blending factor for reference frame (0 = never update, 1 = full replace)
EMA_ALPHA = 0.02

# Freeze reference updates for this many frames after a detection
REF_FREEZE_FRAMES = ANALYSIS_FPS * 5  # 5 seconds

# Minimum centre-to-edge signal ratio to accept detection — configurable via env
CENTRE_EDGE_RATIO_MIN = float(os.getenv("CENTRE_EDGE_RATIO_MIN", "1.5"))

# Signal trace logging (1fps = every ANALYSIS_FPS frames)
SIGNAL_TRACE_INTERVAL = ANALYSIS_FPS
SIGNAL_TRACE_SECONDS = 60
SIGNAL_TRACE_SIZE = SIGNAL_TRACE_SECONDS  # 1 entry per second


def _build_centre_weight(h: int, w: int) -> np.ndarray:
    """Gaussian-ish centre weight: 1.0 at centre → 0.3 at corners."""
    cy, cx = h / 2, w / 2
    y = np.arange(h).reshape(-1, 1)
    x = np.arange(w).reshape(1, -1)
    dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / max(cx, cy)
    return np.clip(1.0 - 0.7 * dist, 0.3, 1.0).astype(np.float32)


CENTRE_WEIGHT = _build_centre_weight(ANALYSIS_HEIGHT, ANALYSIS_WIDTH)


def _build_spatial_masks(h: int, w: int) -> tuple:
    """Build boolean masks for centre 50% and outer edge of frame."""
    centre = np.zeros((h, w), dtype=bool)
    y1, y2 = h // 4, h * 3 // 4
    x1, x2 = w // 4, w * 3 // 4
    centre[y1:y2, x1:x2] = True
    edge = ~centre
    return centre, edge


CENTRE_MASK, EDGE_MASK = _build_spatial_masks(ANALYSIS_HEIGHT, ANALYSIS_WIDTH)


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

        # Consecutive-frame confirmation counter
        self._consec_above = 0

        # Freeze reference updates after detection
        self._ref_freeze_until = 0

        # Adaptive threshold history
        self._scores_a: Deque[float] = collections.deque(maxlen=HISTORY_SIZE)
        self._scores_b: Deque[float] = collections.deque(maxlen=HISTORY_SIZE)

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

        # Active recording process (for auto-record on detection)
        self._rec_process: Optional[subprocess.Popen] = None
        self._rec_file: Optional[str] = None

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

        # Kill ffmpeg reader
        if self._process and self._process.poll() is None:
            try:
                self._process.kill()
                self._process.wait(timeout=5)
            except Exception:
                pass
        self._process = None

        # Wait for thread
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

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
            "recording_active": self._rec_process is not None
            and self._rec_process.poll() is None,
        }

    # ------------------------------------------------------------------
    # Internal: frame reading
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Main loop: launch ffmpeg, read decoded frames, process each."""
        cmd = [
            "ffmpeg",
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
    # Internal: frame processing (dual-signal detection)
    # ------------------------------------------------------------------

    def _process_frame(self, frame: np.ndarray) -> None:
        """
        Process one 160×90 RGB frame.

        Computes signal A (consecutive diff) and signal B (reference diff).
        Both use mean-subtraction for scintillation immunity.
        Requires both signals above adaptive threshold for CONSEC_FRAMES_REQUIRED
        consecutive frames, with spatial concentration check, before firing.
        """
        self._current_frame = frame

        # --- Signal A: consecutive-frame diff ---
        score_a = 0.0
        if self._prev_frame is not None:
            diff_a = frame - self._prev_frame
            mean_shift = diff_a.mean(axis=(0, 1), keepdims=True)
            diff_a -= mean_shift
            score_a = float(np.abs(diff_a).mean())

        # --- EMA reference blending (replaces hard swap) ---
        if self._ref_frame is None:
            self._ref_frame = frame.copy()
        elif self._frame_idx > self._ref_freeze_until:
            self._ref_frame = (1 - EMA_ALPHA) * self._ref_frame + EMA_ALPHA * frame

        # --- Signal B: centre-weighted reference diff ---
        diff_b = frame - self._ref_frame
        mean_shift_b = diff_b.mean(axis=(0, 1), keepdims=True)
        diff_b -= mean_shift_b
        self._current_diff_b = diff_b
        weighted = np.abs(diff_b) * CENTRE_WEIGHT[:, :, np.newaxis]
        score_b = float(weighted.mean())

        # --- Spatial concentration: centre vs edge ---
        abs_diff_gray = np.abs(diff_b).mean(axis=2)  # H×W
        centre_score = float(abs_diff_gray[CENTRE_MASK].mean())
        edge_score = float(abs_diff_gray[EDGE_MASK].mean())
        centre_ratio = centre_score / max(edge_score, 0.001)

        self._prev_frame = frame.copy()

        # --- Store scores ---
        self._scores_a.append(score_a)
        self._scores_b.append(score_b)

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
                }
            )

        # Need enough history for adaptive threshold
        if len(self._scores_a) < ANALYSIS_FPS * 3:  # ~3 seconds warmup
            return

        # --- Adaptive threshold ---
        thresh_a = self._adaptive_threshold(self._scores_a) * self.sensitivity_scale
        thresh_b = self._adaptive_threshold(self._scores_b) * self.sensitivity_scale

        # --- Consecutive-frame confirmation ---
        triggered = (
            score_a > thresh_a
            and score_b > thresh_b
            and centre_ratio >= CENTRE_EDGE_RATIO_MIN
        )

        if triggered:
            self._consec_above += 1
        else:
            self._consec_above = 0

        if self._consec_above == CONSEC_FRAMES_REQUIRED:
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
            f"consec={CONSEC_FRAMES_REQUIRED}f/{CONSEC_FRAMES_REQUIRED/ANALYSIS_FPS:.0f}ms)"
        )

        # Save diagnostic frames
        self._save_diagnostic_frames(event, ts)

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

    def _save_diagnostic_frames(self, event: DetectionEvent, ts: datetime) -> None:
        """Save trigger frame and diff heatmap as diagnostic JPGs.

        The detection runs at 160×90 for speed.  Diagnostic frames are
        upscaled 4× before saving so they are legible in the gallery,
        and the diff heatmap is labelled to avoid confusion with real
        camera images.
        """
        if self._current_frame is None:
            return
        try:
            year_month = os.path.join(self.capture_dir, str(ts.year), f"{ts.month:02d}")
            os.makedirs(year_month, exist_ok=True)
            base = f"det_{ts.strftime('%Y%m%d_%H%M%S')}"
            UPSCALE = 4  # 160×90 → 640×360

            # Raw trigger frame (upscaled)
            frame_file = os.path.join(year_month, f"{base}_frame.jpg")
            rgb = np.clip(self._current_frame, 0, 255).astype(np.uint8)
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            bgr = cv2.resize(bgr, (ANALYSIS_WIDTH * UPSCALE, ANALYSIS_HEIGHT * UPSCALE),
                             interpolation=cv2.INTER_NEAREST)
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
                cv2.putText(heatmap, ts.strftime("%H:%M:%S"), (8, 50),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
                cv2.imwrite(diff_file, heatmap)
                event.diff_path = diff_file

            logger.info(f"[Detector] Diagnostic frames saved: {base}")
        except Exception as e:
            logger.warning(f"[Detector] Diagnostic frame save failed: {e}")

    def _start_detection_recording(self, ts: datetime) -> Optional[str]:
        """Launch a full-res RTSP recording for DETECTION_RECORD_DURATION seconds."""
        # Don't start if one is already running
        if self._rec_process and self._rec_process.poll() is None:
            logger.info("[Detector] Recording already active, skipping")
            return self._rec_file

        year_month = os.path.join(self.capture_dir, str(ts.year), f"{ts.month:02d}")
        os.makedirs(year_month, exist_ok=True)

        filename = f"det_{ts.strftime('%Y%m%d_%H%M%S')}.mp4"
        filepath = os.path.join(year_month, filename)

        duration = DETECTION_RECORD_DURATION
        cmd = [
            "ffmpeg",
            "-rtsp_transport",
            "tcp",
            "-timeout",
            str((duration + 15) * 1000000),
            "-i",
            self.rtsp_url,
            "-t",
            str(duration),
            "-c",
            "copy",
            "-movflags",
            "frag_keyframe+empty_moov",
            "-y",
            filepath,
        ]

        try:
            self._rec_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            self._rec_file = filepath
            logger.info(f"[Detector] Recording started: {filepath} ({duration}s)")

            # Schedule thumbnail + metadata generation when done
            threading.Thread(
                target=self._finalize_recording,
                args=(filepath, ts, duration),
                name="detect-finalize",
                daemon=True,
            ).start()

            return filepath
        except Exception as e:
            logger.error(f"[Detector] Failed to start recording: {e}")
            return None

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
                    "ffmpeg",
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
        """Wait for recording to finish, then generate thumbnail + metadata."""
        if self._rec_process:
            try:
                self._rec_process.wait(timeout=duration + 15)
            except subprocess.TimeoutExpired:
                self._rec_process.kill()
                self._rec_process.wait()

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
                    "ffmpeg",
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
