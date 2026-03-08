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

# Reference frame update interval (frames)
REF_UPDATE_INTERVAL = ANALYSIS_FPS * 10  # every 10 s

# Cooldown between detections (seconds)
DETECTION_COOLDOWN = 30

# Recording duration when transit detected (seconds)
DETECTION_RECORD_DURATION = 10


def _build_centre_weight(h: int, w: int) -> np.ndarray:
    """Gaussian-ish centre weight: 1.0 at centre → 0.3 at corners."""
    cy, cx = h / 2, w / 2
    y = np.arange(h).reshape(-1, 1)
    x = np.arange(w).reshape(1, -1)
    dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / max(cx, cy)
    return np.clip(1.0 - 0.7 * dist, 0.3, 1.0).astype(np.float32)


CENTRE_WEIGHT = _build_centre_weight(ANALYSIS_HEIGHT, ANALYSIS_WIDTH)


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
    )

    def __init__(
        self,
        timestamp: datetime,
        signal_a: float,
        signal_b: float,
        threshold_a: float,
        threshold_b: float,
        frame_idx: int,
    ):
        self.timestamp = timestamp
        self.signal_a = signal_a
        self.signal_b = signal_b
        self.threshold_a = threshold_a
        self.threshold_b = threshold_b
        self.frame_idx = frame_idx
        self.recording_file: Optional[str] = None
        self.flight_info: Optional[Dict] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "signal_a": round(self.signal_a, 4),
            "signal_b": round(self.signal_b, 4),
            "threshold_a": round(self.threshold_a, 4),
            "threshold_b": round(self.threshold_b, 4),
            "frame_idx": self.frame_idx,
            "recording_file": self.recording_file,
            "flight_info": self.flight_info,
        }


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
        self._ref_countdown = 0  # frames until next ref update

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
        self._ref_countdown = 0
        self._scores_a.clear()
        self._scores_b.clear()

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
        Checks adaptive threshold and fires detection if exceeded.
        """
        # --- Signal A: consecutive-frame diff ---
        score_a = 0.0
        if self._prev_frame is not None:
            diff_a = frame - self._prev_frame
            # Subtract per-channel mean (scintillation immunity)
            mean_shift = diff_a.mean(axis=(0, 1), keepdims=True)
            diff_a -= mean_shift
            score_a = float(np.abs(diff_a).mean())

        # --- Reference frame management ---
        if self._ref_frame is None:
            self._ref_frame = frame.copy()
            self._ref_countdown = REF_UPDATE_INTERVAL
        else:
            self._ref_countdown -= 1
            if self._ref_countdown <= 0:
                self._ref_frame = frame.copy()
                self._ref_countdown = REF_UPDATE_INTERVAL

        # --- Signal B: centre-weighted reference diff ---
        diff_b = frame - self._ref_frame
        mean_shift_b = diff_b.mean(axis=(0, 1), keepdims=True)
        diff_b -= mean_shift_b
        # Apply centre weight (broadcast over channels)
        weighted = np.abs(diff_b) * CENTRE_WEIGHT[:, :, np.newaxis]
        score_b = float(weighted.mean())

        self._prev_frame = frame.copy()

        # --- Store scores ---
        self._scores_a.append(score_a)
        self._scores_b.append(score_b)

        # Need enough history for adaptive threshold
        if len(self._scores_a) < ANALYSIS_FPS * 3:  # ~3 seconds warmup
            return

        # --- Adaptive threshold ---
        thresh_a = self._adaptive_threshold(self._scores_a) * self.sensitivity_scale
        thresh_b = self._adaptive_threshold(self._scores_b) * self.sensitivity_scale

        # --- Detection check ---
        triggered = (score_a > thresh_a) or (score_b > thresh_b)

        if triggered:
            now = time.time()
            if now - self._last_detection_time >= DETECTION_COOLDOWN:
                self._last_detection_time = now
                self._fire_detection(score_a, score_b, thresh_a, thresh_b)

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
        self, score_a: float, score_b: float, thresh_a: float, thresh_b: float
    ) -> None:
        """Handle a confirmed transit detection."""
        self._detection_count += 1
        ts = datetime.now()

        event = DetectionEvent(
            timestamp=ts,
            signal_a=score_a,
            signal_b=score_b,
            threshold_a=thresh_a,
            threshold_b=thresh_b,
            frame_idx=self._frame_idx,
        )

        logger.info(
            f"[Detector] 🎯 TRANSIT DETECTED at {ts.strftime('%H:%M:%S')} "
            f"(A={score_a:.4f}/{thresh_a:.4f}, B={score_b:.4f}/{thresh_b:.4f})"
        )

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

        # Generate thumbnail
        thumb_path = filepath.rsplit(".", 1)[0] + "_thumb.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg",
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

    def _enrich_event(self, event: DetectionEvent) -> None:
        """
        Query FlightAware for flights overhead at detection time.

        Runs in a background thread so it doesn't block detection.
        """
        try:
            lat = float(os.getenv("OBSERVER_LATITUDE", "0"))
            lon = float(os.getenv("OBSERVER_LONGITUDE", "0"))
            if lat == 0 and lon == 0:
                return

            # Use the bounding box from env
            lat_ll = float(os.getenv("LAT_LOWER_LEFT", str(lat - 1)))
            lon_ll = float(os.getenv("LONG_LOWER_LEFT", str(lon - 1)))
            lat_ur = float(os.getenv("LAT_UPPER_RIGHT", str(lat + 1)))
            lon_ur = float(os.getenv("LONG_UPPER_RIGHT", str(lon + 1)))

            from src.flight_data import get_flight_data, parse_fligh_data
            from src.position import AreaBoundingBox
            from src.constants import API_URL, get_aeroapi_key

            api_key = get_aeroapi_key()
            if not api_key:
                return

            bbox = AreaBoundingBox(
                lat_lower_left=lat_ll,
                long_lower_left=lon_ll,
                lat_upper_right=lat_ur,
                long_upper_right=lon_ur,
            )
            raw = get_flight_data(bbox, API_URL, api_key)
            flights = [parse_fligh_data(f) for f in raw.get("flights", [])]

            if flights:
                # Find the flight closest to the observer's target line-of-sight
                from src.astro import CelestialObject
                from src.position import geographic_to_altaz, get_my_pos
                from src.constants import ASTRO_EPHEMERIS

                my_pos = get_my_pos(
                    lat, lon, float(os.getenv("OBSERVER_ELEVATION", "0")),
                    base_ref=ASTRO_EPHEMERIS["earth"]
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
                        target_az = coords["azimuth"]
                    except Exception:
                        continue

                    for flight in flights:
                        try:
                            f_alt, f_az = geographic_to_altaz(
                                flight["latitude"],
                                flight["longitude"],
                                flight.get("elevation", 10000),
                                lat,
                                lon,
                                float(os.getenv("OBSERVER_ELEVATION", "0")),
                            )
                            alt_diff = abs(f_alt - target_alt)
                            az_diff = abs(f_az - target_az)
                            if az_diff > 180:
                                az_diff = 360 - az_diff
                            # Cosine-weighted separation
                            import math

                            sep = math.sqrt(
                                alt_diff**2
                                + (az_diff * math.cos(math.radians(target_alt))) ** 2
                            )
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
