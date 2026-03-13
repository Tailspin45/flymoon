"""
Solar Timelapse — day-long background timelapse of the Sun.

Captures one JPEG frame at a configurable interval (default 120s) from the
telescope's RTSP stream, stores frames on disk, and assembles them into an
MP4 timelapse at sunset or on manual stop.

Each frame is annotated with detected sunspots (circled in grey) using the
same CLAHE + adaptive-threshold pipeline as the transit analyzer.

The capture loop runs in a background daemon thread and automatically
pauses/resumes when transit events (predicted or detected) need the
recording pipeline.
"""

import json
import os
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional

import cv2
import numpy as np
from tzlocal import get_localzone

from src import logger
from src.astro import CelestialObject
from src.constants import ASTRO_EPHEMERIS, get_ffmpeg_path

FFMPEG = get_ffmpeg_path() or "ffmpeg"
from src.position import get_my_pos

EARTH = ASTRO_EPHEMERIS["earth"]

# Sunspot annotation color (grey circles, matching transit_analyzer)
SPOT_COLOR = (140, 140, 140)


def _detect_disk(frame: np.ndarray):
    """Return (cx, cy, radius) of the solar disk, or None.

    Uses Hough circle detection with a contour-based fallback.
    Mirrors transit_analyzer._detect_disk().
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (9, 9), 2)
    h, w = blurred.shape
    min_r = min(h, w) // 8
    max_r = min(h, w) // 2

    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT,
        dp=1.2, minDist=min(h, w) // 2,
        param1=50, param2=30,
        minRadius=min_r, maxRadius=max_r,
    )
    if circles is not None:
        c = np.round(circles[0][0]).astype(int)
        return int(c[0]), int(c[1]), int(c[2])

    _, thresh = cv2.threshold(blurred, 200, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        largest = max(contours, key=cv2.contourArea)
        if cv2.contourArea(largest) > (min_r ** 2 * np.pi):
            (cx, cy), radius = cv2.minEnclosingCircle(largest)
            return int(cx), int(cy), int(radius)
    return None


def annotate_sunspots(frame: np.ndarray) -> np.ndarray:
    """Detect sunspots on the solar disk and draw grey circles around them.

    Uses CLAHE enhancement + adaptive thresholding + darkness verification,
    matching the pipeline in transit_analyzer._write_composite_image().

    Returns an annotated copy of the frame (original is not modified).
    """
    disk = _detect_disk(frame)
    if disk is None:
        return frame.copy()

    cx, cy, radius = disk
    canvas = frame.copy()
    h, w = canvas.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # CLAHE to enhance contrast for sunspot detection
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    blur_e = cv2.GaussianBlur(enhanced, (3, 3), 0)

    # Detect spots inside the disk (98% radius to exclude limb)
    spot_inner_r = int(radius * 0.98)
    spot_mask = np.zeros(gray.shape[:2], dtype=np.uint8)
    cv2.circle(spot_mask, (cx, cy), spot_inner_r, 255, -1)

    adapt = cv2.adaptiveThreshold(
        blur_e, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
        blockSize=51, C=6,
    )
    adapt = cv2.bitwise_and(adapt, spot_mask)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    adapt = cv2.morphologyEx(adapt, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(adapt, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Verify each candidate is darker than local on-disk mean
    blur_ref = cv2.GaussianBlur(gray, (5, 5), 0)
    verify_mask = np.zeros(gray.shape[:2], dtype=np.uint8)
    cv2.circle(verify_mask, (cx, cy), radius, 255, -1)

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
        if local_val >= local_mean - 10:
            continue  # not darker than surroundings

        _, _, sw, sh = cv2.boundingRect(cnt)
        sr = max(10, max(sw, sh) // 2 + 6)
        cv2.circle(canvas, (sx, sy), sr, SPOT_COLOR, 2)
        spot_count += 1

    # Mask outside disk to black for a clean look
    disk_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(disk_mask, (cx, cy), radius + 2, 255, -1)
    canvas[disk_mask == 0] = 0

    # Timestamp overlay
    ts = datetime.now().strftime("%H:%M:%S")
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(0.4, radius / 300)
    thick = max(1, int(scale * 2))
    tx = cx - radius + 10
    ty = cy + radius - 10
    cv2.putText(canvas, ts, (tx, ty), font, scale, (255, 255, 255), thick)

    return canvas


class SolarTimelapse:
    """Singleton manager for day-long solar timelapse capture."""

    def __init__(self):
        # Re-entrant lock is required because some methods call status()
        # while already holding the lock.
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = paused
        self._interval: float = 120.0  # seconds between frames
        self._running = False
        self._paused = False
        self._frame_count = 0
        self._start_time: Optional[datetime] = None
        self._last_capture: float = 0  # monotonic timestamp
        self._frames_dir: str = ""
        self._output_path: str = ""
        self._host: Optional[str] = None
        self._rtsp_port: int = 4554
        self._min_sun_alt: float = 0.0  # stop when sun below this
        self._stabilize_enabled: bool = True
        self._stabilize_max_shift_px: float = 25.0
        self._stabilize_smoothing: float = 0.85
        self._stabilize_ref_gray: Optional[np.ndarray] = None
        self._stabilize_offset = (0.0, 0.0)

    # ── Public API ──────────────────────────────────────────────────────

    def _today_paths(self, now: datetime):
        day_str = now.strftime("%Y%m%d")
        frames_dir = os.path.join(
            "static", "captures", str(now.year), f"{now.month:02d}", f"timelapse_{day_str}"
        )
        output_path = os.path.join(
            "static", "captures", str(now.year), f"{now.month:02d}", f"timelapse_{day_str}.mp4"
        )
        return frames_dir, output_path

    def _existing_frame_count(self, frames_dir: str) -> int:
        if not os.path.isdir(frames_dir):
            return 0
        count = 0
        for name in os.listdir(frames_dir):
            if name.startswith("frame_") and name.endswith(".jpg"):
                count += 1
        return count

    def _latest_frame_url_for_dir(self, frames_dir: str) -> Optional[str]:
        """Return latest frame URL from a timelapse directory (prefer annotated)."""
        if not frames_dir or not os.path.isdir(frames_dir):
            return None
        ann_dir = os.path.join(frames_dir, "annotated")
        if os.path.isdir(ann_dir):
            ann_frames = sorted(f for f in os.listdir(ann_dir) if f.endswith(".jpg"))
            if ann_frames:
                latest = os.path.join(ann_dir, ann_frames[-1])
                rel = os.path.relpath(latest, "static").replace(os.sep, "/")
                return f"/static/{rel}"
        frames = sorted(
            f
            for f in os.listdir(frames_dir)
            if f.startswith("frame_") and f.endswith(".jpg")
        )
        if not frames:
            return None
        latest = os.path.join(frames_dir, frames[-1])
        rel = os.path.relpath(latest, "static").replace(os.sep, "/")
        return f"/static/{rel}"

    def has_today_frames(self) -> bool:
        """True when today's timelapse folder already contains captured frames."""
        frames_dir, _ = self._today_paths(datetime.now())
        return self._existing_frame_count(frames_dir) > 0

    def start(self, host: str, interval: float = 120.0) -> dict:
        with self._lock:
            if self._running:
                return {"error": "Timelapse already running"}

            self._host = host
            self._rtsp_port = int(os.getenv("SEESTAR_RTSP_PORT", "4554"))
            self._interval = max(10.0, interval)
            self._stabilize_enabled = (
                os.getenv("SOLAR_TIMELAPSE_STABILIZE", "true").strip().lower()
                in ("1", "true", "yes", "on")
            )
            self._stabilize_max_shift_px = float(
                os.getenv("SOLAR_TIMELAPSE_STABILIZE_MAX_SHIFT", "25")
            )
            self._stabilize_smoothing = float(
                os.getenv("SOLAR_TIMELAPSE_STABILIZE_SMOOTHING", "0.85")
            )
            self._stabilize_ref_gray = None
            self._stabilize_offset = (0.0, 0.0)
            self._stop_event.clear()
            self._pause_event.clear()
            self._paused = False
            self._frame_count = 0
            self._last_capture = 0

            now = datetime.now()
            self._start_time = now
            self._frames_dir, self._output_path = self._today_paths(now)
            os.makedirs(self._frames_dir, exist_ok=True)

            self._running = True
            self._thread = threading.Thread(
                target=self._capture_loop, daemon=True, name="solar-timelapse"
            )
            self._thread.start()
            logger.info(
                f"[Timelapse] Started — interval={self._interval}s, "
                f"frames_dir={self._frames_dir}"
            )
            return self.status()

    def resume_today(self, host: str, interval: float = 120.0) -> dict:
        """Resume today's timelapse from existing frames after restart/crash."""
        with self._lock:
            if self._running:
                return self.status()

            now = datetime.now()
            frames_dir, output_path = self._today_paths(now)
            os.makedirs(frames_dir, exist_ok=True)
            existing_count = self._existing_frame_count(frames_dir)

            self._host = host
            self._rtsp_port = int(os.getenv("SEESTAR_RTSP_PORT", "4554"))
            self._interval = max(10.0, interval)
            self._stabilize_enabled = (
                os.getenv("SOLAR_TIMELAPSE_STABILIZE", "true").strip().lower()
                in ("1", "true", "yes", "on")
            )
            self._stabilize_max_shift_px = float(
                os.getenv("SOLAR_TIMELAPSE_STABILIZE_MAX_SHIFT", "25")
            )
            self._stabilize_smoothing = float(
                os.getenv("SOLAR_TIMELAPSE_STABILIZE_SMOOTHING", "0.85")
            )
            self._stabilize_ref_gray = None
            self._stabilize_offset = (0.0, 0.0)
            self._stop_event.clear()
            self._pause_event.clear()
            self._paused = False
            self._frames_dir = frames_dir
            self._output_path = output_path
            self._frame_count = existing_count
            self._last_capture = 0

            # Preserve approximate session start when resuming from existing frames.
            if existing_count > 0:
                first_frame = os.path.join(frames_dir, "frame_00001.jpg")
                if os.path.exists(first_frame):
                    self._start_time = datetime.fromtimestamp(os.path.getmtime(first_frame))
                else:
                    self._start_time = now
            else:
                self._start_time = now

            self._running = True
            self._thread = threading.Thread(
                target=self._capture_loop, daemon=True, name="solar-timelapse"
            )
            self._thread.start()
            logger.info(
                f"[Timelapse] Resumed — interval={self._interval}s, "
                f"frames_dir={self._frames_dir}, existing_frames={existing_count}"
            )
            return self.status()

    def stop(self, assemble: bool = True) -> dict:
        with self._lock:
            if not self._running:
                return {"error": "Timelapse not running"}
            self._stop_event.set()

        # Wait for thread to finish (max 15s)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=15)

        result = self.status()
        if assemble and self._frame_count > 0:
            result["assembling"] = True
            threading.Thread(
                target=self._assemble_video, daemon=True, name="timelapse-assemble"
            ).start()
        return result

    def pause(self, reason: str = "transit") -> None:
        with self._lock:
            if self._running and not self._paused:
                self._pause_event.set()
                self._paused = True
                logger.info(f"[Timelapse] Paused — reason: {reason}")

    def resume(self) -> None:
        with self._lock:
            if self._running and self._paused:
                self._pause_event.clear()
                self._paused = False
                logger.info("[Timelapse] Resumed")

    def update_interval(self, interval: float) -> dict:
        with self._lock:
            self._interval = max(10.0, interval)
            logger.info(f"[Timelapse] Interval updated to {self._interval}s")
            return self.status()

    def status(self) -> dict:
        with self._lock:
            now = datetime.now()
            today_frames_dir, today_output_path = self._today_paths(now)

            effective_frame_count = self._frame_count
            effective_frames_dir = self._frames_dir if self._running else ""
            effective_output_path = self._output_path if self._output_path else ""

            # After restart, runtime state is empty; surface today's on-disk
            # progress so UI can show accumulated timelapse data.
            if not self._running:
                disk_count = self._existing_frame_count(today_frames_dir)
                if disk_count > 0:
                    effective_frame_count = disk_count
                    effective_frames_dir = today_frames_dir
                    effective_output_path = today_output_path

            elapsed_wall_clock = 0
            if self._start_time and self._running:
                elapsed_wall_clock = (datetime.now() - self._start_time).total_seconds()

            # Capture span should reflect the timelapse sampling cadence, not
            # wall clock (which may include downtime/restarts/pauses).
            capture_span_seconds = effective_frame_count * self._interval

            next_in = 0
            if self._running and not self._paused and self._last_capture > 0:
                since_last = time.monotonic() - self._last_capture
                next_in = max(0, self._interval - since_last)

            result = {
                "running": self._running,
                "paused": self._paused,
                "interval": self._interval,
                "frame_count": effective_frame_count,
                "elapsed": round(elapsed_wall_clock),
                "capture_span_seconds": round(capture_span_seconds),
                "stabilize_enabled": self._stabilize_enabled,
                "next_capture_in": round(next_in),
                "frames_dir": effective_frames_dir or None,
                "output_path": effective_output_path or None,
                "resume_available": (not self._running) and effective_frame_count > 0,
            }

        # Filesystem access outside lock
        if result["frames_dir"]:
            result["latest_frame"] = self._latest_frame_url_for_dir(result["frames_dir"])
        else:
            result["latest_frame"] = None
        return result

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def build_preview(self) -> Optional[str]:
        """Assemble annotated frames into a preview MP4. Returns web path or None.

        Prefers annotated frames (with sunspot circles). Falls back to raw.
        """
        frames_dir = self._frames_dir
        if not frames_dir or not os.path.isdir(frames_dir):
            return None

        # Prefer annotated frames
        ann_dir = os.path.join(frames_dir, "annotated")
        if os.path.isdir(ann_dir):
            ann_frames = sorted(f for f in os.listdir(ann_dir) if f.endswith(".jpg"))
            if len(ann_frames) >= 2:
                preview_path = self._output_path.rsplit(".", 1)[0] + "_preview.mp4"
                pattern = os.path.join(ann_dir, "frame_%05d.jpg")
                return self._build_preview_mp4(pattern, preview_path, len(ann_frames))

        # Fall back to raw frames
        frames = sorted(f for f in os.listdir(frames_dir) if f.endswith(".jpg"))
        if len(frames) < 2:
            return None
        preview_path = self._output_path.rsplit(".", 1)[0] + "_preview.mp4"
        pattern = os.path.join(frames_dir, "frame_%05d.jpg")
        return self._build_preview_mp4(pattern, preview_path, len(frames))

    def _build_preview_mp4(self, pattern: str, output: str,
                           frame_count: int) -> Optional[str]:
        """Encode a preview MP4 and return its web URL."""
        fps = 10
        cmd = [
            FFMPEG,
            "-framerate", str(round(fps, 2)),
            "-i", pattern,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "23",
            "-preset", "fast",
            "-y",
            output,
        ]
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60
            )
            if result.returncode == 0 and os.path.exists(output):
                rel = os.path.relpath(output, "static").replace(os.sep, "/")
                logger.info(
                    f"[Timelapse] Preview built: {frame_count} frames → {output}"
                )
                return f"/static/{rel}"
        except Exception as e:
            logger.warning(f"[Timelapse] Preview build failed: {e}")
        return None

    def get_latest_frame_url(self) -> Optional[str]:
        """Return web URL for the most recently captured frame.

        Prefers the annotated version (with sunspot circles).
        """
        frames_dir = self._frames_dir
        if not frames_dir or not os.path.isdir(frames_dir):
            return None

        # Prefer annotated frame
        ann_dir = os.path.join(frames_dir, "annotated")
        if os.path.isdir(ann_dir):
            ann_frames = sorted(f for f in os.listdir(ann_dir) if f.endswith(".jpg"))
            if ann_frames:
                latest = os.path.join(ann_dir, ann_frames[-1])
                rel = os.path.relpath(latest, "static").replace(os.sep, "/")
                return f"/static/{rel}"

        # Fall back to raw
        frames = sorted(f for f in os.listdir(frames_dir) if f.endswith(".jpg"))
        if not frames:
            return None
        latest = os.path.join(frames_dir, frames[-1])
        rel = os.path.relpath(latest, "static").replace(os.sep, "/")
        return f"/static/{rel}"

    # ── Internal ────────────────────────────────────────────────────────

    def _capture_loop(self):
        """Background loop: grab one frame per interval until sunset or stop."""
        logger.info("[Timelapse] Capture loop started")
        try:
            while not self._stop_event.is_set():
                # Check if paused
                if self._pause_event.is_set():
                    time.sleep(1)
                    continue

                # Check sun altitude — stop if below horizon
                sun_alt = self._get_sun_altitude()
                if sun_alt is not None and sun_alt < self._min_sun_alt:
                    logger.info(
                        f"[Timelapse] Sun below horizon ({sun_alt:.1f}°) — "
                        f"stopping after {self._frame_count} frames"
                    )
                    break

                # Capture a frame
                ok = self._grab_frame()
                if ok:
                    with self._lock:
                        self._frame_count += 1
                        self._last_capture = time.monotonic()

                # Sleep in small increments so stop_event is responsive
                wait_until = time.monotonic() + self._interval
                while time.monotonic() < wait_until:
                    if self._stop_event.is_set():
                        break
                    if self._pause_event.is_set():
                        break
                    time.sleep(1)
        except Exception as e:
            logger.error(f"[Timelapse] Capture loop error: {e}", exc_info=True)
        finally:
            with self._lock:
                was_running = self._running
                self._running = False
                self._paused = False
            logger.info(
                f"[Timelapse] Capture loop ended — {self._frame_count} frames"
            )
            # Auto-assemble if we had frames and weren't manually stopped
            if was_running and self._frame_count > 0 and not self._stop_event.is_set():
                self._assemble_video()

    def _grab_frame(self) -> bool:
        """Grab a single JPEG frame from the RTSP stream via ffmpeg.

        After capture, annotates the frame with sunspot circles and a
        timestamp overlay, saving the annotated version alongside the raw.
        """
        if not self._host:
            return False

        seq = self._frame_count + 1
        filename = f"frame_{seq:05d}.jpg"
        filepath = os.path.join(self._frames_dir, filename)
        rtsp_url = f"rtsp://{self._host}:{self._rtsp_port}/stream"

        cmd = [
            FFMPEG,
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-frames:v", "1",
            "-update", "1",
            "-q:v", "2",
            "-y",
            filepath,
        ]

        try:
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15
            )
            if result.returncode != 0:
                stderr_tail = result.stderr.decode(errors="replace")[-200:]
                logger.warning(f"[Timelapse] Frame grab failed: {stderr_tail}")
                return False

            if not os.path.exists(filepath) or os.path.getsize(filepath) < 100:
                logger.warning(f"[Timelapse] Frame too small or missing: {filepath}")
                return False

            if not self._stabilize_frame(filepath):
                return False

            # Annotate with sunspot detection
            self._annotate_frame(filepath, seq)

            if seq % 10 == 0 or seq == 1:
                logger.info(f"[Timelapse] Frame {seq} captured + annotated")
            return True

        except subprocess.TimeoutExpired:
            logger.warning("[Timelapse] Frame grab timed out")
            return False
        except Exception as e:
            logger.warning(f"[Timelapse] Frame grab error: {e}")
            return False

    def _stabilize_frame(self, frame_path: str) -> bool:
        """Apply translation stabilization in-place to reduce seeing jitter."""
        frame = cv2.imread(frame_path)
        if frame is None:
            logger.warning(f"[Timelapse] Stabilize read failed: {frame_path}")
            return False

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        gray32 = np.float32(gray)

        if not self._stabilize_enabled:
            self._stabilize_ref_gray = gray32
            return True

        if self._stabilize_ref_gray is None:
            self._stabilize_ref_gray = gray32
            return True

        try:
            (dx, dy), response = cv2.phaseCorrelate(self._stabilize_ref_gray, gray32)
            if response < 0.02:
                self._stabilize_ref_gray = gray32
                return True

            dx = float(np.clip(dx, -self._stabilize_max_shift_px, self._stabilize_max_shift_px))
            dy = float(np.clip(dy, -self._stabilize_max_shift_px, self._stabilize_max_shift_px))

            prev_x, prev_y = self._stabilize_offset
            a = float(np.clip(self._stabilize_smoothing, 0.0, 1.0))
            smoothed_x = (a * dx) + ((1.0 - a) * prev_x)
            smoothed_y = (a * dy) + ((1.0 - a) * prev_y)
            self._stabilize_offset = (smoothed_x, smoothed_y)

            m = np.float32([[1, 0, smoothed_x], [0, 1, smoothed_y]])
            stabilized = cv2.warpAffine(
                frame,
                m,
                (frame.shape[1], frame.shape[0]),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
            cv2.imwrite(frame_path, stabilized, [cv2.IMWRITE_JPEG_QUALITY, 92])

            stab_gray = cv2.cvtColor(stabilized, cv2.COLOR_BGR2GRAY)
            stab_gray = cv2.GaussianBlur(stab_gray, (5, 5), 0).astype(np.float32)
            self._stabilize_ref_gray = (0.9 * self._stabilize_ref_gray) + (0.1 * stab_gray)
            return True
        except Exception as e:
            logger.warning(f"[Timelapse] Stabilization skipped: {e}")
            self._stabilize_ref_gray = gray32
            return True

    def _annotate_frame(self, raw_path: str, seq: int):
        """Create an annotated copy with sunspot circles and timestamp."""
        try:
            frame = cv2.imread(raw_path)
            if frame is None:
                return
            annotated = annotate_sunspots(frame)
            ann_dir = os.path.join(self._frames_dir, "annotated")
            os.makedirs(ann_dir, exist_ok=True)
            ann_path = os.path.join(ann_dir, f"frame_{seq:05d}.jpg")
            cv2.imwrite(ann_path, annotated, [cv2.IMWRITE_JPEG_QUALITY, 92])
        except Exception as e:
            logger.warning(f"[Timelapse] Annotation failed for frame {seq}: {e}")

    def _assemble_video(self):
        """Stitch JPEG frames into MP4 timelapses (raw + annotated)."""
        if not self._frames_dir or not os.path.isdir(self._frames_dir):
            return

        raw_frames = sorted(
            f for f in os.listdir(self._frames_dir) if f.endswith(".jpg")
        )
        if len(raw_frames) < 2:
            logger.info("[Timelapse] Not enough frames to assemble video")
            return

        fps = 10

        # Assemble raw frames
        self._encode_sequence(
            os.path.join(self._frames_dir, "frame_%05d.jpg"),
            self._output_path, len(raw_frames), fps, "raw"
        )

        # Assemble annotated frames (sunspot-annotated version)
        ann_dir = os.path.join(self._frames_dir, "annotated")
        if os.path.isdir(ann_dir):
            ann_frames = sorted(
                f for f in os.listdir(ann_dir) if f.endswith(".jpg")
            )
            if len(ann_frames) >= 2:
                ann_output = self._output_path.rsplit(".", 1)[0] + "_sunspots.mp4"
                self._encode_sequence(
                    os.path.join(ann_dir, "frame_%05d.jpg"),
                    ann_output, len(ann_frames), fps, "annotated"
                )

    def _encode_sequence(self, pattern: str, output: str,
                         frame_count: int, fps: float, label: str):
        """Encode a numbered JPEG sequence into an MP4."""
        logger.info(
            f"[Timelapse] Assembling {frame_count} {label} frames → {output}"
        )

        cmd = [
            FFMPEG,
            "-framerate", str(round(fps, 2)),
            "-i", pattern,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "23",
            "-preset", "medium",
            "-y",
            output,
        ]

        try:
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120
            )
            if result.returncode == 0 and os.path.exists(output):
                size_mb = os.path.getsize(output) / (1024 * 1024)
                logger.info(
                    f"[Timelapse] {label.title()} video: {output} "
                    f"({frame_count} frames, {size_mb:.1f} MB)"
                )
                self._write_metadata(frame_count, fps)
                self._generate_thumbnail(output)
            else:
                stderr_tail = result.stderr.decode(errors="replace")[-300:]
                logger.error(f"[Timelapse] {label.title()} assembly failed: {stderr_tail}")
        except Exception as e:
            logger.error(f"[Timelapse] {label.title()} assembly error: {e}", exc_info=True)

    def _write_metadata(self, frame_count: int, fps: float):
        """Write a sidecar JSON metadata file."""
        meta_path = self._output_path.rsplit(".", 1)[0] + ".json"
        metadata = {
            "type": "timelapse",
            "timestamp": self._start_time.isoformat() if self._start_time else None,
            "frame_count": frame_count,
            "interval_seconds": self._interval,
            "fps": round(fps, 2),
            "source": "solar_timelapse",
        }
        try:
            with open(meta_path, "w") as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            logger.warning(f"[Timelapse] Metadata write failed: {e}")

    def _generate_thumbnail(self, video_path: str):
        """Generate a thumbnail from the first frame of the assembled video."""
        thumb_path = video_path.rsplit(".", 1)[0] + "_thumb.jpg"
        try:
            subprocess.run(
                [
                    FFMPEG, "-i", video_path,
                    "-frames:v", "1", "-update", "1",
                    "-q:v", "5", "-y", thumb_path,
                ],
                capture_output=True, timeout=10,
            )
            if os.path.exists(thumb_path):
                logger.info(f"[Timelapse] Thumbnail: {thumb_path}")
        except Exception as e:
            logger.warning(f"[Timelapse] Thumbnail failed: {e}")

    def _get_sun_altitude(self) -> Optional[float]:
        """Return current sun altitude in degrees, or None on error."""
        try:
            lat = float(os.getenv("OBSERVER_LATITUDE", "0"))
            lon = float(os.getenv("OBSERVER_LONGITUDE", "0"))
            elev = float(os.getenv("OBSERVER_ELEVATION", "0"))
            observer = get_my_pos(lat, lon, elev, EARTH)
            local_tz = get_localzone()
            ref_dt = datetime.now(local_tz)
            sun = CelestialObject(name="sun", observer_position=observer)
            sun.update_position(ref_datetime=ref_dt)
            coords = sun.get_coordinates()
            return float(coords["altitude"])
        except Exception as e:
            logger.warning(f"[Timelapse] Sun altitude check failed: {e}")
            return None


# Module-level singleton
_timelapse = SolarTimelapse()


def get_timelapse() -> SolarTimelapse:
    return _timelapse
