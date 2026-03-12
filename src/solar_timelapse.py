"""
Solar Timelapse — day-long background timelapse of the Sun.

Captures one JPEG frame at a configurable interval (default 120s) from the
telescope's RTSP stream, stores frames on disk, and assembles them into an
MP4 timelapse at sunset or on manual stop.

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

from tzlocal import get_localzone

from src import logger
from src.astro import CelestialObject
from src.constants import ASTRO_EPHEMERIS
from src.position import get_my_pos

EARTH = ASTRO_EPHEMERIS["earth"]


class SolarTimelapse:
    """Singleton manager for day-long solar timelapse capture."""

    def __init__(self):
        self._lock = threading.Lock()
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

    # ── Public API ──────────────────────────────────────────────────────

    def start(self, host: str, interval: float = 120.0) -> dict:
        with self._lock:
            if self._running:
                return {"error": "Timelapse already running"}

            self._host = host
            self._rtsp_port = int(os.getenv("SEESTAR_RTSP_PORT", "4554"))
            self._interval = max(10.0, interval)
            self._stop_event.clear()
            self._pause_event.clear()
            self._paused = False
            self._frame_count = 0
            self._last_capture = 0

            now = datetime.now()
            self._start_time = now
            day_str = now.strftime("%Y%m%d")
            self._frames_dir = os.path.join(
                "static", "captures", str(now.year),
                f"{now.month:02d}", f"timelapse_{day_str}"
            )
            os.makedirs(self._frames_dir, exist_ok=True)

            self._output_path = os.path.join(
                "static", "captures", str(now.year),
                f"{now.month:02d}", f"timelapse_{day_str}.mp4"
            )

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
            elapsed = 0
            if self._start_time and self._running:
                elapsed = (datetime.now() - self._start_time).total_seconds()

            next_in = 0
            if self._running and not self._paused and self._last_capture > 0:
                since_last = time.monotonic() - self._last_capture
                next_in = max(0, self._interval - since_last)

            result = {
                "running": self._running,
                "paused": self._paused,
                "interval": self._interval,
                "frame_count": self._frame_count,
                "elapsed": round(elapsed),
                "next_capture_in": round(next_in),
                "frames_dir": self._frames_dir if self._running else None,
                "output_path": self._output_path if self._output_path else None,
            }

        # Filesystem access outside lock
        result["latest_frame"] = self.get_latest_frame_url()
        return result

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def build_preview(self) -> Optional[str]:
        """Assemble current frames into a preview MP4. Returns web path or None."""
        frames_dir = self._frames_dir
        if not frames_dir or not os.path.isdir(frames_dir):
            return None

        frames = sorted(f for f in os.listdir(frames_dir) if f.endswith(".jpg"))
        if len(frames) < 2:
            return None

        preview_path = self._output_path.rsplit(".", 1)[0] + "_preview.mp4"
        pattern = os.path.join(frames_dir, "frame_%05d.jpg")
        fps = max(1, len(frames) / 30)

        cmd = [
            "ffmpeg",
            "-framerate", str(round(fps, 2)),
            "-i", pattern,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "23",
            "-preset", "fast",
            "-y",
            preview_path,
        ]

        try:
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60
            )
            if result.returncode == 0 and os.path.exists(preview_path):
                rel = os.path.relpath(preview_path, "static").replace(os.sep, "/")
                logger.info(
                    f"[Timelapse] Preview built: {len(frames)} frames → {preview_path}"
                )
                return f"/static/{rel}"
        except Exception as e:
            logger.warning(f"[Timelapse] Preview build failed: {e}")
        return None

    def get_latest_frame_url(self) -> Optional[str]:
        """Return web URL for the most recently captured frame."""
        frames_dir = self._frames_dir
        if not frames_dir or not os.path.isdir(frames_dir):
            return None
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
        """Grab a single JPEG frame from the RTSP stream via ffmpeg."""
        if not self._host:
            return False

        seq = self._frame_count + 1
        filename = f"frame_{seq:05d}.jpg"
        filepath = os.path.join(self._frames_dir, filename)
        rtsp_url = f"rtsp://{self._host}:{self._rtsp_port}/stream"

        cmd = [
            "ffmpeg",
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

            if seq % 10 == 0 or seq == 1:
                logger.info(f"[Timelapse] Frame {seq} captured")
            return True

        except subprocess.TimeoutExpired:
            logger.warning("[Timelapse] Frame grab timed out")
            return False
        except Exception as e:
            logger.warning(f"[Timelapse] Frame grab error: {e}")
            return False

    def _assemble_video(self):
        """Stitch JPEG frames into an MP4 timelapse using ffmpeg."""
        if not self._frames_dir or not os.path.isdir(self._frames_dir):
            return

        frames = sorted(
            f for f in os.listdir(self._frames_dir) if f.endswith(".jpg")
        )
        if len(frames) < 2:
            logger.info("[Timelapse] Not enough frames to assemble video")
            return

        output = self._output_path
        logger.info(
            f"[Timelapse] Assembling {len(frames)} frames → {output}"
        )

        # Use glob pattern for sequential frames
        pattern = os.path.join(self._frames_dir, "frame_%05d.jpg")

        # Target ~30 seconds for the output regardless of frame count
        # fps = frame_count / desired_duration
        fps = max(1, len(frames) / 30)

        cmd = [
            "ffmpeg",
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
                    f"[Timelapse] Video assembled: {output} "
                    f"({len(frames)} frames, {size_mb:.1f} MB)"
                )
                self._write_metadata(len(frames), fps)
                self._generate_thumbnail(output)
            else:
                stderr_tail = result.stderr.decode(errors="replace")[-300:]
                logger.error(f"[Timelapse] Assembly failed: {stderr_tail}")
        except Exception as e:
            logger.error(f"[Timelapse] Assembly error: {e}", exc_info=True)

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
                    "ffmpeg", "-i", video_path,
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
