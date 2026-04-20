"""
Telescope control endpoints for Seestar integration.

Provides RESTful web API for controlling the Seestar telescope, including
connection management, viewing modes, video recording, photo capture,
live preview, and file management.

v0.2.0 §3.1 — mechanical split: isolated concerns now live in sub-modules:
  src.telescope.debug_log      — NDJSON agent debug logger
  src.telescope.motor_state    — GoTo/nudge mutex + _CtrlState enum
  src.telescope.recorder_wiring — TransitRecorder scheduling glue
The route implementations remain here; src/telescope/routes.py is a shim.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, Optional

from flask import Response, jsonify, request

from src import logger
from src.astro import CelestialObject
from src.constants import ASTRO_EPHEMERIS, get_ffmpeg_path

FFMPEG = get_ffmpeg_path() or "ffmpeg"
if os.path.isabs(FFMPEG):
    _ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    _ffprobe_candidate = os.path.join(os.path.dirname(FFMPEG), _ffprobe_name)
    FFPROBE = _ffprobe_candidate if os.path.exists(_ffprobe_candidate) else "ffprobe"
else:
    FFPROBE = shutil.which("ffprobe") or "ffprobe"
from src.alpaca_client import (
    AlpacaClient,
    MockAlpacaClient,
    create_alpaca_client_from_env,
)
from src.position import get_my_pos
from src.seestar_client import SeestarClient
from src.sun_centering import (
    SunCenteringAdapter,
    get_sun_center_service,
    start_sun_center_service,
    stop_sun_center_service,
)
from src.site_context import (
    clear_observer_browser_override,
    get_observer_coordinates,
    observer_snapshot_for_api,
    set_observer_from_browser,
)
from src.solar_timelapse import get_timelapse

# Sub-module imports (v0.2.0 §3.1 split)
from src.telescope.debug_log import (  # noqa: E402
    _agent_debug_log,
    _auto_detect_rtsp_warn_ts,
    _rtsp_probe_fail_last_warn_by_host_mode,
    _rtsp_recover_last_attempt_by_host_mode,
    _RTSP_RECOVERY_COOLDOWN_SECONDS,
)
from src.telescope.motor_state import _CtrlState, ctrl as _motor_ctrl  # noqa: E402
from src.telescope.recorder_wiring import (  # noqa: E402
    schedule_recordings_for_transits as _schedule_recordings,
)

# Get EARTH reference for position calculations
EARTH = ASTRO_EPHEMERIS["earth"]

# ── Motor control state machine ───────────────────────────────────────────
# _CtrlState enum and the ctrl singleton now live in src.telescope.motor_state.
# Use _motor_ctrl.state, _motor_ctrl.lock, _motor_ctrl.pre_nudge_tracking
# throughout this module.
# ─────────────────────────────────────────────────────────────────────────

# User-adjustable settings pushed from the browser (see /api/settings).
# Stored here so the heartbeat reconnect logic can read them without a
# circular import back to app.py.
_user_settings: dict = {
    "min_reconnect_altitude": None,  # degrees; None → fall back to env var
}


def _rtsp_port_probe_order(primary: int = 4554) -> list[int]:
    """Fixed RTSP port probe order for Seestar (4554 then 8554).

    Env overrides were removed because a stale SEESTAR_RTSP_PORT in .env
    from a previous firmware can mask the working port across restarts.
    The probe is authoritative.
    """
    return [4554, 8554]


def _rtsp_path_candidates() -> list[str]:
    """Fixed RTSP path probe list for Seestar (/stream)."""
    return ["/stream"]


def _rtsp_candidate_urls(host: str) -> list[str]:
    """All RTSP URL candidates in probe order (port × path)."""
    urls: list[str] = []
    for port in _rtsp_port_probe_order():
        for path in _rtsp_path_candidates():
            urls.append(f"rtsp://{host}:{port}{path}")
    return list(dict.fromkeys(urls))


def _probe_rtsp_url(rtsp_url: str, timeout_seconds: int = 5) -> bool:
    """Return True when one frame can be read quickly from the given RTSP URL."""
    cmd = [
        FFMPEG,
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _resolve_rtsp_stream_url(host: str, timeout_seconds: int = 6) -> Optional[str]:
    """Return first working RTSP URL from candidate list, else None."""
    for url in _rtsp_candidate_urls(host):
        if _probe_rtsp_url(url, timeout_seconds=timeout_seconds):
            return url
    return None


def _ensure_rtsp_ready(
    client,
    *,
    timeout_seconds: int = 4,
    allow_mode_reassert: bool = True,
    warn_cooldown_seconds: float = 20.0,
) -> Optional[str]:
    """Return a working RTSP URL from probe candidates, else None."""
    rtsp_url = _resolve_rtsp_stream_url(client.host, timeout_seconds=timeout_seconds)
    if rtsp_url:
        return rtsp_url

    mode = getattr(client, "_viewing_mode", None)
    if allow_mode_reassert and mode in ("sun", "moon"):
        host_mode = f"{client.host}:{mode}"
        now = time.monotonic()
        last_attempt = _rtsp_recover_last_attempt_by_host_mode.get(host_mode, 0.0)
        if now - last_attempt >= _RTSP_RECOVERY_COOLDOWN_SECONDS:
            _rtsp_recover_last_attempt_by_host_mode[host_mode] = now
            try:
                logger.warning(
                    "[RTSP] Probe failed (mode=%s) — reasserting %s live view",
                    mode,
                    mode,
                )
                if mode == "moon":
                    client.start_lunar_mode()
                else:
                    client.start_solar_mode()
                time.sleep(1.5)
                rtsp_url = _resolve_rtsp_stream_url(
                    client.host, timeout_seconds=max(timeout_seconds, 6)
                )
                if rtsp_url:
                    logger.info("[RTSP] Stream recovered after %s mode reassertion", mode)
                    return rtsp_url
            except Exception as e:
                logger.debug("[RTSP] Mode reassertion failed: %s", e)
        else:
            logger.debug(
                "[RTSP] Probe failed (mode=%s) and recovery is cooling down", mode
            )

    host_mode_warn_key = f"{getattr(client, 'host', 'unknown')}:{mode or 'unknown'}"
    now = time.monotonic()
    last_warn = _rtsp_probe_fail_last_warn_by_host_mode.get(host_mode_warn_key, 0.0)
    if now - last_warn >= max(1.0, float(warn_cooldown_seconds)):
        _rtsp_probe_fail_last_warn_by_host_mode[host_mode_warn_key] = now
        logger.warning(f"[RTSP] Probe failed (mode={mode})")
    else:
        logger.debug(
            "[RTSP] Probe failed (mode=%s) and warning is cooling down",
            mode,
        )
    return None


# Mock Telescope Client for Testing


class MockSeestarClient:
    """Mock Seestar client for testing without hardware."""

    def __init__(
        self, host: str = "mock.telescope", port: int = 4700, timeout: int = 10
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._connected = False
        self._recording = False
        self._recording_start_time: Optional[datetime] = None
        self._focus_pos: Optional[int] = None
        self._camera_gain: Optional[int] = 80
        self._viewing_mode: Optional[str] = (
            None  # sun | moon | scenery — mirrors SeestarClient
        )
        logger.info(f"[Mock] Initialized mock Seestar client for {host}:{port}")

    def connect(self) -> bool:
        """Simulate connection."""
        import time

        time.sleep(0.5)  # Simulate connection delay
        self._connected = True
        logger.info("[Mock] Connected to mock telescope")
        return True

    def disconnect(self) -> bool:
        """Simulate disconnection."""
        if self._recording:
            self.stop_recording()
        self._connected = False
        logger.info("[Mock] Disconnected from mock telescope")
        return True

    def is_connected(self) -> bool:
        """Check if connected."""
        return self._connected

    def start_solar_mode(self) -> bool:
        """Simulate starting solar mode."""
        import time

        time.sleep(0.3)
        self._viewing_mode = "sun"
        logger.info("[Mock] Started solar viewing mode")
        return True

    def start_lunar_mode(self) -> bool:
        """Simulate starting lunar mode."""
        import time

        time.sleep(0.3)
        self._viewing_mode = "moon"
        logger.info("[Mock] Started lunar viewing mode")
        return True

    def start_scenery_mode(self) -> bool:
        """Simulate starting scenery mode."""
        import time

        time.sleep(0.3)
        self._viewing_mode = "scenery"
        logger.info("[Mock] Started scenery viewing mode")
        return True

    def stop_view_mode(self) -> bool:
        """Simulate stopping view mode."""
        import time

        time.sleep(0.2)
        self._viewing_mode = None
        logger.info("[Mock] Stopped viewing mode")
        return True

    def start_recording(self, duration_seconds: Optional[int] = None) -> bool:
        """Simulate starting recording."""
        if self._recording:
            logger.warning("[Mock] Recording already in progress")
            return True

        import time

        time.sleep(0.3)
        self._recording = True
        self._recording_start_time = datetime.now()
        logger.info(f"[Mock] Started recording (duration: {duration_seconds}s)")
        return True

    def stop_recording(self) -> bool:
        """Simulate stopping recording."""
        if not self._recording:
            logger.warning("[Mock] No recording in progress")
            return True

        import time

        time.sleep(0.3)
        duration = None
        if self._recording_start_time:
            duration = (datetime.now() - self._recording_start_time).total_seconds()

        self._recording = False
        self._recording_start_time = None
        logger.info(f"[Mock] Stopped recording (duration: {duration:.1f}s)")
        return True

    def is_recording(self) -> bool:
        """Check if recording."""
        return self._recording

    def capture_photo(self, exposure_time: float = 1.0) -> dict:
        """Simulate photo capture."""
        if not self._connected:
            raise RuntimeError("Cannot capture photo: not connected")
        logger.info(f"[Mock] Captured photo (exposure: {exposure_time}s)")
        return {"result": "success", "exposure_time": exposure_time}

    def get_albums(self) -> dict:
        """Simulate getting albums."""
        if not self._connected:
            raise RuntimeError("Cannot get albums: not connected")
        logger.info("[Mock] Retrieved albums")
        return {
            "path": "DCIM",
            "list": [
                {
                    "name": "2026-02-07",
                    "files": [
                        {
                            "name": "test_photo.jpg",
                            "thn": "test_photo_thumb.jpg",
                            "size": 1024000,
                        }
                    ],
                }
            ],
        }

    def list_files(self) -> dict:
        """Return mock file list."""
        import time

        time.sleep(0.5)
        logger.info("[Mock] Retrieved mock file list")
        return {
            "path": "Astronomy",
            "list": [
                {
                    "name": "Solar_2026-02-03",
                    "files": [
                        {"name": "transit_143000.mp4", "thn": "transit_143000_thn.jpg"},
                        {"name": "transit_150000.mp4", "thn": "transit_150000_thn.jpg"},
                    ],
                },
                {
                    "name": "Lunar_2026-02-02",
                    "files": [
                        {"name": "moon_213000.mp4", "thn": "moon_213000_thn.jpg"},
                    ],
                },
            ],
        }

    def get_status(self) -> Dict[str, Any]:
        """Get mock status."""
        status = {
            "connected": self._connected,
            "recording": self._recording,
            "host": self.host,
            "port": self.port,
        }

        if self._recording_start_time:
            status["recording_duration"] = (
                datetime.now() - self._recording_start_time
            ).total_seconds()

        return status

    def open_arm(self):
        logger.info("[Mock] open_arm")
        return True

    def park(self):
        logger.info("[Mock] park")
        return True

    def shutdown(self):
        logger.info("[Mock] shutdown")
        return True

    def autofocus(self):
        logger.info("[Mock] autofocus")
        base = self._focus_pos if self._focus_pos is not None else 5000
        self._focus_pos = int(base) + 25
        return {
            "sent": True,
            "confirmed": True,
            "provider": "mock",
            "method": "start_auto_focuse",
            "response": {"result": 0},
        }

    def move_step_focus(self, steps: int):
        base = self._focus_pos if self._focus_pos is not None else 0
        self._focus_pos = int(base) + int(steps)
        logger.info(f"[Mock] move_step_focus steps={steps} -> {self._focus_pos}")
        return {"focus_pos": self._focus_pos}

    def get_focuser_position(self, use_fallbacks: bool = True) -> Optional[int]:
        return self._focus_pos

    def refresh_focus_throttled(self, min_interval_sec: float = 5.0) -> None:
        pass

    def set_gain(self, gain):
        self._camera_gain = int(gain)
        logger.info(f"[Mock] set_gain {gain}")
        return {"result": "ok"}

    def set_exposure(self, stack_ms=None, preview_ms=None):
        logger.info(f"[Mock] set_exposure stack={stack_ms} preview={preview_ms}")
        return {"result": "ok"}

    def set_lp_filter(self, enabled):
        logger.info(f"[Mock] set_lp_filter {enabled}")
        return {"result": "ok"}

    def set_dew_heater(self, enabled, power=50):
        logger.info(f"[Mock] set_dew_heater {enabled} power={power}")
        return {"result": "ok"}

    def set_manual_exp(self, enabled):
        logger.info(f"[Mock] set_manual_exp {enabled}")
        return {"result": "ok"}

    def _ping(self) -> None:
        pass  # Mock: no-op, _viewing_mode is managed by start_*/stop_* methods

    def start_view_star(self, ra, dec, target_name="", lp_filter=False):
        logger.info(f"[Mock] start_view_star {target_name}")
        return {"result": "ok"}


# ------------------------------------------------------------------ #
#  Named GoTo locations — persisted to data/goto_locations.json       #
# ------------------------------------------------------------------ #
import threading as _threading

_LOCATIONS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "goto_locations.json"
)
_locations_lock = _threading.Lock()

_FAVORITES_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "telescope_favorites.json"
)
_favorites_lock = _threading.Lock()


def _load_locations() -> dict:
    """Return the saved goto locations dict, or {} on any error."""
    try:
        with open(_LOCATIONS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_locations(locs: dict) -> None:
    """Atomically write the locations dict to disk."""
    os.makedirs(os.path.dirname(_LOCATIONS_FILE), exist_ok=True)
    tmp = _LOCATIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(locs, f, indent=2)
    os.replace(tmp, _LOCATIONS_FILE)


def _normalize_favorite_url(url: Any) -> Optional[str]:
    """Normalise a favorite URL to a stable captures-relative URL.

    Favorites are keyed by `/static/captures/...` URL so they remain stable
    across Flask port changes between app restarts.
    """
    if not isinstance(url, str):
        return None
    s = url.strip()
    if not s:
        return None
    s = s.split("?", 1)[0].split("#", 1)[0]
    if not s.startswith("/static/captures/"):
        return None
    if ".." in s:
        return None
    return s


def _load_telescope_favorites() -> list[str]:
    """Return persisted favorite capture URLs, or [] on any error."""
    try:
        with open(_FAVORITES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    if not isinstance(raw, list):
        return []

    out: list[str] = []
    seen = set()
    for item in raw:
        norm = _normalize_favorite_url(item)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _save_telescope_favorites(favorites: list[str]) -> None:
    """Atomically write favorite capture URLs to disk."""
    os.makedirs(os.path.dirname(_FAVORITES_FILE), exist_ok=True)
    tmp = _FAVORITES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(favorites, f, indent=2)
    os.replace(tmp, _FAVORITES_FILE)


# ------------------------------------------------------------------ #
#  Module-level state (singleton pattern)                             #
# ------------------------------------------------------------------ #
_telescope_client: Optional[SeestarClient] = None
_alpaca_client: Optional[AlpacaClient] = None
_recording_state: Dict[str, Any] = {
    "active": False,
    "start_time": None,
}
_simulate_mode: bool = False  # Runtime toggle for simulation


# Helper Functions


def is_enabled() -> bool:
    """Check if Seestar integration is enabled via environment variable."""
    return os.getenv("ENABLE_SEESTAR", "false").lower() == "true"


def is_mock_mode() -> bool:
    """Check if mock telescope mode is enabled (env or runtime toggle)."""
    return _simulate_mode or os.getenv("MOCK_TELESCOPE", "false").lower() == "true"


def get_telescope_client() -> Optional[SeestarClient]:
    """
    Get or create singleton telescope client.

    Returns:
        SeestarClient or MockSeestarClient instance, or None if not yet created
    """
    global _telescope_client
    if _telescope_client is None:
        # Check if mock mode is enabled
        if is_mock_mode():
            logger.info("[Telescope] Mock mode enabled - using MockSeestarClient")
            _telescope_client = MockSeestarClient(host="mock.telescope", port=4700)
            return _telescope_client

        # Regular mode - use real Seestar client.
        # A blank host is OK — _auto_discover() will find the scope via UDP
        # broadcast or TCP subnet scan when connect() is called.
        host = os.getenv("SEESTAR_HOST", "")

        try:
            port = int(os.getenv("SEESTAR_PORT", "4700"))
            timeout = int(
                os.getenv("SEESTAR_TIMEOUT", "30")
            )  # Increased from 10 to 30 seconds
            heartbeat = int(
                os.getenv("SEESTAR_HEARTBEAT_INTERVAL", "3")
            )  # 3 seconds matches seestar_alp
            retry_attempts = int(
                os.getenv("SEESTAR_RETRY_ATTEMPTS", "3")
            )  # Number of connection attempts
            retry_delay = float(
                os.getenv("SEESTAR_RETRY_INITIAL_DELAY", "1")
            )  # Initial delay before first retry
            _telescope_client = SeestarClient(
                host=host,
                port=port,
                timeout=timeout,
                heartbeat_interval=heartbeat,
                retry_attempts=retry_attempts,
                retry_initial_delay=retry_delay,
            )
            # Gate reconnect attempts: only reconnect when the selected target
            # is at or above the minimum altitude the user set in the UI quadrant.
            try:
                from src.astro import target_above_min_altitude

                _env_min_alt = float(os.getenv("MIN_TARGET_ALTITUDE", "10"))

                def _make_horizon_check(client, env_min_alt):
                    def _check():
                        lat, lon, elev = get_observer_coordinates()
                        min_alt = (
                            _user_settings.get("min_reconnect_altitude") or env_min_alt
                        )
                        return target_above_min_altitude(
                            client._viewing_mode, lat, lon, elev, min_alt
                        )

                    return _check

                _telescope_client._above_horizon_check = _make_horizon_check(
                    _telescope_client, _env_min_alt
                )
            except Exception as _e:
                logger.warning(f"[Telescope] Could not set horizon check: {_e}")
        except Exception as e:
            logger.error(f"[Telescope] Failed to create client: {e}")
            return None

    return _telescope_client


def get_alpaca_client():
    """Get or create singleton ALPACA client for motor control."""
    global _alpaca_client
    if _alpaca_client is None:
        if is_mock_mode():
            _alpaca_client = MockAlpacaClient()
        else:
            _alpaca_client = create_alpaca_client_from_env()
    return _alpaca_client


def handle_error(e: Exception, default_code: int = 500) -> tuple:
    """
    Map exceptions to HTTP responses.

    Args:
        e: Exception to handle
        default_code: Default HTTP status code

    Returns:
        Tuple of (json_response, status_code)
    """
    error_msg = str(e)

    # Map specific errors to HTTP status codes
    if "not connected" in error_msg.lower():
        status_code = 400
    elif "timeout" in error_msg.lower():
        status_code = 504
    elif "connection failed" in error_msg.lower():
        status_code = 503
    elif "already in progress" in error_msg.lower():
        status_code = 409
    elif "no recording" in error_msg.lower():
        status_code = 400
    else:
        status_code = default_code

    logger.error(f"[Telescope] Error: {error_msg}", exc_info=True)
    return jsonify({"error": error_msg}), status_code


# ------------------------------------------------------------------ #
#  Extended control routes — GoTo, park, autofocus, camera settings  #
#  named locations                                                    #
# ------------------------------------------------------------------ #


def telescope_goto():
    """POST /telescope/goto — slew to a position.

    Body: {mode: "radec"|"altaz", ra?, dec?, alt?, az?}
    """
    run_id = f"goto_{int(time.time() * 1000)}"
    client = get_telescope_client()
    # region agent log
    _agent_debug_log(
        run_id,
        "H9_H10",
        "src/telescope_routes.py:telescope_goto:entry",
        "GoTo route invoked",
        {
            "client_exists": bool(client),
            "connected": bool(client and client.is_connected()),
        },
    )
    # endregion
    if not client or not client.is_connected():
        return jsonify({"error": "Telescope not connected"}), 503

    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "altaz")
    # region agent log
    _agent_debug_log(
        run_id,
        "H9_H10",
        "src/telescope_routes.py:telescope_goto:payload",
        "GoTo payload parsed",
        {"mode": mode, "keys": sorted(list(data.keys()))},
    )
    # endregion

    alpaca = get_alpaca_client()
    if not alpaca or not alpaca.is_connected():
        return (
            jsonify({"error": "ALPACA not connected — motor commands require ALPACA"}),
            503,
        )

    # State machine: abort any active nudge before slewing
    with _motor_ctrl.lock:
        if _motor_ctrl.state == _CtrlState.NUDGING:
            logger.info("[GoTo] Aborting active nudge before slew")
            try:
                alpaca.stop_axes(timeout_sec=2.0)
                if _motor_ctrl.pre_nudge_tracking:
                    alpaca.set_tracking(True, timeout_sec=2.0)
                    _motor_ctrl.pre_nudge_tracking = False
            except Exception as ex:
                logger.warning(f"[GoTo] nudge abort failed: {ex}")
        if _recording_state["active"]:
            return jsonify({"error": "Cannot GoTo while recording is active"}), 409
        _motor_ctrl.state = _CtrlState.SLEWING

    try:
        if mode == "radec":
            ra = float(data["ra"])
            dec = float(data["dec"])
            result = alpaca.goto_radec(ra, dec, timeout_sec=3.0)

            def _wait_radec_complete():
                deadline = time.time() + 60
                while time.time() < deadline:
                    try:
                        if not alpaca.is_slewing(timeout_sec=1.2):
                            break
                    except Exception:
                        break
                    time.sleep(0.5)
                with _motor_ctrl.lock:
                    _motor_ctrl.state = _CtrlState.IDLE

            threading.Thread(target=_wait_radec_complete, daemon=True).start()
            # region agent log
            _agent_debug_log(
                run_id,
                "H10",
                "src/telescope_routes.py:telescope_goto:radec_ok",
                "GoTo RA/Dec command succeeded via ALPACA",
                {"ra": ra, "dec": dec, "result_type": type(result).__name__},
            )
            # endregion
            return (
                jsonify(
                    {
                        "success": True,
                        "result": result,
                        "via": "ALPACA",
                        "provider": "alpaca",
                        "provider_ready": True,
                    }
                ),
                200,
            )
        elif mode == "altaz":
            alt = float(data["alt"])
            az = float(data["az"])
            # region agent log
            _agent_debug_log(
                run_id,
                "H10",
                "src/telescope_routes.py:telescope_goto:altaz_begin",
                "GoTo Alt/Az command begin",
                {"alt": alt, "az": az},
            )
            # endregion
            logger.info(f"[GoTo] Alt/Az GoTo to alt={alt:.1f}° az={az:.1f}°")
            resume = (data.get("resume_tracking") or "").strip().lower()
            if resume not in ("", "sun", "moon"):
                resume = ""

            def _goto_then_resume():
                result = alpaca.goto_altaz(alt, az, timeout_sec=3.0)
                logger.info(f"[GoTo] ALPACA goto_altaz dispatched: {result!r}")
                if resume in ("sun", "moon"):
                    # Wait for slew to complete instead of a fixed sleep
                    with _motor_ctrl.lock:
                        _motor_ctrl.state = _CtrlState.GOTO_RESUMING
                    deadline = time.time() + 60
                    while time.time() < deadline:
                        try:
                            if not alpaca.is_slewing(timeout_sec=1.2):
                                break
                        except Exception:
                            break
                        time.sleep(0.5)
                    try:
                        if resume == "sun":
                            client.start_solar_mode()
                            logger.info(
                                "[GoTo] Resumed solar tracking after Alt/Az GoTo"
                            )
                            _maybe_auto_start_detector("sun")
                        else:
                            client.start_lunar_mode()
                            logger.info(
                                "[GoTo] Resumed lunar tracking after Alt/Az GoTo"
                            )
                            _maybe_auto_start_detector("moon")
                    except Exception as ex:
                        logger.warning(f"[GoTo] resume_tracking={resume} failed: {ex}")
                with _motor_ctrl.lock:
                    _motor_ctrl.state = _CtrlState.IDLE

            t = threading.Thread(
                target=_goto_then_resume,
                daemon=True,
            )
            t.start()
            return (
                jsonify(
                    {
                        "success": True,
                        "message": f"Slewing to alt={alt:.1f}° az={az:.1f}° via ALPACA",
                        "via": "ALPACA",
                        "tracking_note": (
                            "Scope enters sidereal tracking after GoTo. "
                            "Pass resume_tracking: 'sun' or 'moon' to switch mode after slew."
                        ),
                        "resume_tracking": resume or None,
                        "provider": "alpaca",
                        "provider_ready": True,
                    }
                ),
                200,
            )
        else:
            return jsonify({"error": f"Unknown mode '{mode}'"}), 400

    except (KeyError, ValueError) as e:
        with _motor_ctrl.lock:
            _motor_ctrl.state = _CtrlState.IDLE
        # region agent log
        _agent_debug_log(
            run_id,
            "H9",
            "src/telescope_routes.py:telescope_goto:bad_request",
            "GoTo payload invalid",
            {"error": str(e)},
        )
        # endregion
        return jsonify({"error": f"Invalid parameters: {e}"}), 400
    except Exception as e:
        with _motor_ctrl.lock:
            _motor_ctrl.state = _CtrlState.IDLE
        # region agent log
        _agent_debug_log(
            run_id,
            "H10",
            "src/telescope_routes.py:telescope_goto:exception",
            "GoTo route exception",
            {"error": str(e), "type": type(e).__name__},
        )
        # endregion
        return handle_error(e)


def telescope_stop_view():
    """POST /telescope/stop — stop current view/stack mode and abort any motor activity."""
    client = get_telescope_client()
    if not client or not client.is_connected():
        return jsonify({"error": "Telescope not connected"}), 503
    try:
        alpaca = get_alpaca_client()
        if alpaca and alpaca.is_connected():
            try:
                alpaca.abort_slew()
                alpaca.stop_axes()
            except Exception as ex:
                logger.warning(f"[Stop] ALPACA abort failed: {ex}")
        client.stop_view_mode()
        with _motor_ctrl.lock:
            _motor_ctrl.state = _CtrlState.IDLE
            _motor_ctrl.pre_nudge_tracking = False
        return jsonify({"success": True, "message": "Stop command sent"}), 200
    except Exception as e:
        return handle_error(e)


def telescope_nudge():
    """POST /telescope/nudge — joystick-style manual move via ALPACA.

    Body: {"axis": 0, "rate": 1.0}
      axis: 0=RA/Az, 1=Dec/Alt.  rate: deg/sec (negative=reverse), 0=stop.
    """
    alpaca = get_alpaca_client()
    if not alpaca or not alpaca.is_connected():
        return (
            jsonify({"error": "ALPACA not connected — motor commands require ALPACA"}),
            503,
        )

    with _motor_ctrl.lock:
        if _motor_ctrl.state in (_CtrlState.SLEWING, _CtrlState.GOTO_RESUMING):
            return jsonify({"error": f"Cannot nudge while {_motor_ctrl.state.value}"}), 409

    data = request.get_json(force=True, silent=True) or {}
    try:
        axis = int(data.get("axis", 0))
        rate = float(data.get("rate", 1.0))
        if axis not in (0, 1):
            return jsonify({"error": "axis must be 0 or 1"}), 400
        max_rate = float(alpaca.get_max_move_rate(axis))
        if max_rate <= 0:
            max_rate = 6.0
        if abs(rate) > max_rate:
            clamped = max(-max_rate, min(max_rate, rate))
            logger.info(
                f"[Nudge] Clamped rate from {rate:.2f} to {clamped:.2f} (max {max_rate:.2f})"
            )
            rate = clamped

        # Disable ALPACA tracking before MoveAxis (required by firmware — Finding A)
        client = get_telescope_client()
        viewing_mode = getattr(client, "_viewing_mode", None) if client else None
        try:
            pre_tracking = bool(alpaca.get_tracking(timeout_sec=1.2))
        except Exception:
            pre_tracking = viewing_mode in ("sun", "moon")
        try:
            alpaca.set_tracking(False, timeout_sec=2.0)
        except Exception as ex:
            logger.warning(f"[Nudge] set_tracking(False) failed: {ex}")

        result = alpaca.move_axis(axis, rate, timeout_sec=2.0)

        with _motor_ctrl.lock:
            _motor_ctrl.pre_nudge_tracking = pre_tracking
            _motor_ctrl.state = _CtrlState.NUDGING
        return jsonify({"success": True, "result": result, "via": "ALPACA"}), 200
    except Exception as e:
        with _motor_ctrl.lock:
            _motor_ctrl.state = _CtrlState.IDLE
        return handle_error(e)


def telescope_nudge_stop():
    """POST /telescope/nudge/stop — stop all motion via ALPACA."""
    alpaca = get_alpaca_client()
    if not alpaca or not alpaca.is_connected():
        return jsonify({"error": "ALPACA not connected"}), 503
    try:
        result = alpaca.stop_axes(timeout_sec=2.0)
        # Restore tracking if it was on before the nudge started
        with _motor_ctrl.lock:
            restore = _motor_ctrl.pre_nudge_tracking
            _motor_ctrl.pre_nudge_tracking = False
            _motor_ctrl.state = _CtrlState.IDLE
        if restore:
            try:
                alpaca.set_tracking(True, timeout_sec=2.0)
                logger.info("[NudgeStop] Tracking restored after nudge")
            except Exception as ex:
                logger.warning(f"[NudgeStop] set_tracking(True) failed: {ex}")
        return (
            jsonify(
                {
                    "success": True,
                    "message": "Motion stopped",
                    "via": "ALPACA",
                    "result": result,
                }
            ),
            200,
        )
    except Exception as e:
        with _motor_ctrl.lock:
            _motor_ctrl.pre_nudge_tracking = False
            _motor_ctrl.state = _CtrlState.IDLE
        return handle_error(e)


def telescope_open_arm():
    """POST /telescope/open-arm — open (unfold) the telescope arm."""
    alpaca = get_alpaca_client()
    if alpaca and alpaca.is_connected():
        try:
            result = alpaca.unpark()
            return (
                jsonify(
                    {
                        "success": True,
                        "message": "Unpark (open arm) sent via ALPACA",
                        "via": "ALPACA",
                        "result": result,
                    }
                ),
                200,
            )
        except Exception as e:
            return handle_error(e)

    client = get_telescope_client()
    if not client or not client.is_connected():
        return jsonify({"error": "Telescope not connected"}), 503
    try:
        client.open_arm()
        return (
            jsonify(
                {"success": True, "message": "Open arm command sent", "via": "JSON-RPC"}
            ),
            200,
        )
    except Exception as e:
        return handle_error(e)


def telescope_park():
    """POST /telescope/park — park the telescope."""
    alpaca = get_alpaca_client()
    if alpaca and alpaca.is_connected():
        try:
            result = alpaca.park()
            return (
                jsonify(
                    {
                        "success": True,
                        "message": "Park sent via ALPACA",
                        "via": "ALPACA",
                        "result": result,
                    }
                ),
                200,
            )
        except Exception as e:
            return handle_error(e)

    client = get_telescope_client()
    if not client or not client.is_connected():
        return jsonify({"error": "Telescope not connected"}), 503
    try:
        client.park()
        return (
            jsonify(
                {"success": True, "message": "Park command sent", "via": "JSON-RPC"}
            ),
            200,
        )
    except Exception as e:
        return handle_error(e)


def telescope_shutdown():
    """POST /telescope/shutdown — shutdown the Seestar."""
    client = get_telescope_client()
    if not client or not client.is_connected():
        return jsonify({"error": "Telescope not connected"}), 503
    try:
        client.shutdown()
        return jsonify({"success": True, "message": "Shutdown command sent"}), 200
    except Exception as e:
        return handle_error(e)


def telescope_autofocus():
    """POST /telescope/autofocus — trigger autofocus."""
    client = get_telescope_client()
    if not client or not client.is_connected():
        return jsonify({"error": "Telescope not connected"}), 503
    try:
        body = request.get_json(silent=True) or {}
        steps = int(body.get("steps", 4))
        step_size = int(body.get("step_size", 12))
        exposure = float(body.get("exposure_seconds", 0.8))
        backlash = int(body.get("backlash_steps", 6))

        alpaca = get_alpaca_client()
        if alpaca and alpaca.is_connected():
            active_mode = getattr(client, "_viewing_mode", None)
            af_result = alpaca.run_autofocus(
                num_steps=steps,
                step_size=step_size,
                exposure_seconds=exposure,
                backlash_steps=backlash,
            )
            # ALPACA exposure sequences can leave live view down on some firmware.
            # Reassert current solar/lunar view so preview/detection/timelapse recover.
            if active_mode in ("sun", "moon"):
                try:
                    if active_mode == "moon":
                        client.start_lunar_mode()
                    else:
                        client.start_solar_mode()
                    _ensure_rtsp_ready(client, timeout_seconds=5)
                except Exception as e:
                    logger.warning(
                        "[Telescope] Post-autofocus %s stream restore failed: %s",
                        active_mode,
                        e,
                    )
            if isinstance(af_result, dict) and af_result.get("success"):
                return (
                    jsonify(
                        {
                            "success": True,
                            "confirmed": True,
                            "message": (
                                f"Autofocus complete at position {af_result.get('final_focus_pos')}"
                            ),
                            "result": af_result,
                            "provider": "alpaca_autofocus",
                            "provider_ready": True,
                        }
                    ),
                    200,
                )
            return (
                jsonify(
                    {
                        "error": af_result.get("error")
                        if isinstance(af_result, dict)
                        else "ALPACA autofocus failed",
                        "result": af_result,
                        "provider": "alpaca_autofocus",
                        "provider_ready": True,
                    }
                ),
                502,
            )

        # Fallback: JSON-RPC fire-and-forget command for setups without ALPACA.
        result = client.autofocus()
        sent = isinstance(result, dict) and bool(result.get("sent"))
        if not sent:
            return (
                jsonify(
                    {
                        "error": "Autofocus command was not accepted by the scope",
                        "result": result,
                        "provider": "jsonrpc",
                        "provider_ready": bool(client.is_connected()),
                    }
                ),
                502,
            )
        confirmed = bool(result.get("confirmed"))
        return (
            jsonify(
                {
                    "success": True,
                    "confirmed": confirmed,
                    "message": (
                        "Autofocus started"
                        if confirmed
                        else "Autofocus command sent, but the scope did not confirm start"
                    ),
                    "result": result,
                    "provider": "jsonrpc",
                    "provider_ready": True,
                }
            ),
            (200 if confirmed else 202),
        )
    except Exception as e:
        return handle_error(e)


def telescope_position():
    """GET /telescope/position — current pointing Alt/Az.

    Returns ALPACA-backed scope telemetry when available.
    Does not present computed Sun/Moon coordinates as scope pointing.
    """
    alpaca = get_alpaca_client()
    if alpaca and alpaca.is_connected():
        pos = alpaca.get_cached_position()
        if pos:
            return (
                jsonify(
                    {
                        "alt": pos.get("alt"),
                        "az": pos.get("az"),
                        "ra": pos.get("ra"),
                        "dec": pos.get("dec"),
                        "provider": "alpaca",
                        "provider_ready": True,
                        "source": "scope_telemetry",
                    }
                ),
                200,
            )
        return (
            jsonify(
                {
                    "alt": None,
                    "az": None,
                    "ra": None,
                    "dec": None,
                    "provider": "alpaca",
                    "provider_ready": False,
                    "provider_error": "No cached ALPACA telemetry yet",
                    "suggestion": "Wait for telemetry poll or reconnect telescope",
                }
            ),
            200,
        )

    client = get_telescope_client()
    connected = bool(client and client.is_connected())
    mode = getattr(client, "_viewing_mode", None) if client else None
    return (
        jsonify(
            {
                "alt": None,
                "az": None,
                "ra": None,
                "dec": None,
                "provider": "none",
                "provider_ready": False,
                "provider_error": "ALPACA not connected",
                "connected": connected,
                "viewing_mode": mode,
                "suggestion": "Connect ALPACA for real scope pointing telemetry",
            }
        ),
        200,
    )


def telescope_focus_step():
    """POST /telescope/focus/step — move focuser by N steps."""
    client = get_telescope_client()
    if not client or not client.is_connected():
        return jsonify({"error": "Telescope not connected"}), 503
    try:
        def _alpaca_failed(resp):
            if not isinstance(resp, dict):
                return True
            if resp.get("error"):
                return True
            try:
                return int(resp.get("ErrorNumber", 0)) != 0
            except (TypeError, ValueError):
                return bool(resp.get("ErrorNumber"))

        body = request.get_json(silent=True) or {}
        steps = int(body.get("steps", 0))
        alpaca = get_alpaca_client()
        if alpaca and alpaca.is_connected():
            result = alpaca.move_focuser_steps(steps, timeout_sec=6.0)
            if not _alpaca_failed(result):
                return (
                    jsonify({"success": True, "result": result, "provider": "alpaca"}),
                    200,
                )
            logger.info(
                "[Focus] ALPACA focuser move unavailable, falling back to JSON-RPC"
            )
        result = client.move_step_focus(steps)
        return (
            jsonify({"success": True, "result": result, "provider": "jsonrpc"}),
            200,
        )
    except Exception as e:
        return handle_error(e)


def patch_camera_settings():
    """PATCH /telescope/settings/camera — set gain/exposure/filter/dew heater."""
    client = get_telescope_client()
    if not client or not client.is_connected():
        return jsonify({"error": "Telescope not connected"}), 503

    data = request.get_json(force=True, silent=True) or {}
    results = {}

    try:
        def _alpaca_failed(resp):
            if not isinstance(resp, dict):
                return True
            if resp.get("error"):
                return True
            try:
                return int(resp.get("ErrorNumber", 0)) != 0
            except (TypeError, ValueError):
                return bool(resp.get("ErrorNumber"))

        if "gain" in data:
            gain_value = int(data["gain"])
            gain_provider = "jsonrpc"
            gain_result = None
            alpaca = get_alpaca_client()
            if alpaca and alpaca.is_connected():
                gain_result = alpaca.set_camera_gain(gain_value, timeout_sec=2.0)
                if _alpaca_failed(gain_result):
                    logger.info(
                        "[Camera] ALPACA gain set unavailable, falling back to JSON-RPC"
                    )
                    gain_result = client.set_gain(gain_value)
                else:
                    gain_provider = "alpaca"
            else:
                gain_result = client.set_gain(gain_value)
            results["gain"] = gain_result
            results["gain_provider"] = gain_provider
        if "stack_ms" in data or "preview_ms" in data:
            results["exposure"] = client.set_exposure(
                stack_ms=data.get("stack_ms"),
                preview_ms=data.get("preview_ms"),
            )
        if "lp_filter" in data:
            results["lp_filter"] = client.set_lp_filter(bool(data["lp_filter"]))
        if "dew_heater" in data:
            results["dew_heater"] = client.set_dew_heater(
                bool(data["dew_heater"]),
                power=int(data.get("dew_power", 50)),
            )
        return jsonify({"success": True, "results": results}), 200
    except Exception as e:
        return handle_error(e)


def telescope_auto_exp():
    """POST /telescope/camera/auto-exp — toggle auto/manual exposure.

    Body: {"enabled": true}  → auto-exposure on (manual_exp=false)
          {"enabled": false} → manual exposure
    """
    client = get_telescope_client()
    if not client or not client.is_connected():
        return jsonify({"error": "Telescope not connected"}), 503

    data = request.get_json(force=True, silent=True) or {}
    auto_on = bool(data.get("enabled", True))
    try:
        result = client.set_manual_exp(not auto_on)
        return jsonify({"success": True, "auto_exp": auto_on, "result": result}), 200
    except Exception as e:
        return handle_error(e)


def list_goto_locations():
    """GET /telescope/goto/locations — list all saved named locations."""
    with _locations_lock:
        locs = _load_locations()
    # Return as a sorted list for the frontend dropdown
    items = [{"name": k, **v} for k, v in sorted(locs.items())]
    return jsonify(items), 200


def save_goto_location():
    """POST /telescope/goto/locations — save a named location.

    Body: {name: str, alt: float, az: float}
    """
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        alt = float(data["alt"])
        az = float(data["az"])
    except (KeyError, ValueError) as e:
        return jsonify({"error": f"alt and az (degrees) are required: {e}"}), 400

    with _locations_lock:
        locs = _load_locations()
        locs[name] = {"alt": round(alt, 4), "az": round(az, 4)}
        _save_locations(locs)

    logger.info(f"[GoTo] Saved location '{name}': alt={alt:.2f}° az={az:.2f}°")
    return jsonify({"success": True, "name": name, "alt": alt, "az": az}), 200


def delete_goto_location(name: str):
    """DELETE /telescope/goto/locations/<name> — remove a named location."""
    with _locations_lock:
        locs = _load_locations()
        if name not in locs:
            return jsonify({"error": f"Location '{name}' not found"}), 404
        del locs[name]
        _save_locations(locs)

    logger.info(f"[GoTo] Deleted location '{name}'")
    return jsonify({"success": True}), 200


# Connection Management Endpoints


def discover_seestar():
    """GET /telescope/discover - Find Seestar on the network.

    Tries UDP broadcast first (works across subnets / AP mode), then falls
    back to a TCP /24 port scan of the machine's default interface subnet.
    """
    import concurrent.futures
    import json as _json
    import socket as _socket
    import time as _time

    port = int(os.getenv("SEESTAR_PORT", "4700"))

    # --- Pass 1: UDP broadcast scan_iscope (ALP protocol, port 4720) ---
    udp_found = []
    sock = None
    try:
        UDP_PORT = 4720
        message = _json.dumps({"id": 1, "method": "scan_iscope", "params": ""}) + "\r\n"
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)
        sock.settimeout(2.0)
        sock.bind(("", 0))
        sock.sendto(message.encode(), ("255.255.255.255", UDP_PORT))
        deadline = _time.time() + 2.0
        while _time.time() < deadline:
            try:
                _, addr = sock.recvfrom(1024)
                ip = addr[0]
                if not ip.startswith("127.") and ip not in udp_found:
                    udp_found.append(ip)
            except _socket.timeout:
                break
    except Exception as e:
        logger.debug(f"[Discover] UDP broadcast error (non-fatal): {e}")
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    # --- Pass 1b: ALPACA UDP discovery (port 32227) ---
    alpaca_found = []
    alpaca_port = int(os.getenv("SEESTAR_ALPACA_PORT", "32323"))
    try:
        asock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        asock.setsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)
        asock.settimeout(2.0)
        asock.bind(("", 0))
        asock.sendto(b"alpacadiscovery1", ("255.255.255.255", 32227))
        deadline = _time.time() + 2.0
        while _time.time() < deadline:
            try:
                data, addr = asock.recvfrom(4096)
                ip = addr[0]
                if not ip.startswith("127.") and ip not in alpaca_found:
                    alpaca_found.append(ip)
                    try:
                        reply = _json.loads(data.decode())
                        if "AlpacaPort" in reply:
                            alpaca_port = int(reply["AlpacaPort"])
                    except (ValueError, _json.JSONDecodeError):
                        pass
            except _socket.timeout:
                break
        asock.close()
    except Exception as e:
        logger.debug(f"[Discover] ALPACA UDP error (non-fatal): {e}")

    if udp_found:
        logger.info(f"[Discover] UDP found: {udp_found}, ALPACA: {alpaca_found}")
        return (
            jsonify(
                {
                    "found": udp_found,
                    "port": port,
                    "method": "udp",
                    "alpaca_found": alpaca_found,
                    "alpaca_port": alpaca_port,
                }
            ),
            200,
        )

    # --- Pass 2: TCP /24 port scan ---
    timeout = 0.4
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        return jsonify({"error": "Cannot determine local IP"}), 500

    base = local_ip.rsplit(".", 1)[0]
    hosts = [f"{base}.{i}" for i in range(1, 255)]

    def _probe(ip):
        try:
            with _socket.create_connection((ip, port), timeout=timeout):
                return ip
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        results = list(pool.map(_probe, hosts))

    found = [ip for ip in results if ip]
    logger.info(f"[Discover] TCP scan found: {found}")
    return (
        jsonify(
            {"found": found, "port": port, "subnet": f"{base}.0/24", "method": "tcp"}
        ),
        200,
    )


def connect_telescope():
    """POST /telescope/connect - Connect to Seestar telescope."""
    logger.info("[Telescope] POST /telescope/connect")

    if not is_enabled():
        return (
            jsonify(
                {
                    "error": "Seestar integration is disabled. Set ENABLE_SEESTAR=true in .env"
                }
            ),
            400,
        )

    try:
        client = get_telescope_client()
        if not client:
            return (
                jsonify(
                    {
                        "error": "Failed to initialize telescope client. Check ENABLE_SEESTAR=true in .env"
                    }
                ),
                400,
            )

        if hasattr(client, "reset_connect_log_verbosity"):
            client.reset_connect_log_verbosity()
        client.connect()

        # Also connect ALPACA for motor control (firmware 3.0+)
        alpaca = get_alpaca_client()
        alpaca_ok = False
        if alpaca:
            # Share the discovered host if JSON-RPC found it
            if not alpaca.host and client.host:
                alpaca.host = client.host
            try:
                alpaca_ok = alpaca.connect()
                if alpaca_ok:
                    logger.info(
                        f"[Telescope] ALPACA connected to {alpaca.host}:{alpaca.port}"
                    )
                else:
                    logger.warning(
                        "[Telescope] ALPACA connection failed — motor commands will use JSON-RPC fallback"
                    )
            except Exception as ae:
                logger.warning(f"[Telescope] ALPACA connect error: {ae}")

        # Put scope into solar mode so RTSP stream is available. Always sun (not
        # time-of-day moon) — transit app is sun-first; moon is user-selected later.
        if hasattr(client, "_viewing_mode") and client._viewing_mode is None:
            client._viewing_mode = "sun"
            logger.info("[Telescope] Starting solar view to enable RTSP stream")
            try:
                client.start_solar_mode()
                import time

                time.sleep(2)  # Let scope spin up RTSP
            except Exception as e:
                logger.warning(
                    f"[Telescope] Could not start solar view: {e} — RTSP may be unavailable"
                )

        # Kick the RTSP probe in the background so the user's first preview
        # click hits a cached URL instead of paying the probe cost. The probe
        # is authoritative — any .env-saved port/path is ignored.
        def _probe_rtsp_async(c):
            try:
                url = _resolve_rtsp_stream_url(c.host, timeout_seconds=5)
                if url:
                    c._rtsp_cached_url = url
                    logger.info(f"[RTSP] Probe succeeded on connect: {url}")
                else:
                    logger.warning(
                        "[RTSP] Probe on connect found no working URL — "
                        "will retry on first preview request"
                    )
            except Exception as exc:
                logger.debug(f"[RTSP] Probe on connect errored: {exc}")

        threading.Thread(
            target=_probe_rtsp_async,
            args=(client,),
            daemon=True,
            name="rtsp-probe-on-connect",
        ).start()

        auto_resume = os.getenv("SOLAR_TIMELAPSE_AUTO_RESUME", "true").strip().lower()
        if auto_resume in ("1", "true", "yes", "on"):
            interval_raw = os.getenv("SOLAR_TIMELAPSE_INTERVAL", "120")
            try:
                resume_interval = float(interval_raw)
            except ValueError:
                logger.warning(
                    f"[Telescope] Invalid SOLAR_TIMELAPSE_INTERVAL='{interval_raw}', using 120"
                )
                resume_interval = 120.0
            tl = get_timelapse()
            if not tl.is_running:
                tl.resume_today(host=client.host, interval=resume_interval)

        logger.info(f"[Telescope] Connected to {client.host}:{client.port}")
        return (
            jsonify(
                {
                    "success": True,
                    "connected": True,
                    "host": client.host,
                    "port": client.port,
                    "message": "Connected to Seestar telescope",
                    "alpaca_connected": alpaca_ok,
                    "alpaca_port": alpaca.port if alpaca else None,
                }
            ),
            200,
        )

    except RuntimeError as e:
        error_msg = str(e)
        logger.error(f"[Telescope] Connection error: {error_msg}")
        return (
            jsonify(
                {
                    "success": False,
                    "connected": False,
                    "error": error_msg,
                    "message": "Failed to connect to Seestar. Check that the telescope is powered on, connected to the network, and SEESTAR_HOST is correct.",
                }
            ),
            503,  # Service Unavailable
        )

    except Exception as e:
        return handle_error(e)


def disconnect_telescope():
    """POST /telescope/disconnect - Disconnect from telescope."""
    logger.info("[Telescope] POST /telescope/disconnect")

    try:
        global _recording_state

        client = get_telescope_client()
        if client:
            client.disconnect()
            _recording_state = {"active": False, "start_time": None}

        alpaca = get_alpaca_client()
        if alpaca:
            alpaca.disconnect()

        # Experimental sun-centering loop must stop when hardware disconnects.
        stop_sun_center_service()

        logger.info("[Telescope] Disconnected from telescope")
        return (
            jsonify(
                {
                    "success": True,
                    "connected": False,
                    "message": "Disconnected from telescope",
                }
            ),
            200,
        )

    except Exception as e:
        return handle_error(e)


_eclipse_cache: dict = {"data": None, "ts": 0.0}
_ECLIPSE_CACHE_TTL = 60.0  # seconds


def get_telescope_status():
    """GET /telescope/status - Get current telescope status."""
    logger.debug("[Telescope] GET /telescope/status")

    def _get_eclipse_data():
        """Return upcoming eclipse dict or None (never raises). Cached 60 s."""
        import time as _time

        now = _time.monotonic()
        if now - _eclipse_cache["ts"] < _ECLIPSE_CACHE_TTL:
            return _eclipse_cache["data"]
        try:
            from src.eclipse_monitor import get_eclipse_monitor

            lat, lon, elev = get_observer_coordinates()
            result = get_eclipse_monitor().get_upcoming_eclipse(lat, lon, elev)
        except Exception as ex:
            logger.warning(f"[Telescope] Eclipse check failed: {ex}")
            result = None
        _eclipse_cache["data"] = result
        _eclipse_cache["ts"] = now
        return result

    try:
        client = get_telescope_client()

        if not client or not client.is_connected():
            with _motor_ctrl.lock:
                ctrl_state = _motor_ctrl.state.value
            return (
                jsonify(
                    {
                        "connected": False,
                        "recording": False,
                        "enabled": is_enabled(),
                        "mock_mode": is_mock_mode(),
                        "host": (
                            os.getenv("SEESTAR_HOST")
                            if not is_mock_mode()
                            else "mock.telescope"
                        ),
                        "port": int(os.getenv("SEESTAR_PORT", "4700")),
                        "ctrl_state": ctrl_state,
                        "eclipse": _get_eclipse_data(),
                        "focus_pos": None,
                        "focus_pos_source": None,
                        "camera_gain": None,
                        "camera_gain_min": None,
                        "camera_gain_max": None,
                        "provider": "none",
                        "provider_ready": False,
                        "provider_error": "JSON-RPC client not connected",
                    }
                ),
                200,
            )

        backend_recording = bool(getattr(client, "_recording", False))
        recording_active = _recording_state["active"] or backend_recording

        # Keep focus odometer fresh only when JSON-RPC query probes are explicitly enabled.
        if (
            not is_mock_mode()
            and getattr(client, "_query_rpc_enabled", False)
            and hasattr(client, "refresh_focus_throttled")
        ):
            try:
                client.refresh_focus_throttled(min_interval_sec=3.0)
            except Exception as _fe:
                logger.debug(f"[Telescope] refresh_focus_throttled: {_fe}")

        # Merge ALPACA state if available
        alpaca = get_alpaca_client()
        alpaca_status = None
        if alpaca and alpaca.is_connected():
            alpaca.refresh_aux_state_throttled(min_interval_sec=8.0)
            alpaca_status = alpaca.get_status()

        abs_fp = getattr(client, "_focus_pos", None)
        rel_fp = getattr(client, "_focus_relative_odometer", None)

        def _coerce_int(v):
            if v is None:
                return None
            if isinstance(v, bool):
                return None
            if isinstance(v, int):
                return v
            if isinstance(v, float):
                return int(round(v))
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    return None
                try:
                    return int(s)
                except ValueError:
                    try:
                        return int(round(float(s)))
                    except ValueError:
                        return None
            return None

        _raw_fp = abs_fp if abs_fp is not None else rel_fp
        _fp_json = _coerce_int(_raw_fp)
        _focus_src = (
            "absolute"
            if abs_fp is not None
            else ("relative" if rel_fp is not None else None)
        )
        if alpaca_status and alpaca_status.get("focuser_position") is not None:
            _raw_fp = alpaca_status.get("focuser_position")
            _fp_alpaca = _coerce_int(_raw_fp)
            if _fp_alpaca is not None:
                _fp_json = _fp_alpaca
                _focus_src = "alpaca_absolute"

        cam_gain = None
        cam_gain_min = None
        cam_gain_max = None
        if alpaca_status:
            cam_gain = _coerce_int(alpaca_status.get("camera_gain"))
            cam_gain_min = _coerce_int(alpaca_status.get("camera_gain_min"))
            cam_gain_max = _coerce_int(alpaca_status.get("camera_gain_max"))
        if cam_gain is None:
            cam_gain = _coerce_int(getattr(client, "_camera_gain", None))

        status = {
            "connected": client.is_connected(),
            "recording": recording_active,
            "viewing_mode": getattr(client, "_viewing_mode", None),
            "host": client.host,
            "port": client.port,
            "enabled": is_enabled(),
            "mock_mode": is_mock_mode(),
            "eclipse": _get_eclipse_data(),
            "alpaca": alpaca_status,
            "focus_pos": _fp_json,
            "focus_pos_source": _focus_src,
            "camera_gain": cam_gain,
            "camera_gain_min": cam_gain_min,
            "camera_gain_max": cam_gain_max,
            "provider": "alpaca" if alpaca_status else "jsonrpc",
            "provider_ready": bool(alpaca_status) or bool(client.is_connected()),
            "provider_error": (
                None
                if alpaca_status
                else "ALPACA not connected — motor telemetry unavailable"
            ),
            "jsonrpc_query_enabled": bool(getattr(client, "_query_rpc_enabled", False)),
        }
        with _motor_ctrl.lock:
            status["ctrl_state"] = _motor_ctrl.state.value
        if recording_active and _recording_state["start_time"]:
            status["recording_duration"] = (
                datetime.now() - _recording_state["start_time"]
            ).total_seconds()

        return jsonify(status), 200

    except Exception as e:
        # Status endpoint should never fail, return error state
        logger.error(f"[Telescope] Status check error: {e}")
        return (
            jsonify(
                {
                    "connected": False,
                    "recording": False,
                    "enabled": is_enabled(),
                    "error": str(e),
                    "eclipse": None,
                    "focus_pos": None,
                    "focus_pos_source": None,
                    "camera_gain": None,
                    "camera_gain_min": None,
                    "camera_gain_max": None,
                    "provider": "none",
                    "provider_ready": False,
                }
            ),
            200,
        )


# Viewing Mode Endpoints


def get_current_target():
    """GET /telescope/target - Get current target based on time of day."""
    from datetime import datetime

    hour = datetime.now().hour
    is_daytime = 6 <= hour < 18

    return (
        jsonify({"target": "sun" if is_daytime else "moon", "is_daytime": is_daytime}),
        200,
    )


# Recording Endpoints


def start_recording():
    """POST /telescope/recording/start - Start video recording from RTSP stream."""
    logger.info("[Telescope] POST /telescope/recording/start")

    try:
        global _recording_state

        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        rtsp_url = _ensure_rtsp_ready(client)
        if not rtsp_url:
            return (
                jsonify(
                    {
                        "error": "RTSP stream is not ready. Set scope to Sun/Moon view and try again."
                    }
                ),
                503,
            )

        if _recording_state["active"]:
            return (
                jsonify({"error": "Recording already in progress", "recording": True}),
                409,
            )

        # Get parameters from request
        duration = 30  # default
        interval = 0  # default (normal video)

        if request.is_json:
            duration = int(request.json.get("duration", 30))
            interval = float(request.json.get("interval", 0))

        # Generate filename with timestamp
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mode_suffix = f"_timelapse_{interval}s" if interval > 0 else ""
        filename = f"vid_{timestamp}{mode_suffix}.mp4"

        # Create year/month directories
        now = datetime.now()
        year_month_path = os.path.join(
            "static/captures", str(now.year), f"{now.month:02d}"
        )
        os.makedirs(year_month_path, exist_ok=True)

        filepath = os.path.join(year_month_path, filename)

        logger.info(
            f"[Telescope] Starting recording: {filepath} (duration={duration}s, interval={interval}s)"
        )

        # Common RTSP input options — keep the connection alive for the
        # full recording duration (Seestar closes idle streams quickly).
        rtsp_input = [
            FFMPEG,
            "-rtsp_transport",
            "tcp",
            "-timeout",
            str((duration + 30) * 1000000),  # socket I/O timeout (µs)
            "-i",
            rtsp_url,
            "-t",
            str(duration),
        ]

        # Build FFmpeg command
        if interval > 0:
            # Timelapse mode: capture frames at specified interval.
            # -g 1 makes every frame a keyframe (timelapse has few frames).
            cmd = rtsp_input + [
                "-vf",
                f"fps=1/{interval}",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "1",
                "-an",
                "-movflags",
                "+faststart",
                "-y",
                filepath,
            ]
        else:
            # Normal video mode: re-encode to H.264 with proper keyframes.
            # Previous approach used -c copy -movflags frag_keyframe+empty_moov
            # which produced fragmented MP4 with an empty moov atom — editors
            # could not parse it (reported 0/0 frames).
            cmd = rtsp_input + [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "30",
                "-an",
                "-movflags",
                "+faststart",
                "-y",
                filepath,
            ]

        # Start FFmpeg in background
        process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )

        _recording_state = {
            "active": True,
            "start_time": datetime.now(),
            "process": process,
            "filepath": filepath,
            "filename": filename,
            "duration": duration,
        }

        logger.info(f"[Telescope] Recording started (PID: {process.pid})")
        return (
            jsonify(
                {
                    "success": True,
                    "recording": True,
                    "start_time": _recording_state["start_time"].isoformat(),
                    "duration": duration,
                    "interval": interval,
                    "message": "Recording started",
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"[Telescope] Failed to start recording: {e}", exc_info=True)
        return handle_error(e)


def stop_recording():
    """POST /telescope/recording/stop - Stop video recording."""
    logger.info("[Telescope] POST /telescope/recording/stop")

    try:
        global _recording_state

        if not _recording_state["active"]:
            return (
                jsonify({"error": "No recording in progress", "recording": False}),
                400,
            )

        # Calculate duration
        duration = 0
        if _recording_state["start_time"]:
            duration = (datetime.now() - _recording_state["start_time"]).total_seconds()

        # Terminate FFmpeg process gracefully so it can finalize the file
        ffmpeg_stderr = b""
        if "process" in _recording_state and _recording_state["process"]:
            process = _recording_state["process"]
            try:
                process.send_signal(__import__("signal").SIGTERM)
            except Exception:
                process.terminate()
            try:
                _, ffmpeg_stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                _, ffmpeg_stderr = process.communicate()
            if ffmpeg_stderr:
                stderr_tail = ffmpeg_stderr.decode(errors="replace")[-500:]
                logger.info(f"[Telescope] FFmpeg stderr (tail): {stderr_tail}")
            logger.info("[Telescope] FFmpeg process terminated")

        filename = _recording_state.get("filename", "unknown")
        filepath = _recording_state.get("filepath", "")

        # Create metadata + thumbnail
        if filepath and os.path.exists(filepath):
            metadata = {
                "timestamp": (
                    _recording_state["start_time"].isoformat()
                    if _recording_state["start_time"]
                    else None
                ),
                "duration": duration,
                "source": "rtsp_stream",
                "type": "video",
            }

            metadata_path = filepath.rsplit(".", 1)[0] + ".json"
            with open(metadata_path, "w") as f:
                import json

                json.dump(metadata, f, indent=2)

            # Generate thumbnail from first frame
            thumb_path = filepath.rsplit(".", 1)[0] + "_thumb.jpg"
            try:
                result = subprocess.run(
                    [
                        FFMPEG,
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
                if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                    rel_thumb = os.path.relpath(thumb_path, "static")
                    f"/static/{rel_thumb.replace(os.sep, '/')}"
                    logger.info(f"[Telescope] Thumbnail generated: {thumb_path}")
                else:
                    logger.warning(
                        f"[Telescope] Thumbnail not created — ffmpeg stderr: "
                        f"{result.stderr.decode(errors='replace')[-200:]}"
                    )
            except Exception as te:
                logger.warning(f"[Telescope] Thumbnail generation failed: {te}")

        _recording_state = {"active": False, "start_time": None}

        logger.info(
            f"[Telescope] Recording stopped: {filename} (duration: {duration:.1f}s)"
        )
        return (
            jsonify(
                {
                    "success": True,
                    "recording": False,
                    "duration": duration,
                    "filename": filename,
                    "message": "Recording stopped",
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"[Telescope] Failed to stop recording: {e}", exc_info=True)
        return handle_error(e)


def get_recording_status():
    """GET /telescope/recording/status - Get current recording status."""
    logger.debug("[Telescope] GET /telescope/recording/status")

    try:
        status = {"recording": _recording_state["active"]}

        if _recording_state["active"] and _recording_state["start_time"]:
            status["duration"] = (
                datetime.now() - _recording_state["start_time"]
            ).total_seconds()
            status["start_time"] = _recording_state["start_time"].isoformat()

        return jsonify(status), 200

    except Exception as e:
        return handle_error(e)


# ── Solar Timelapse Endpoints ──────────────────────────────────────────


def start_timelapse():
    """POST /telescope/timelapse/start — begin day-long solar timelapse."""
    logger.info("[Telescope] POST /telescope/timelapse/start")
    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        if not _ensure_rtsp_ready(client):
            return (
                jsonify(
                    {
                        "error": "RTSP stream is not ready. Ensure Sun/Moon view is active before timelapse."
                    }
                ),
                503,
            )

        data = request.get_json(silent=True) or {}
        interval = float(data.get("interval", 120))

        tl = get_timelapse()
        result = tl.start(host=client.host, interval=interval)
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result), 200
    except Exception as e:
        return handle_error(e)


def stop_timelapse():
    """POST /telescope/timelapse/stop — stop timelapse and assemble video."""
    logger.info("[Telescope] POST /telescope/timelapse/stop")
    try:
        tl = get_timelapse()
        result = tl.stop(assemble=True)
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result), 200
    except Exception as e:
        return handle_error(e)


def get_timelapse_status():
    """GET /telescope/timelapse/status — current timelapse state."""
    try:
        tl = get_timelapse()
        if not tl.is_running:
            auto_resume = (
                os.getenv("SOLAR_TIMELAPSE_AUTO_RESUME", "true").strip().lower()
            )
            if auto_resume in ("1", "true", "yes", "on") and tl.has_today_frames():
                client = get_telescope_client()
                if client and client.is_connected():
                    interval_raw = os.getenv("SOLAR_TIMELAPSE_INTERVAL", "120")
                    try:
                        resume_interval = float(interval_raw)
                    except ValueError:
                        resume_interval = 120.0
                    tl.resume_today(host=client.host, interval=resume_interval)
        return jsonify(tl.status()), 200
    except Exception as e:
        return handle_error(e)


def update_timelapse_settings():
    """PATCH /telescope/timelapse/settings — update interval mid-capture."""
    logger.info("[Telescope] PATCH /telescope/timelapse/settings")
    try:
        data = request.get_json(silent=True) or {}
        tl = get_timelapse()
        if "interval" in data:
            tl.update_interval(float(data["interval"]))
        if "smoothing" in data:
            tl.update_smoothing(float(data["smoothing"]))
        if "interval" not in data and "smoothing" not in data:
            return jsonify({"error": "interval or smoothing required"}), 400
        return jsonify(tl.status()), 200
    except Exception as e:
        return handle_error(e)


def pause_timelapse():
    """POST /telescope/timelapse/pause — pause capture for transit event."""
    try:
        tl = get_timelapse()
        reason = (request.get_json(silent=True) or {}).get("reason", "transit")
        tl.pause(reason)
        return jsonify(tl.status()), 200
    except Exception as e:
        return handle_error(e)


def resume_timelapse():
    """POST /telescope/timelapse/resume — resume after transit event."""
    try:
        tl = get_timelapse()
        tl.resume()
        return jsonify(tl.status()), 200
    except Exception as e:
        return handle_error(e)


def preview_timelapse():
    """POST /telescope/timelapse/preview — build preview video from frames so far."""
    logger.info("[Telescope] POST /telescope/timelapse/preview")
    try:
        tl = get_timelapse()
        url = tl.build_preview()
        if url:
            return jsonify({"success": True, "url": url}), 200
        return jsonify({"error": "Not enough frames for preview (need ≥2)"}), 400
    except Exception as e:
        return handle_error(e)


# File Management Endpoint


def _find_video_thumbnail(full_path: str):
    """Return URL for a video's _thumb.jpg if it exists, else None."""
    if not full_path.lower().endswith((".mp4", ".avi", ".mov")):
        return None
    thumb_path = full_path.rsplit(".", 1)[0] + "_thumb.jpg"
    if os.path.exists(thumb_path):
        rel = os.path.relpath(thumb_path, "static")
        return f"/static/{rel.replace(os.sep, '/')}"
    return None


def _find_companion(full_path: str, suffix: str):
    """Return URL for a companion file (e.g. _diff.jpg, _frame.jpg) if it exists."""
    base = full_path.rsplit(".", 1)[0]
    companion = base + suffix
    if os.path.exists(companion):
        rel = os.path.relpath(companion, "static").replace(os.sep, "/")
        return f"/static/{rel}"
    return None


def _is_timelapse_frame(full_path: str) -> bool:
    """True for raw/annotated per-frame JPEGs inside timelapse frame folders."""
    name = os.path.basename(full_path).lower()
    if not (name.startswith("frame_") and name.endswith(".jpg")):
        return False
    norm = full_path.replace("\\", "/").lower()
    return "/timelapse_" in norm


def _read_timelapse_metadata_for_video(full_path: str) -> dict:
    """Return timelapse metadata for a video, if a sidecar JSON exists."""
    if not full_path.lower().endswith(".mp4"):
        return {}
    candidates = [full_path.rsplit(".", 1)[0] + ".json"]
    # Annotated output is timelapse_YYYYMMDD_sunspots.mp4 while metadata is
    # written to timelapse_YYYYMMDD.json.
    if full_path.lower().endswith("_sunspots.mp4"):
        candidates.append(full_path[: -len("_sunspots.mp4")] + ".json")
    for meta_path in candidates:
        if not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            if isinstance(meta, dict) and meta.get("type") == "timelapse":
                return {
                    "timelapse_frame_count": meta.get("frame_count"),
                    "timelapse_interval_seconds": meta.get("interval_seconds"),
                }
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def list_telescope_files():
    """GET /telescope/files - List locally captured files."""
    logger.info("[Telescope] GET /telescope/files")

    try:
        # List files from local captures directory
        captures_path = "static/captures"
        files = []

        if os.path.exists(captures_path):
            # Walk through captures directory
            for root, dirs, filenames in os.walk(captures_path):
                for filename in filenames:
                    if (
                        filename.lower().endswith(
                            (".jpg", ".jpeg", ".png", ".mp4", ".avi")
                        )
                        and "_thumb." not in filename.lower()
                        and "_tmp."
                        not in filename.lower()  # skip in-progress temp files
                        and "_diff." not in filename.lower()  # skip diff heatmaps
                        and "_frame." not in filename.lower()  # skip trigger frames
                        and not filename.lower().endswith(
                            "_analysis.json"
                        )  # skip sidecar JSON
                    ):
                        full_path = os.path.join(root, filename)
                        if _is_timelapse_frame(full_path):
                            continue
                        rel_path = os.path.relpath(full_path, "static")

                        # Get file modification time
                        mtime = os.path.getmtime(full_path)

                        item = {
                            "name": filename,
                            "url": f"/static/{rel_path.replace(os.sep, '/')}",
                            "mtime": mtime,
                            "thumbnail": _find_video_thumbnail(full_path),
                            "diff_heatmap": _find_companion(full_path, "_diff.jpg"),
                            "trigger_frame": _find_companion(full_path, "_frame.jpg"),
                        }
                        item.update(_read_timelapse_metadata_for_video(full_path))
                        files.append(item)

        # Sort by modification time (newest first)
        files.sort(key=lambda x: x["mtime"], reverse=True)

        logger.info(f"[Telescope] Retrieved {len(files)} local files")
        response = jsonify(
            {"files": files, "total": len(files), "source": "local_captures"}
        )
        response.headers["Cache-Control"] = "no-store"
        return response, 200

    except Exception as e:
        logger.error(f"[Telescope] Error listing files: {e}")
        return handle_error(e)


def get_telescope_favorites():
    """GET /telescope/files/favorites - Return persisted favorite capture URLs."""
    try:
        with _favorites_lock:
            favorites = _load_telescope_favorites()
        return jsonify({"favorites": favorites}), 200
    except Exception as e:
        logger.error(f"[Telescope] Error loading favorites: {e}")
        return handle_error(e)


def save_telescope_favorites():
    """POST /telescope/files/favorites - Persist favorite capture URLs."""
    try:
        payload = request.get_json(silent=True) or {}
        items = payload.get("favorites")
        if not isinstance(items, list):
            return jsonify({"error": "favorites must be a list"}), 400

        # Preserve order but de-duplicate after normalisation.
        dedup: "OrderedDict[str, None]" = OrderedDict()
        for item in items:
            norm = _normalize_favorite_url(item)
            if norm:
                dedup[norm] = None
        favorites = list(dedup.keys())

        with _favorites_lock:
            _save_telescope_favorites(favorites)

        return jsonify({"ok": True, "favorites": favorites}), 200
    except Exception as e:
        logger.error(f"[Telescope] Error saving favorites: {e}")
        return handle_error(e)


def get_telescope_file_frame():
    """GET /telescope/files/frame - Extract a specific video frame as JPEG."""
    try:
        file_path = (request.args.get("path") or "").strip()
        frame_raw = (request.args.get("frame") or "").strip()
        fps_raw = (request.args.get("fps") or "30").strip()
        max_width_raw = (request.args.get("max_width") or "").strip()

        if not file_path:
            return jsonify({"error": "Missing 'path' parameter"}), 400
        if frame_raw == "":
            return jsonify({"error": "Missing 'frame' parameter"}), 400

        try:
            frame_idx = int(frame_raw)
        except ValueError:
            return jsonify({"error": "Invalid frame index"}), 400
        if frame_idx < 0:
            return jsonify({"error": "Frame index must be >= 0"}), 400

        try:
            fps = float(fps_raw)
        except ValueError:
            fps = 30.0
        fps = max(1.0, min(120.0, fps))

        # Optional max_width for display-resolution extraction
        max_width = 0
        if max_width_raw:
            try:
                max_width = int(max_width_raw)
            except ValueError:
                max_width = 0
            max_width = max(0, min(3840, max_width))

        # Security: constrain to captures directory
        full_path = os.path.join("static", file_path)
        abs_path = os.path.abspath(full_path)
        captures_abs = os.path.abspath("static/captures")
        if not abs_path.startswith(captures_abs):
            return jsonify({"error": "Invalid file path"}), 403
        if not os.path.exists(abs_path):
            return jsonify({"error": "File not found"}), 404
        if not abs_path.lower().endswith((".mp4", ".avi", ".mov", ".mkv", ".webm")):
            return jsonify({"error": "Not a video file"}), 400

        ts = frame_idx / fps

        # Build optional scale filter for display-resolution extraction
        scale_part = (
            f",scale={max_width}:-2" if max_width > 0 else ""
        )

        # Exact frame index extraction first (deterministic for short clips),
        # then timestamp-based fallbacks.
        cmds = [
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-hwaccel",
                "none",
                "-i",
                abs_path,
                "-an",
                "-sn",
                "-dn",
                "-vf",
                f"select=eq(n\\,{frame_idx}){scale_part}",
                "-frames:v",
                "1",
                "-q:v",
                "4",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-",
            ],
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-hwaccel",
                "none",
                "-i",
                abs_path,
                "-an",
                "-sn",
                "-dn",
                "-ss",
                f"{ts:.6f}",
                "-vf",
                f"null{scale_part}" if scale_part else "null",
                "-frames:v",
                "1",
                "-q:v",
                "4",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-",
            ],
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-hwaccel",
                "none",
                "-ss",
                f"{ts:.6f}",
                "-i",
                abs_path,
                "-an",
                "-sn",
                "-dn",
                "-vf",
                f"null{scale_part}" if scale_part else "null",
                "-frames:v",
                "1",
                "-q:v",
                "4",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-",
            ],
        ]

        jpeg = b""
        last_err = ""
        for cmd in cmds:
            proc = subprocess.run(cmd, capture_output=True, timeout=12)
            if proc.returncode == 0 and proc.stdout:
                jpeg = proc.stdout
                break
            last_err = (proc.stderr or b"").decode(errors="replace")[-200:]

        if not jpeg:
            logger.warning(f"[Telescope] Frame extract failed for {file_path}: {last_err}")
            return jsonify({"error": "Failed to extract frame"}), 500

        resp = Response(jpeg, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        return resp, 200

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Frame extraction timed out"}), 504
    except Exception as e:
        logger.error(f"[Telescope] Error extracting frame: {e}")
        return handle_error(e)


def _parse_rate(rate_raw: str) -> float:
    if not rate_raw:
        return 0.0
    rate_raw = str(rate_raw).strip()
    if "/" in rate_raw:
        num_s, den_s = rate_raw.split("/", 1)
        try:
            num = float(num_s)
            den = float(den_s)
            return (num / den) if den else 0.0
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(rate_raw)
    except (TypeError, ValueError):
        return 0.0


def get_telescope_video_info():
    """GET /telescope/files/video-info - Return fps/duration/frame_count for a video."""
    try:
        file_path = (request.args.get("path") or "").strip()
        if not file_path:
            return jsonify({"error": "Missing 'path' parameter"}), 400

        full_path = os.path.join("static", file_path)
        abs_path = os.path.abspath(full_path)
        captures_abs = os.path.abspath("static/captures")
        if not abs_path.startswith(captures_abs):
            return jsonify({"error": "Invalid file path"}), 403
        if not os.path.exists(abs_path):
            return jsonify({"error": "File not found"}), 404
        if not abs_path.lower().endswith((".mp4", ".avi", ".mov", ".mkv", ".webm")):
            return jsonify({"error": "Not a video file"}), 400

        cmd = [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate,nb_frames,duration",
            "-of",
            "json",
            abs_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=8)
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace")[-200:]
            return jsonify({"error": f"ffprobe failed: {err}"}), 500

        payload = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}")
        streams = payload.get("streams") or []
        if not streams:
            return jsonify({"error": "No video stream found"}), 500
        stream = streams[0] or {}

        fps = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate"))
        duration = 0.0
        try:
            duration = float(stream.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0

        frame_count = 0
        try:
            frame_count = int(stream.get("nb_frames") or 0)
        except (TypeError, ValueError):
            frame_count = 0
        if frame_count <= 0 and duration > 0 and fps > 0:
            frame_count = int(round(duration * fps))

        return (
            jsonify(
                {
                    "success": True,
                    "fps": fps,
                    "duration": duration,
                    "frame_count": frame_count,
                }
            ),
            200,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Video info probe timed out"}), 504
    except Exception as e:
        logger.error(f"[Telescope] Error probing video info: {e}")
        return handle_error(e)


def delete_telescope_file():
    """POST /telescope/files/delete - Delete a captured file."""
    logger.info("[Telescope] POST /telescope/files/delete")

    try:
        if not request.is_json:
            return jsonify({"error": "Request must be JSON"}), 400

        file_path = request.json.get("path")
        if not file_path:
            return jsonify({"error": "Missing 'path' parameter"}), 400

        # Security: ensure path is within captures directory
        full_path = os.path.join("static", file_path)
        abs_path = os.path.abspath(full_path)
        captures_abs = os.path.abspath("static/captures")

        if not abs_path.startswith(captures_abs):
            logger.warning(
                f"[Telescope] Attempted to delete file outside captures: {file_path}"
            )
            return jsonify({"error": "Invalid file path"}), 403

        # Delete image file
        if os.path.exists(abs_path):
            os.remove(abs_path)
            logger.info(f"[Telescope] Deleted file: {file_path}")
        else:
            return jsonify({"error": "File not found"}), 404

        # Delete metadata file if exists
        metadata_path = abs_path.rsplit(".", 1)[0] + ".json"
        if os.path.exists(metadata_path):
            os.remove(metadata_path)
            logger.info(f"[Telescope] Deleted metadata: {metadata_path}")

        # Delete thumbnail if exists
        thumb_path = abs_path.rsplit(".", 1)[0] + "_thumb.jpg"
        if os.path.exists(thumb_path):
            os.remove(thumb_path)

        # Delete analyzed composite and sidecar if they exist (e.g. analyzed_vid_xxx.jpg)
        stem = os.path.splitext(abs_path)[0]
        base_dir = os.path.dirname(stem)
        base_name = os.path.basename(stem)
        for ext in (".jpg", "_analysis.json"):
            analyzed = os.path.join(base_dir, "analyzed_" + base_name + ext)
            if os.path.exists(analyzed):
                os.remove(analyzed)
                logger.info(f"[Telescope] Deleted analyzed artifact: {analyzed}")

        return (
            jsonify(
                {"success": True, "message": f"Deleted {os.path.basename(file_path)}"}
            ),
            200,
        )

    except Exception as e:
        logger.error(f"[Telescope] Error deleting file: {e}")
        return handle_error(e)


def rename_telescope_file():
    """POST /telescope/files/rename - Rename a captured file in place."""
    logger.info("[Telescope] POST /telescope/files/rename")

    try:
        if not request.is_json:
            return jsonify({"error": "Request must be JSON"}), 400

        file_path = str(request.json.get("path", "")).strip()
        new_name = str(request.json.get("new_name", "")).strip()
        if not file_path:
            return jsonify({"error": "Missing 'path' parameter"}), 400
        if not new_name:
            return jsonify({"error": "Missing 'new_name' parameter"}), 400

        # Disallow path traversal and nested paths in new name.
        if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
            return jsonify({"error": "Invalid target filename"}), 400

        full_path = os.path.join("static", file_path)
        abs_old = os.path.abspath(full_path)
        captures_abs = os.path.abspath("static/captures")

        if not abs_old.startswith(captures_abs):
            return jsonify({"error": "Invalid file path"}), 403
        if not os.path.exists(abs_old):
            return jsonify({"error": "File not found"}), 404

        old_dir = os.path.dirname(abs_old)
        old_ext = os.path.splitext(abs_old)[1]

        stem_in, ext_in = os.path.splitext(new_name)
        if not stem_in:
            return jsonify({"error": "Invalid target filename"}), 400
        if ext_in and ext_in.lower() != old_ext.lower():
            return jsonify({"error": f"File extension must remain {old_ext}"}), 400
        if not ext_in:
            new_name = f"{new_name}{old_ext}"

        abs_new = os.path.abspath(os.path.join(old_dir, new_name))
        if not abs_new.startswith(captures_abs):
            return jsonify({"error": "Invalid target path"}), 403
        if abs_new == abs_old:
            rel = os.path.relpath(abs_old, "static").replace(os.sep, "/")
            return jsonify({"success": True, "name": os.path.basename(abs_old), "path": rel, "url": f"/static/{rel}"}), 200
        if os.path.exists(abs_new):
            return jsonify({"error": "A file with that name already exists"}), 409

        os.rename(abs_old, abs_new)

        # Rename companion assets with matching stem if present.
        old_stem = os.path.splitext(abs_old)[0]
        new_stem = os.path.splitext(abs_new)[0]
        for suffix in (".json", "_thumb.jpg", "_diff.jpg", "_frame.jpg", "_analysis.json", "_analyzed.mp4"):
            src = old_stem + suffix
            dst = new_stem + suffix
            if os.path.exists(src) and not os.path.exists(dst):
                os.rename(src, dst)

        # Rename analyzed artifacts (analyzed_<stem>.jpg / analyzed_<stem>_analysis.json).
        old_base = os.path.basename(old_stem)
        new_base = os.path.basename(new_stem)
        base_dir = os.path.dirname(old_stem)
        for suffix in (".jpg", "_analysis.json"):
            src = os.path.join(base_dir, f"analyzed_{old_base}{suffix}")
            dst = os.path.join(base_dir, f"analyzed_{new_base}{suffix}")
            if os.path.exists(src) and not os.path.exists(dst):
                os.rename(src, dst)

        rel = os.path.relpath(abs_new, "static").replace(os.sep, "/")
        return (
            jsonify(
                {
                    "success": True,
                    "message": f"Renamed to {os.path.basename(abs_new)}",
                    "name": os.path.basename(abs_new),
                    "path": rel,
                    "url": f"/static/{rel}",
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"[Telescope] Error renaming file: {e}", exc_info=True)
        return handle_error(e)


# Video Analysis Endpoint


def analyze_file():
    """POST /telescope/files/analyze - Run transit analyzer on a saved MP4."""
    logger.info("[Telescope] POST /telescope/files/analyze")

    try:
        if not request.is_json:
            return jsonify({"error": "Request must be JSON"}), 400

        file_path = request.json.get("path")
        if not file_path:
            return jsonify({"error": "Missing 'path' parameter"}), 400

        # Security: only allow files inside static/captures
        full_path = os.path.join("static", file_path)
        abs_path = os.path.abspath(full_path)
        captures_abs = os.path.abspath("static/captures")

        if not abs_path.startswith(captures_abs):
            return jsonify({"error": "Invalid file path"}), 403

        if not os.path.exists(abs_path):
            return jsonify({"error": "File not found"}), 404

        if not abs_path.lower().endswith(".mp4"):
            return jsonify({"error": "Only MP4 files can be analyzed"}), 400

        from src.transit_analyzer import analyze_video

        # Optional tuning parameters from frontend sliders
        req = request.json
        diff_threshold = req.get("diff_threshold")
        min_blob_pixels = req.get("min_blob_pixels")
        disk_margin_pct = req.get("disk_margin_pct")
        target = req.get("target", "auto")
        max_positions = req.get("max_positions")

        def _run():
            try:
                result = analyze_video(
                    abs_path,
                    output_annotated=True,
                    diff_threshold=(
                        int(diff_threshold) if diff_threshold is not None else None
                    ),
                    min_blob_pixels=(
                        int(min_blob_pixels) if min_blob_pixels is not None else None
                    ),
                    disk_margin_pct=(
                        float(disk_margin_pct) if disk_margin_pct is not None else None
                    ),
                    target=target,
                    max_positions=(
                        int(max_positions) if max_positions is not None else None
                    ),
                )
                return result
            except Exception as exc:
                logger.error(f"[Analyzer] Error: {exc}")
                raise

        result = _run()

        base = os.path.splitext(file_path)[0].replace("analyzed_", "")
        folder = os.path.dirname(base)
        stem = os.path.basename(base)
        return (
            jsonify(
                {
                    "success": True,
                    "disk_detected": result.disk_detected,
                    "duration": result.duration_seconds,
                    "transit_events": result.transit_events,
                    "transit_positions": result.transit_positions,
                    "detection_count": len(result.detections),
                    "static_detections": sum(
                        1 for d in result.detections if d.is_static
                    ),
                    "composite_image": folder + "/analyzed_" + stem + ".jpg",
                    "annotated_file": folder + "/analyzed_" + stem + ".jpg",
                    "sidecar_file": folder + "/analyzed_" + stem + "_analysis.json",
                    "error": result.error,
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"[Telescope] Analysis error: {e}")
        return handle_error(e)


def isolate_transit_route():
    """POST /telescope/files/isolate-transit

    Lightweight transit-frame isolation for short det_*.mp4 clips.
    Returns per-frame darkness scores and transit spans without needing
    a full disk-detection analysis.

    Body: { path: "captures/...", peak_time_s: <float|null> }
    """
    logger.info("[Telescope] POST /telescope/files/isolate-transit")
    try:
        req = request.get_json(force=True) if request.is_json else {}
        rel_path = req.get("path", "")
        if not rel_path:
            return jsonify({"error": "Missing path"}), 400

        captures_abs = os.path.abspath("static/captures")
        abs_path = os.path.abspath(os.path.join("static", rel_path))
        if not abs_path.startswith(captures_abs):
            return jsonify({"error": "Invalid file path"}), 403
        if not os.path.exists(abs_path):
            return jsonify({"error": "File not found"}), 404
        if not abs_path.lower().endswith(".mp4"):
            return jsonify({"error": "Only MP4 files supported"}), 400

        from src.transit_analyzer import isolate_transit_frames

        peak_time_s = req.get("peak_time_s")
        if peak_time_s is not None:
            try:
                peak_time_s = float(peak_time_s)
            except (TypeError, ValueError):
                peak_time_s = None

        result = isolate_transit_frames(abs_path, peak_time_s=peak_time_s)
        return jsonify(result), 200

    except Exception as exc:
        logger.error(f"[Telescope] isolate-transit error: {exc}")
        return handle_error(exc)


def video_fps_route():
    """GET /api/video/fps?path=captures/...

    Probe an MP4's video-stream fps via ffprobe. Returns {"fps": <float>} on
    success, or 404 {"error": "..."} on any failure (missing file, ffprobe
    absent, parse error). The frontend falls back to 30 fps on non-200.
    """
    rel_path = request.args.get("path", "")
    if not rel_path:
        return jsonify({"error": "Missing path"}), 400

    captures_abs = os.path.abspath("static/captures")
    abs_path = os.path.abspath(os.path.join("static", rel_path))
    if not abs_path.startswith(captures_abs):
        return jsonify({"error": "Invalid file path"}), 403
    if not os.path.exists(abs_path):
        return jsonify({"error": "File not found"}), 404
    if not abs_path.lower().endswith(".mp4"):
        return jsonify({"error": "Only MP4 files supported"}), 400

    ffprobe = os.getenv("FFPROBE_PATH", "") or "ffprobe"
    # Derive ffprobe from ffmpeg path when FFMPEG is an absolute path and
    # ffprobe sits next to it (common for bundled builds).
    if FFMPEG and os.path.isabs(FFMPEG):
        candidate = os.path.join(os.path.dirname(FFMPEG), "ffprobe")
        if os.path.isfile(candidate):
            ffprobe = candidate

    try:
        r = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=r_frame_rate",
                "-of",
                "csv=p=0",
                abs_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
        )
        if r.returncode != 0:
            return jsonify({"error": "ffprobe failed"}), 404
        raw = r.stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            return jsonify({"error": "empty ffprobe output"}), 404
        if "/" in raw:
            num, den = raw.split("/", 1)
            fps = float(num) / float(den) if float(den) != 0 else 0.0
        else:
            fps = float(raw)
        if fps <= 0 or fps > 1000:
            return jsonify({"error": "implausible fps"}), 404
        return jsonify({"fps": fps}), 200
    except FileNotFoundError:
        return jsonify({"error": "ffprobe not installed"}), 404
    except subprocess.TimeoutExpired:
        return jsonify({"error": "ffprobe timeout"}), 404
    except Exception as exc:
        logger.warning(f"[Telescope] video fps probe error: {exc}")
        return jsonify({"error": str(exc)}), 404


def composite_from_frames_route():
    """POST /telescope/files/composite-from-frames

    Build a composite image from user-selected video frames.
    Body: { path: "captures/...", frame_indices: [0, 15, 30], fps: 30, target: "sun" }
    """
    logger.info("[Telescope] POST /telescope/files/composite-from-frames")
    try:
        req = request.get_json(force=True) if request.is_json else {}
        rel_path = req.get("path", "")
        if not rel_path:
            return jsonify({"error": "Missing path"}), 400

        captures_abs = os.path.abspath("static/captures")
        file_path = os.path.abspath(os.path.join("static", rel_path))
        if not file_path.startswith(captures_abs):
            return jsonify({"error": "Forbidden"}), 403
        if not os.path.exists(file_path):
            return jsonify({"error": "File not found"}), 404

        frame_indices = req.get("frame_indices", [])
        if not frame_indices or not isinstance(frame_indices, list):
            return jsonify({"error": "No frame_indices provided"}), 400

        frame_indices = [int(f) for f in frame_indices]
        fps = float(req.get("fps", 30))
        target = req.get("target", "sun")

        from src.transit_analyzer import composite_from_frames

        result = composite_from_frames(
            file_path,
            frame_indices,
            fps=fps,
            target=target,
        )

        if result.get("error"):
            return jsonify(result), 400

        return jsonify({"success": True, **result})

    except Exception as e:
        logger.error(f"[Telescope] Composite-from-frames error: {e}")
        return handle_error(e)


def composite_viewer():
    """GET /telescope/composite?path=captures/2026/03/analyzed_vid_xxx.jpg"""
    img_path = request.args.get("path", "")
    if not img_path:
        return "Missing path", 400

    captures_abs = os.path.abspath("static/captures")
    abs_path = os.path.abspath(os.path.join("static", img_path))
    if not abs_path.startswith(captures_abs):
        return "Forbidden", 403
    if not os.path.exists(abs_path):
        return "Image not found", 404

    # Load sidecar JSON for legend data
    sidecar_path = abs_path.replace(".jpg", "_analysis.json")
    sidecar = {}
    if os.path.exists(sidecar_path):
        import json as _json

        with open(sidecar_path) as f:
            sidecar = _json.load(f)

    events = sidecar.get("transit_events", [])
    detection_count = sidecar.get("detection_count", 0)
    static_count = sidecar.get("detection_count", 0) - len([e for e in events])  # rough
    disk_detected = sidecar.get("disk_detected", False)
    duration = sidecar.get("duration_seconds", 0)
    source = os.path.basename(sidecar.get("source_file", img_path))

    events_html = ""
    for i, evt in enumerate(events, 1):
        t = round((evt.get("start_seconds", 0) + evt.get("end_seconds", 0)) / 2, 2)
        ms = evt.get("duration_ms", 0)
        conf = evt.get("confidence", "")
        events_html += f'<div class="evt-row">Transit {i}: {t}s (~{ms}ms) <span class="conf conf-{conf}">{conf}</span></div>'
    if not events_html:
        events_html = '<div style="color:#888;">No transits detected</div>'

    img_url = "/static/" + img_path

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Transit Composite — {source}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: #111; color: #ccc; font-family: sans-serif; height: 100vh; display: flex; overflow: hidden; }}
#imgPane {{ flex: 1; display: flex; align-items: center; justify-content: center; overflow: hidden; background: #000; }}
#imgPane img {{ max-width: 100%; max-height: 100vh; object-fit: contain; }}
#sidePanel {{ width: 220px; min-width: 220px; background: #1a1a1a; border-left: 1px solid #333; padding: 16px; display: flex; flex-direction: column; gap: 12px; overflow-y: auto; font-size: 0.85em; }}
h2 {{ font-size: 1em; color: #eee; }}
.section {{ border-top: 1px solid #333; padding-top: 10px; }}
.section-title {{ font-weight: bold; color: #aaa; margin-bottom: 6px; font-size: 0.9em; }}
.evt-row {{ margin-bottom: 4px; }}
.conf {{ font-size: 0.8em; padding: 1px 5px; border-radius: 3px; }}
.conf-high {{ background: #1a4a1a; color: #4dff88; }}
.conf-medium {{ background: #4a3a00; color: #ffcc44; }}
.conf-low {{ background: #333; color: #aaa; }}
.leg-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }}
.leg-dot {{ flex-shrink: 0; width: 12px; height: 12px; border-radius: 50%; border: 2px solid; }}
</style>
</head>
<body>
<div id="imgPane"><img src="{img_url}" /></div>
<div id="sidePanel">
  <h2>Transit Composite</h2>
  <div style="color:#aaa; font-size:0.8em;">{source}</div>
  <div class="section">
    <div class="section-title">Result</div>
    {events_html}
    <div style="margin-top:6px; color:#888; font-size:0.85em;">{detection_count} detections · {round(duration,1)}s · disk {'✓' if disk_detected else '✗'}</div>
  </div>
  <div class="section">
    <div class="section-title">Legend</div>
    <div class="leg-row"><span class="leg-dot" style="border-color:#ff4444;"></span><span>Transit position</span></div>
    <div class="leg-row"><span class="leg-dot" style="border-color:#888888;"></span><span>Sunspot (filtered)</span></div>
    <div class="leg-row"><span class="leg-dot" style="border-color:#ffff00;"></span><span>Disk boundary</span></div>
  </div>
</div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html"}


def upload_telescope_file():
    """POST /telescope/files/upload - Upload an external MP4, JPG, or PNG to the captures directory."""
    logger.info("[Telescope] POST /telescope/files/upload")

    _VIDEO_EXTS = (".mp4",)
    _IMAGE_EXTS = (".jpg", ".jpeg", ".png")
    _ALLOWED_EXTS = _VIDEO_EXTS + _IMAGE_EXTS

    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        f = request.files["file"]
        if not f or not f.filename:
            return jsonify({"error": "Empty file"}), 400

        orig_name = f.filename
        ext = os.path.splitext(orig_name)[1].lower()
        if ext not in _ALLOWED_EXTS:
            return (
                jsonify(
                    {"error": "Only .mp4, .jpg, .jpeg, and .png files are accepted"}
                ),
                400,
            )

        is_video = ext in _VIDEO_EXTS
        is_image = ext in _IMAGE_EXTS

        # Validate magic bytes
        header = f.read(12)
        f.seek(0)
        if is_video:
            if len(header) < 8 or header[4:8] not in (
                b"ftyp",
                b"moov",
                b"mdat",
                b"free",
                b"wide",
            ):
                return jsonify({"error": "File does not appear to be a valid MP4"}), 400
        elif ext in (".jpg", ".jpeg"):
            if len(header) < 3 or header[:3] != b"\xff\xd8\xff":
                return (
                    jsonify({"error": "File does not appear to be a valid JPEG"}),
                    400,
                )
        elif ext == ".png":
            if len(header) < 8 or header[:8] != b"\x89PNG\r\n\x1a\n":
                return jsonify({"error": "File does not appear to be a valid PNG"}), 400

        # Enforce size limits: 500 MB for video, 50 MB for images
        import io

        f.seek(0, io.SEEK_END)
        size = f.tell()
        f.seek(0)
        max_size = 500 * 1024 * 1024 if is_video else 50 * 1024 * 1024
        max_label = "500 MB" if is_video else "50 MB"
        if size > max_size:
            return jsonify({"error": f"File exceeds {max_label} limit"}), 400

        # Save to static/captures/YYYY/MM/
        from datetime import datetime as _dt

        now = _dt.now()
        dest_dir = os.path.join(
            "static", "captures", now.strftime("%Y"), now.strftime("%m")
        )
        os.makedirs(dest_dir, exist_ok=True)

        # Use a safe filename — prefix with timestamp to avoid collisions
        safe_name = orig_name.replace(" ", "_")
        dest_path = os.path.join(dest_dir, safe_name)
        # If a file with the same name already exists, add a counter
        base, ext = os.path.splitext(dest_path)
        counter = 1
        while os.path.exists(dest_path):
            dest_path = f"{base}_{counter}{ext}"
            counter += 1

        f.save(dest_path)
        rel_path = os.path.relpath(dest_path, "static").replace(os.sep, "/")
        logger.info(f"[Telescope] Uploaded file: {dest_path}")

        # Generate thumbnail
        # For videos: extract first frame with ffmpeg.
        # For images: the file itself is the thumbnail.
        thumb_url = None
        thumb_path = os.path.splitext(dest_path)[0] + "_thumb.jpg"
        if is_image:
            # Use the uploaded image directly as its own thumbnail
            thumb_url = f"/static/{rel_path}"
        else:
            try:
                result = subprocess.run(
                    [
                        FFMPEG,
                        "-i",
                        dest_path,
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
                    timeout=15,
                )
                if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                    rel_thumb = os.path.relpath(thumb_path, "static").replace(
                        os.sep, "/"
                    )
                    thumb_url = f"/static/{rel_thumb}"
                    logger.info(f"[Telescope] Thumbnail generated: {thumb_path}")
            except Exception as te:
                logger.warning(f"[Telescope] Thumbnail generation skipped: {te}")

        return (
            jsonify(
                {
                    "success": True,
                    "url": f"/static/{rel_path}",
                    "name": os.path.basename(dest_path),
                    "thumbnail": thumb_url,
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"[Telescope] Upload error: {e}", exc_info=True)
        return handle_error(e)


# Video Trim Endpoints


def trim_telescope_file():
    """POST /telescope/files/trim - Non-destructive trim: writes a new _trim.mp4 beside the original.

    Body: { "path": "captures/...", "start_s": float, "end_s": float }
    Original is never modified.
    Returns: { "success": true, "url": "/static/...", "name": "..." }
    """
    logger.info("[Telescope] POST /telescope/files/trim")
    try:
        if not request.is_json:
            return jsonify({"error": "Request must be JSON"}), 400

        data = request.json
        file_path = data.get("path")
        start_s = data.get("start_s")
        end_s = data.get("end_s")

        if not file_path or start_s is None or end_s is None:
            return jsonify({"error": "Missing path, start_s, or end_s"}), 400
        if start_s < 0 or end_s <= start_s:
            return jsonify({"error": "Invalid start_s / end_s range"}), 400

        full_path = os.path.join("static", file_path)
        abs_path = os.path.abspath(full_path)
        captures_abs = os.path.abspath("static/captures")
        if not abs_path.startswith(captures_abs):
            return jsonify({"error": "Invalid file path"}), 403
        if not os.path.exists(abs_path):
            return jsonify({"error": "File not found"}), 404

        stem, ext = os.path.splitext(abs_path)
        if ext.lower() != ".mp4":
            return jsonify({"error": "Only .mp4 files can be trimmed"}), 400

        # Output alongside original: trim_<name>.mp4 (add counter if already exists)
        base_dir = os.path.dirname(abs_path)
        base_name = os.path.basename(abs_path)
        out_path = os.path.join(base_dir, "trim_" + base_name)
        counter = 1
        while os.path.exists(out_path):
            out_path = os.path.join(base_dir, f"trim{counter}_" + base_name)
            counter += 1

        # Re-encode to produce a clean MP4 with proper keyframes.
        # -ss/-to placed after -i for frame-accurate trimming.
        result = subprocess.run(
            [FFMPEG, "-y",
             "-i", abs_path,
             "-ss", str(float(start_s)),
             "-to", str(float(end_s)),
             "-c:v", "libx264",
             "-preset", "fast",
             "-crf", "23",
             "-bf", "0",
             "-pix_fmt", "yuv420p",
             "-g", "30",
             "-an",
             "-movflags", "+faststart",
             out_path],
            capture_output=True,
            timeout=300,
        )

        if result.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            if os.path.exists(out_path):
                os.remove(out_path)
            stderr = result.stderr.decode(errors="replace")[-500:]
            logger.error(f"[Telescope] ffmpeg trim failed: {stderr}")
            return jsonify({"error": f"ffmpeg failed: {stderr}"}), 500

        # Generate thumbnail for the trimmed file
        out_stem = os.path.splitext(out_path)[0]
        thumb_path = out_stem + "_thumb.jpg"
        try:
            subprocess.run(
                [FFMPEG, "-y", "-i", out_path,
                 "-frames:v", "1", "-update", "1", "-q:v", "5", thumb_path],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass

        rel_path = os.path.relpath(out_path, "static").replace(os.sep, "/")
        out_name = os.path.basename(out_path)
        logger.info(f"[Telescope] Trimmed {file_path} [{start_s}s–{end_s}s] → {out_name}")
        return jsonify({"success": True, "url": f"/static/{rel_path}", "name": out_name}), 200

    except Exception as e:
        logger.error(f"[Telescope] Trim error: {e}", exc_info=True)
        return handle_error(e)


# Video Export Endpoint


def export_telescope_file():
    """POST /telescope/files/export - Re-encode a video as a clean, editable MP4.

    Body: { "path": "captures/..." }
    Returns: { "success": true, "url": "/static/...", "name": "..." }
    """
    logger.info("[Telescope] POST /telescope/files/export")
    try:
        if not request.is_json:
            return jsonify({"error": "Request must be JSON"}), 400

        data = request.json
        file_path = data.get("path")

        if not file_path:
            return jsonify({"error": "Missing path"}), 400

        full_path = os.path.join("static", file_path)
        abs_path = os.path.abspath(full_path)
        captures_abs = os.path.abspath("static/captures")
        if not abs_path.startswith(captures_abs):
            return jsonify({"error": "Invalid file path"}), 403
        if not os.path.exists(abs_path):
            return jsonify({"error": "File not found"}), 404

        stem, ext = os.path.splitext(abs_path)
        if ext.lower() not in (".mp4", ".avi", ".mkv", ".webm", ".mov"):
            return jsonify({"error": "Unsupported video format"}), 400

        # Output alongside original: export_<name>.mp4 (add counter if exists)
        base_dir = os.path.dirname(abs_path)
        base_stem = os.path.splitext(os.path.basename(abs_path))[0]
        out_path = os.path.join(base_dir, f"export_{base_stem}.mp4")
        counter = 1
        while os.path.exists(out_path):
            out_path = os.path.join(base_dir, f"export{counter}_{base_stem}.mp4")
            counter += 1

        result = subprocess.run(
            [FFMPEG, "-y",
             "-i", abs_path,
             "-c:v", "libx264",
             "-preset", "fast",
             "-crf", "23",
             "-bf", "0",
             "-pix_fmt", "yuv420p",
             "-g", "30",
             "-an",
             "-movflags", "+faststart",
             out_path],
            capture_output=True,
            timeout=300,
        )

        if result.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            if os.path.exists(out_path):
                os.remove(out_path)
            stderr = result.stderr.decode(errors="replace")[-500:]
            logger.error(f"[Telescope] ffmpeg export failed: {stderr}")
            return jsonify({"error": f"ffmpeg failed: {stderr}"}), 500

        # Generate thumbnail for the exported file
        out_stem = os.path.splitext(out_path)[0]
        thumb_path = out_stem + "_thumb.jpg"
        try:
            subprocess.run(
                [FFMPEG, "-y", "-i", out_path,
                 "-frames:v", "1", "-update", "1", "-q:v", "5", thumb_path],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass

        rel_path = os.path.relpath(out_path, "static").replace(os.sep, "/")
        out_name = os.path.basename(out_path)
        logger.info(f"[Telescope] Exported {file_path} → {out_name}")
        return jsonify({"success": True, "url": f"/static/{rel_path}", "name": out_name}), 200

    except Exception as e:
        logger.error(f"[Telescope] Export error: {e}", exc_info=True)
        return handle_error(e)


# Photo Capture Endpoint


def capture_photo():
    """POST /telescope/capture/photo - Capture a single photo from live stream."""
    logger.info("[Telescope] POST /telescope/capture/photo")

    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        rtsp_url = _ensure_rtsp_ready(client)
        if not rtsp_url:
            return (
                jsonify(
                    {
                        "error": "RTSP stream is not ready. Set scope to Sun/Moon view and try again."
                    }
                ),
                503,
            )

        # Generate filename with timestamp
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"capture_{timestamp}.jpg"

        # Create year/month directories
        now = datetime.now()
        year_month_path = os.path.join(
            "static/captures", str(now.year), f"{now.month:02d}"
        )
        os.makedirs(year_month_path, exist_ok=True)

        filepath = os.path.join(year_month_path, filename)

        logger.info(f"[Telescope] Capturing frame from RTSP stream to {filepath}")

        # Use FFmpeg to grab a single frame from RTSP stream
        cmd = [
            FFMPEG,
            "-rtsp_transport",
            "tcp",
            "-i",
            rtsp_url,
            "-frames:v",
            "1",  # Capture only 1 frame
            "-update",
            "1",  # Required for single image output
            "-q:v",
            "2",  # High quality JPEG
            "-y",  # Overwrite if exists
            filepath,
        ]

        # Run FFmpeg with timeout
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
        )

        if result.returncode != 0:
            logger.error(f"[Telescope] FFmpeg capture failed: {result.stderr.decode()}")
            return jsonify({"error": "Failed to capture frame from stream"}), 500

        # Create metadata
        metadata = {
            "timestamp": now.isoformat(),
            "source": "live_stream",
            "telescope": client.host,
            "viewing_mode": (
                client._viewing_mode if hasattr(client, "_viewing_mode") else None
            ),
        }

        metadata_path = filepath.rsplit(".", 1)[0] + ".json"
        with open(metadata_path, "w") as f:
            import json

            json.dump(metadata, f, indent=2)

        # Build web path for response
        rel_path = os.path.relpath(filepath, "static").replace("\\", "/")

        logger.info(f"[Telescope] Photo captured successfully: {filename}")
        return (
            jsonify(
                {
                    "success": True,
                    "filename": filename,
                    "path": rel_path,
                    "url": f"/static/{rel_path}",
                    "message": "Photo captured from live stream",
                }
            ),
            200,
        )

    except subprocess.TimeoutExpired:
        logger.error("[Telescope] Photo capture timeout")
        return jsonify({"error": "Capture timeout"}), 500
    except Exception as e:
        logger.error(f"[Telescope] Photo capture error: {e}", exc_info=True)
        return handle_error(e)


# Target Visibility and Selection Endpoints


def get_target_visibility():
    """GET /telescope/target/visibility - Get Sun/Moon visibility status."""
    logger.debug("[Telescope] GET /telescope/target/visibility")

    try:
        from tzlocal import get_localzone

        latitude, longitude, elevation = get_observer_coordinates()

        observer_position = get_my_pos(
            lat=latitude, lon=longitude, elevation=elevation, base_ref=EARTH
        )

        # Get current time in local timezone
        local_tz = get_localzone()
        ref_datetime = datetime.now(local_tz)

        # Calculate Sun position
        sun = CelestialObject(name="sun", observer_position=observer_position)
        sun.update_position(ref_datetime=ref_datetime)
        sun_coords = sun.get_coordinates()

        # Calculate Moon position
        moon = CelestialObject(name="moon", observer_position=observer_position)
        moon.update_position(ref_datetime=ref_datetime)
        moon_coords = moon.get_coordinates()

        # Determine visibility (above horizon = altitude > 0)
        sun_visible = bool(sun_coords["altitude"] > 0)
        moon_visible = bool(moon_coords["altitude"] > 0)

        logger.debug(
            f"[Telescope] Sun: {sun_coords['altitude']:.1f}°, Moon: {moon_coords['altitude']:.1f}°"
        )

        return (
            jsonify(
                {
                    "sun": {
                        "altitude": float(sun_coords["altitude"]),
                        "azimuth": float(sun_coords["azimuthal"]),
                        "visible": sun_visible,
                    },
                    "moon": {
                        "altitude": float(moon_coords["altitude"]),
                        "azimuth": float(moon_coords["azimuthal"]),
                        "visible": moon_visible,
                    },
                    "timestamp": ref_datetime.isoformat(),
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"[Telescope] Failed to get target visibility: {e}")
        return handle_error(e)


def switch_to_sun():
    """POST /telescope/target/sun - Switch telescope to solar viewing mode."""
    logger.info("[Telescope] POST /telescope/target/sun")

    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        latitude, longitude, elevation = get_observer_coordinates()

        observer_position = get_my_pos(
            lat=latitude, lon=longitude, elevation=elevation, base_ref=EARTH
        )

        # Check if Sun is visible
        from tzlocal import get_localzone

        local_tz = get_localzone()
        ref_datetime = datetime.now(local_tz)

        sun = CelestialObject(name="sun", observer_position=observer_position)
        sun.update_position(ref_datetime=ref_datetime)
        sun_coords = sun.get_coordinates()

        if sun_coords["altitude"] <= 0:
            return (
                jsonify(
                    {
                        "error": "Sun is below horizon",
                        "altitude": sun_coords["altitude"],
                        "visible": False,
                    }
                ),
                400,
            )

        # Switch to solar mode
        client.start_solar_mode()
        _maybe_auto_start_detector("sun")

        logger.info("[Telescope] Switched to solar viewing mode")
        return (
            jsonify(
                {
                    "success": True,
                    "target": "sun",
                    "altitude": sun_coords["altitude"],
                    "azimuth": sun_coords["azimuthal"],
                    "message": "⚠️ SOLAR FILTER REQUIRED - Ensure solar filter is installed before viewing!",
                    "warning": "solar_filter_required",
                }
            ),
            200,
        )

    except Exception as e:
        return handle_error(e)


def switch_to_moon():
    """POST /telescope/target/moon - Switch telescope to lunar viewing mode."""
    logger.info("[Telescope] POST /telescope/target/moon")

    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        latitude, longitude, elevation = get_observer_coordinates()

        observer_position = get_my_pos(
            lat=latitude, lon=longitude, elevation=elevation, base_ref=EARTH
        )

        # Check if Moon is visible
        from tzlocal import get_localzone

        local_tz = get_localzone()
        ref_datetime = datetime.now(local_tz)

        moon = CelestialObject(name="moon", observer_position=observer_position)
        moon.update_position(ref_datetime=ref_datetime)
        moon_coords = moon.get_coordinates()

        if moon_coords["altitude"] <= 0:
            return (
                jsonify(
                    {
                        "error": "Moon is below horizon",
                        "altitude": moon_coords["altitude"],
                        "visible": False,
                    }
                ),
                400,
            )

        # Switch to lunar mode
        client.start_lunar_mode()
        _maybe_auto_start_detector("moon")

        logger.info("[Telescope] Switched to lunar viewing mode")
        return (
            jsonify(
                {
                    "success": True,
                    "target": "moon",
                    "altitude": moon_coords["altitude"],
                    "azimuth": moon_coords["azimuthal"],
                    "message": "✓ Remove solar filter if installed - Lunar viewing safe without filter",
                    "warning": "remove_solar_filter",
                }
            ),
            200,
        )

    except Exception as e:
        return handle_error(e)


def switch_to_scenery():
    """POST /telescope/mode/scenery - Switch telescope to scenery mode (no tracking)."""
    logger.info("[Telescope] POST /telescope/mode/scenery")

    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        # Live detection only makes sense against a Sun/Moon disc — stop it
        # before the scope switches to a freeform scenery view.
        detection_was_running = False
        try:
            from src.transit_detector import get_detector, stop_detector

            det = get_detector()
            if det and det.is_running:
                stop_detector()
                detection_was_running = True
                logger.info(
                    "[Telescope] Stopped live detection because scope entered scenery mode"
                )
        except Exception as exc:
            logger.warning(
                f"[Telescope] Failed to stop live detection on scenery switch: {exc}"
            )

        client.start_scenery_mode()

        logger.info("[Telescope] Switched to scenery viewing mode")
        return (
            jsonify(
                {
                    "success": True,
                    "target": "scenery",
                    "detection_stopped": detection_was_running,
                    "message": "Scenery mode active — no sidereal tracking, manual positioning enabled",
                }
            ),
            200,
        )

    except Exception as e:
        return handle_error(e)


# Live Preview Stream Endpoint


def telescope_preview_stream():
    """GET /telescope/preview/stream.mjpg - MJPEG live preview stream from RTSP."""
    logger.info(
        "[Telescope] GET /telescope/preview/stream.mjpg - Starting MJPEG stream"
    )

    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        # Prefer a URL that was already validated by the eager probe kicked
        # off at connect time (or by a previous successful stream). Only
        # re-run the probe when the cache is empty.
        rtsp_url = getattr(client, "_rtsp_cached_url", None)
        if rtsp_url:
            logger.info(f"[Telescope] Using cached RTSP URL: {rtsp_url}")
        else:
            rtsp_url = _ensure_rtsp_ready(client, timeout_seconds=5)
            if rtsp_url:
                try:
                    client._rtsp_cached_url = rtsp_url
                except Exception:
                    pass
            else:
                # No silent fallback to candidates[0] — that just hands a
                # broken URL to ffmpeg and the frontend throws `Stream failed`.
                # Return 503 so the client can retry cleanly.
                return (
                    jsonify(
                        {
                            "error": "RTSP probe failed; no working stream URL",
                            "retryable": True,
                        }
                    ),
                    503,
                )

        logger.info(f"[Telescope] Transcoding RTSP stream: {rtsp_url}")

        def generate_mjpeg():
            """Generate MJPEG frames from RTSP stream using FFmpeg."""
            # FFmpeg command to transcode RTSP → individual JPEG frames
            cmd = [
                FFMPEG,
                "-rtsp_transport",
                "tcp",
                "-timeout",
                "10000000",
                "-i",
                rtsp_url,
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-q:v",
                "5",
                "-r",
                "10",
                "pipe:1",
            ]

            process = None
            frames_yielded = 0
            try:
                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
                )

                logger.info(f"[Telescope] FFmpeg process started (PID: {process.pid})")

                # Buffer for accumulating data
                buffer = b""

                while True:
                    # Read data in small chunks
                    chunk = process.stdout.read(4096)
                    if not chunk:
                        break

                    buffer += chunk

                    # Look for complete JPEG frames (start: FF D8, end: FF D9)
                    while True:
                        # Find JPEG start
                        start_idx = buffer.find(b"\xff\xd8")
                        if start_idx == -1:
                            break

                        # Find JPEG end after start
                        end_idx = buffer.find(b"\xff\xd9", start_idx + 2)
                        if end_idx == -1:
                            # Incomplete frame, keep buffering
                            break

                        # Extract complete JPEG frame
                        jpeg_frame = buffer[start_idx : end_idx + 2]
                        buffer = buffer[end_idx + 2 :]

                        # Yield MJPEG frame
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: "
                            + str(len(jpeg_frame)).encode()
                            + b"\r\n"
                            b"\r\n" + jpeg_frame + b"\r\n"
                        )
                        frames_yielded += 1

            except GeneratorExit:
                logger.info("[Telescope] Client disconnected from stream")
            except Exception as e:
                logger.error(f"[Telescope] FFmpeg stream error: {e}")
            finally:
                if process:
                    process.kill()
                    process.wait()
                    logger.info("[Telescope] FFmpeg process terminated")
                # If ffmpeg never produced a frame the cached URL is stale —
                # drop it so the next request re-probes instead of looping on
                # the same bad URL.
                if frames_yielded == 0:
                    try:
                        if getattr(client, "_rtsp_cached_url", None) == rtsp_url:
                            client._rtsp_cached_url = None
                            logger.warning(
                                "[RTSP] Cached URL produced zero frames; cleared cache"
                            )
                    except Exception:
                        pass

        return Response(
            generate_mjpeg(), mimetype="multipart/x-mixed-replace; boundary=frame"
        )

    except Exception as e:
        logger.error(f"[Telescope] Preview stream failed: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# TRANSIT MONITORING
# ============================================================================


# Simulation Mode Toggle


def toggle_simulate_mode():
    """POST /telescope/simulate - Toggle simulation mode."""
    global _simulate_mode, _telescope_client

    _simulate_mode = not _simulate_mode

    # Reset client so it gets recreated with correct type
    if _telescope_client:
        try:
            _telescope_client.disconnect()
        except Exception as exc:
            logger.warning(
                "Error disconnecting telescope client during mode toggle: %s", exc
            )
        _telescope_client = None

    logger.info(
        f"[Telescope] Simulation mode {'enabled' if _simulate_mode else 'disabled'}"
    )

    return (
        jsonify(
            {
                "success": True,
                "simulate_mode": _simulate_mode,
                "message": f"Simulation mode {'enabled' if _simulate_mode else 'disabled'}",
            }
        ),
        200,
    )


def get_simulate_status():
    """GET /telescope/simulate - Get simulation mode status."""
    return jsonify({"simulate_mode": _simulate_mode}), 200


# ============================================================================
# NOTIFICATION MUTE
# ============================================================================


def toggle_notifications_mute():
    """POST /telescope/notifications/mute - Toggle Telegram alert mute."""
    from src.telegram_notify import get_notifications_muted, set_notifications_muted

    muted = not get_notifications_muted()
    set_notifications_muted(muted)
    logger.info(f"[Telescope] Telegram notifications {'muted' if muted else 'unmuted'}")
    return jsonify({"muted": muted}), 200


def get_notifications_status():
    """GET /telescope/notifications/status - Get Telegram alert mute state."""
    from src.telegram_notify import get_notifications_muted

    return jsonify({"muted": get_notifications_muted()}), 200


# Route Registration Helper


def update_user_settings():
    """POST /api/settings — receive browser-side UI settings for server use."""
    data = request.get_json(silent=True) or {}
    if "min_reconnect_altitude" in data:
        try:
            _user_settings["min_reconnect_altitude"] = float(
                data["min_reconnect_altitude"]
            )
            logger.info(
                f"[Settings] min_reconnect_altitude updated to "
                f"{_user_settings['min_reconnect_altitude']}°"
            )
        except (TypeError, ValueError):
            return jsonify({"error": "min_reconnect_altitude must be a number"}), 400
    if data.get("observer_revert_to_env") is True:
        clear_observer_browser_override()
        logger.info(
            "[Settings] Observer browser override cleared — using .env OBSERVER_*"
        )
    if "observer_latitude" in data and "observer_longitude" in data:
        try:
            lat = float(data["observer_latitude"])
            lon = float(data["observer_longitude"])
            raw_e = data.get("observer_elevation", 0)
            elev = float(raw_e) if raw_e not in (None, "") else 0.0
            set_observer_from_browser(lat, lon, elev)
            eff_lat, eff_lon, eff_elev = get_observer_coordinates()
            logger.info(
                f"[Settings] Observer site from browser: {lat:.5f}°, {lon:.5f}°, {elev:.0f}m "
                f"(effective for telescope: {eff_lat:.5f}°, {eff_lon:.5f}°, {eff_elev:.0f}m)"
            )
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"Invalid observer_latitude/longitude: {e}"}), 400
    return jsonify(
        {"ok": True, "settings": {**_user_settings, **observer_snapshot_for_api()}}
    )


def get_user_settings():
    """GET /api/settings — return current server-side UI settings."""
    return jsonify({**_user_settings, **observer_snapshot_for_api()})


def telescope_debug_cmd():
    """POST /telescope/debug/cmd — send any raw command via the live socket and return the response.
    Body: {"method": "scope_speed_move", "params": {...}, "expect_response": true, "timeout": 8}
    """
    client = get_telescope_client()
    if not client or not client.is_connected():
        return jsonify({"error": "not connected"}), 503
    data = request.get_json(force=True, silent=True) or {}
    method = data.get("method", "pi_is_verified")
    params = data.get("params", None)
    expect = data.get("expect_response", True)
    timeout = data.get("timeout", 8)
    import time as _t

    t0 = _t.time()
    try:
        result = client._send_command(
            method, params=params, expect_response=expect, timeout_override=timeout
        )
        return (
            jsonify(
                {
                    "method": method,
                    "result": result,
                    "elapsed": round(_t.time() - t0, 2),
                }
            ),
            200,
        )
    except Exception as e:
        return (
            jsonify(
                {"method": method, "error": str(e), "elapsed": round(_t.time() - t0, 2)}
            ),
            200,
        )


# ============================================================================
# ALPACA ENDPOINTS
# ============================================================================


def alpaca_telemetry():
    """GET /telescope/alpaca/telemetry — full ALPACA scope telemetry.

    Returns position (RA/Dec/Alt/Az), state (tracking, slewing, parked),
    device info, and capabilities.  Useful for a live scope status panel.
    """
    alpaca = get_alpaca_client()
    if not alpaca:
        return jsonify({"error": "ALPACA not connected", "connected": False}), 200
    if not alpaca.is_connected():
        return (
            jsonify(
                {
                    "error": "ALPACA not connected",
                    "connected": False,
                    "host": getattr(alpaca, "host", None),
                    "port": getattr(alpaca, "port", None),
                }
            ),
            200,
        )
    telemetry = alpaca.get_telemetry()
    telemetry["host"] = getattr(alpaca, "host", None)
    telemetry["port"] = getattr(alpaca, "port", None)
    return jsonify(telemetry), 200


def alpaca_tracking():
    """POST /telescope/alpaca/tracking — enable or disable sidereal tracking.

    Body: {"enabled": true|false}
    """
    alpaca = get_alpaca_client()
    if not alpaca or not alpaca.is_connected():
        return jsonify({"error": "ALPACA not connected"}), 503
    data = request.get_json(force=True, silent=True) or {}
    enabled = data.get("enabled", True)
    try:
        result = alpaca.set_tracking(bool(enabled))
        return (
            jsonify({"success": True, "tracking": bool(enabled), "result": result}),
            200,
        )
    except Exception as e:
        return handle_error(e)


def alpaca_settings():
    """GET/PATCH /telescope/alpaca/settings — telemetry poll interval (Tune panel)."""
    alpaca = get_alpaca_client()
    if request.method == "GET":
        if not alpaca:
            return (
                jsonify(
                    {
                        "poll_interval_sec": 5.0,
                        "alpaca_configured": False,
                    }
                ),
                200,
            )
        return (
            jsonify(
                {
                    "poll_interval_sec": alpaca.get_poll_interval(),
                    "alpaca_configured": True,
                }
            ),
            200,
        )
    # PATCH
    if not alpaca:
        return (
            jsonify({"error": "ALPACA not enabled (set ENABLE_SEESTAR=true)"}),
            503,
        )
    data = request.get_json(force=True, silent=True) or {}
    if "poll_interval_sec" in data:
        try:
            alpaca.set_poll_interval(float(data["poll_interval_sec"]))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid poll_interval_sec"}), 400
    return (
        jsonify(
            {
                "success": True,
                "poll_interval_sec": alpaca.get_poll_interval(),
            }
        ),
        200,
    )


def alpaca_abort_slew():
    """POST /telescope/alpaca/abort — abort any in-progress slew."""
    alpaca = get_alpaca_client()
    if not alpaca or not alpaca.is_connected():
        return jsonify({"error": "ALPACA not connected"}), 503
    try:
        result = alpaca.abort_slew()
        alpaca.stop_axes()
        with _motor_ctrl.lock:
            _motor_ctrl.state = _CtrlState.IDLE
            _motor_ctrl.pre_nudge_tracking = False
        return jsonify({"success": True, "result": result}), 200
    except Exception as e:
        with _motor_ctrl.lock:
            _motor_ctrl.state = _CtrlState.IDLE
        return handle_error(e)


def register_routes(app):
    """
    Register all telescope routes with the Flask app.

    Args:
        app: Flask application instance
    """
    # User settings sync (browser → server)
    app.add_url_rule(
        "/api/settings", "api_settings_post", update_user_settings, methods=["POST"]
    )
    app.add_url_rule(
        "/api/settings", "api_settings_get", get_user_settings, methods=["GET"]
    )

    # Connection management
    app.add_url_rule(
        "/telescope/discover", "telescope_discover", discover_seestar, methods=["GET"]
    )
    app.add_url_rule(
        "/telescope/connect", "telescope_connect", connect_telescope, methods=["POST"]
    )
    app.add_url_rule(
        "/telescope/disconnect",
        "telescope_disconnect",
        disconnect_telescope,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/status", "telescope_status", get_telescope_status, methods=["GET"]
    )

    # Target info and visibility
    app.add_url_rule(
        "/telescope/target", "telescope_get_target", get_current_target, methods=["GET"]
    )
    app.add_url_rule(
        "/telescope/target/visibility",
        "telescope_target_visibility",
        get_target_visibility,
        methods=["GET"],
    )
    app.add_url_rule(
        "/telescope/target/sun", "telescope_switch_sun", switch_to_sun, methods=["POST"]
    )
    app.add_url_rule(
        "/telescope/target/moon",
        "telescope_switch_moon",
        switch_to_moon,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/mode/scenery",
        "telescope_switch_scenery",
        switch_to_scenery,
        methods=["POST"],
    )

    # Recording
    app.add_url_rule(
        "/telescope/recording/start",
        "telescope_recording_start",
        start_recording,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/recording/stop",
        "telescope_recording_stop",
        stop_recording,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/recording/status",
        "telescope_recording_status",
        get_recording_status,
        methods=["GET"],
    )

    # Solar Timelapse
    app.add_url_rule(
        "/telescope/timelapse/start",
        "telescope_timelapse_start",
        start_timelapse,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/timelapse/stop",
        "telescope_timelapse_stop",
        stop_timelapse,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/timelapse/status",
        "telescope_timelapse_status",
        get_timelapse_status,
        methods=["GET"],
    )
    app.add_url_rule(
        "/telescope/timelapse/settings",
        "telescope_timelapse_settings",
        update_timelapse_settings,
        methods=["PATCH"],
    )
    app.add_url_rule(
        "/telescope/timelapse/pause",
        "telescope_timelapse_pause",
        pause_timelapse,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/timelapse/resume",
        "telescope_timelapse_resume",
        resume_timelapse,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/timelapse/preview",
        "telescope_timelapse_preview",
        preview_timelapse,
        methods=["POST"],
    )

    # Photo capture
    app.add_url_rule(
        "/telescope/capture/photo",
        "telescope_capture_photo",
        capture_photo,
        methods=["POST"],
    )

    # Live preview
    app.add_url_rule(
        "/telescope/preview/stream.mjpg",
        "telescope_preview_stream",
        telescope_preview_stream,
        methods=["GET"],
    )

    # File management
    app.add_url_rule(
        "/telescope/files", "telescope_files", list_telescope_files, methods=["GET"]
    )
    app.add_url_rule(
        "/telescope/files/favorites",
        "telescope_files_favorites_get",
        get_telescope_favorites,
        methods=["GET"],
    )
    app.add_url_rule(
        "/telescope/files/favorites",
        "telescope_files_favorites_save",
        save_telescope_favorites,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/files/frame",
        "telescope_files_frame",
        get_telescope_file_frame,
        methods=["GET"],
    )
    app.add_url_rule(
        "/telescope/files/video-info",
        "telescope_files_video_info",
        get_telescope_video_info,
        methods=["GET"],
    )
    app.add_url_rule(
        "/telescope/files/delete",
        "telescope_files_delete",
        delete_telescope_file,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/files/rename",
        "telescope_files_rename",
        rename_telescope_file,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/files/analyze",
        "telescope_files_analyze",
        analyze_file,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/files/composite-from-frames",
        "telescope_composite_from_frames",
        composite_from_frames_route,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/files/isolate-transit",
        "telescope_isolate_transit",
        isolate_transit_route,
        methods=["POST"],
    )

    app.add_url_rule(
        "/api/video/fps",
        "api_video_fps",
        video_fps_route,
        methods=["GET"],
    )

    app.add_url_rule(
        "/telescope/composite",
        "telescope_composite_viewer",
        composite_viewer,
        methods=["GET"],
    )

    app.add_url_rule(
        "/telescope/files/upload",
        "telescope_files_upload",
        upload_telescope_file,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/files/trim",
        "telescope_files_trim",
        trim_telescope_file,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/files/export",
        "telescope_files_export",
        export_telescope_file,
        methods=["POST"],
    )

    # Transit monitoring
    app.add_url_rule(
        "/telescope/transit/status",
        "telescope_transit_status",
        get_transit_status,
        methods=["GET"],
    )
    app.add_url_rule(
        "/telescope/transit/check",
        "telescope_transit_check",
        transit_check,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/armed",
        "telescope_armed_status",
        get_armed_status,
        methods=["GET"],
    )

    # Simulation mode
    app.add_url_rule(
        "/telescope/simulate",
        "telescope_simulate_toggle",
        toggle_simulate_mode,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/simulate",
        "telescope_simulate_status",
        get_simulate_status,
        methods=["GET"],
    )

    # Notification mute toggle
    app.add_url_rule(
        "/telescope/notifications/mute",
        "telescope_notifications_mute",
        toggle_notifications_mute,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/notifications/status",
        "telescope_notifications_status",
        get_notifications_status,
        methods=["GET"],
    )

    # Transit detection (real-time)
    app.add_url_rule(
        "/telescope/detect/start",
        "telescope_detect_start",
        start_detection,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/detect/stop",
        "telescope_detect_stop",
        stop_detection,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/detect/status",
        "telescope_detect_status",
        get_detection_status,
        methods=["GET"],
    )
    app.add_url_rule(
        "/telescope/detect/settings",
        "telescope_detect_settings",
        update_detection_settings,
        methods=["PATCH"],
    )
    app.add_url_rule(
        "/telescope/detect/events",
        "telescope_detect_events",
        get_detection_events,
        methods=["GET"],
    )

    # Experimental Sun centering (Solar mode only)
    app.add_url_rule(
        "/telescope/sun-center/start",
        "telescope_sun_center_start",
        start_sun_centering,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/sun-center/stop",
        "telescope_sun_center_stop",
        stop_sun_centering,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/sun-center/recenter",
        "telescope_sun_center_recenter",
        recenter_sun_centering,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/sun-center/status",
        "telescope_sun_center_status",
        get_sun_centering_status,
        methods=["GET"],
    )
    app.add_url_rule(
        "/telescope/sun-center/settings",
        "telescope_sun_center_settings",
        update_sun_centering_settings,
        methods=["PATCH"],
    )

    # ── Detection test harness endpoints ──────────────────────────────────
    app.add_url_rule(
        "/telescope/harness/inject",
        "telescope_harness_inject",
        harness_inject,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/harness/sweep",
        "telescope_harness_sweep",
        harness_sweep,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/harness/validate",
        "telescope_harness_validate",
        harness_validate,
        methods=["POST"],
    )

    # Extended control routes (Option A — native JSON-RPC)
    app.add_url_rule(
        "/telescope/goto",
        "telescope_goto",
        telescope_goto,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/stop",
        "telescope_stop_view",
        telescope_stop_view,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/nudge",
        "telescope_nudge",
        telescope_nudge,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/nudge/stop",
        "telescope_nudge_stop",
        telescope_nudge_stop,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/open-arm",
        "telescope_open_arm",
        telescope_open_arm,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/park",
        "telescope_park",
        telescope_park,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/shutdown",
        "telescope_shutdown",
        telescope_shutdown,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/autofocus",
        "telescope_autofocus",
        telescope_autofocus,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/position",
        "telescope_position",
        telescope_position,
        methods=["GET"],
    )
    app.add_url_rule(
        "/telescope/focus/step",
        "telescope_focus_step",
        telescope_focus_step,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/settings/camera",
        "patch_camera_settings",
        patch_camera_settings,
        methods=["PATCH"],
    )
    app.add_url_rule(
        "/telescope/camera/auto-exp",
        "telescope_auto_exp",
        telescope_auto_exp,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/goto/locations",
        "list_goto_locations",
        list_goto_locations,
        methods=["GET"],
    )
    app.add_url_rule(
        "/telescope/goto/locations",
        "save_goto_location",
        save_goto_location,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/goto/locations/<path:name>",
        "delete_goto_location",
        delete_goto_location,
        methods=["DELETE"],
    )

    app.add_url_rule(
        "/telescope/debug/cmd",
        "telescope_debug_cmd",
        telescope_debug_cmd,
        methods=["POST"],
    )

    # ALPACA motor control & telemetry
    app.add_url_rule(
        "/telescope/alpaca/telemetry",
        "alpaca_telemetry",
        alpaca_telemetry,
        methods=["GET"],
    )
    app.add_url_rule(
        "/telescope/alpaca/tracking",
        "alpaca_tracking",
        alpaca_tracking,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/alpaca/abort",
        "alpaca_abort_slew",
        alpaca_abort_slew,
        methods=["POST"],
    )
    app.add_url_rule(
        "/telescope/alpaca/settings",
        "alpaca_settings",
        alpaca_settings,
        methods=["GET", "PATCH"],
    )

    logger.info("[Telescope] Routes registered (ALPACA endpoints included)")

    # Auto-connect if ENABLE_SEESTAR=true
    if is_enabled():
        _auto_connect_background()


def _auto_connect_background():
    """Attempt to connect to Seestar at startup, retrying until the scope is
    reachable (e.g. still booting).  Gives up after ~5 minutes.

    The _connect_lock inside SeestarClient serialises this thread against any
    concurrent POST /telescope/connect requests so only one TCP handshake
    happens at a time.
    """
    import time as _time

    _RETRY_DELAYS = [
        5,
        10,
        15,
        30,
        60,
    ]  # seconds between retries (capped at last value)
    _MAX_ATTEMPTS = 10

    def _worker():
        client = get_telescope_client()
        if not client:
            logger.warning(
                "[Telescope] Auto-connect: no client (check SEESTAR_HOST in .env)"
            )
            return

        if client.is_connected():
            logger.info("[Telescope] Auto-connect: already connected")
            _post_connect(client)
            return

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                client.connect()
                logger.info(
                    f"[Telescope] Auto-connect: connected to {client.host}:{client.port}"
                    f" (attempt {attempt})"
                )
                _post_connect(client)
                return
            except Exception as e:
                delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
                if attempt < _MAX_ATTEMPTS:
                    logger.info(
                        f"[Telescope] Auto-connect attempt {attempt}: scope not reachable"
                        f" ({e}) — retrying in {delay}s"
                    )
                    _time.sleep(delay)
                else:
                    logger.info(
                        f"[Telescope] Auto-connect: gave up after {_MAX_ATTEMPTS} attempts"
                        f" ({e}) — connect manually from the telescope page"
                    )

    def _post_connect(client):
        """Steps to run once connected: start solar mode, ALPACA, and resume timelapse."""
        # Connect ALPACA for motor control
        alpaca = get_alpaca_client()
        if alpaca and not alpaca.is_connected():
            if not alpaca.host and client.host:
                alpaca.host = client.host
            try:
                if alpaca.connect():
                    logger.info(
                        f"[Telescope] Auto-connect: ALPACA connected to {alpaca.host}:{alpaca.port}"
                    )
                else:
                    logger.warning("[Telescope] Auto-connect: ALPACA connection failed")
            except Exception as ae:
                logger.warning(f"[Telescope] Auto-connect: ALPACA error: {ae}")

        # Always solar mode for RTSP (see telescope_connect)
        if hasattr(client, "_viewing_mode") and client._viewing_mode is None:
            client._viewing_mode = "sun"
            logger.info("[Telescope] Auto-connect: starting solar view for RTSP")
            try:
                client.start_solar_mode()
                import time as _t

                _t.sleep(2)
                _maybe_auto_start_detector("sun")
            except Exception as e:
                logger.warning(
                    f"[Telescope] Auto-connect: could not start solar view: {e}"
                )

        # Resume timelapse only if today already has frames (genuine crash-resume).
        # Don't auto-start on a fresh boot — the scope may be idle with no RTSP stream.
        auto_resume = os.getenv("SOLAR_TIMELAPSE_AUTO_RESUME", "true").strip().lower()
        if auto_resume in ("1", "true", "yes", "on"):
            tl = get_timelapse()
            if not tl.is_running and tl.has_today_frames():
                try:
                    interval = float(os.getenv("SOLAR_TIMELAPSE_INTERVAL", "120"))
                except ValueError:
                    interval = 120.0
                tl.resume_today(host=client.host, interval=interval)

        # Watchdog: restart the detector if it dies while the scope is still connected.
        _start_detector_watchdog(client)

    t = threading.Thread(target=_worker, name="seestar-auto-connect", daemon=True)
    t.start()


def _start_detector_watchdog(client) -> None:
    """Launch a daemon thread that restarts the transit detector if it stops
    unexpectedly (e.g. RTSP stream drop) while the scope is still connected."""

    import time as _time

    _WATCHDOG_INTERVAL = 30  # seconds between checks

    def _watchdog():
        while True:
            _time.sleep(_WATCHDOG_INTERVAL)
            try:
                if not client.is_connected():
                    # Scope disconnected — stop watching; auto-connect will handle it.
                    logger.debug("[Watchdog] Scope disconnected, watchdog exiting.")
                    return

                from src.transit_detector import get_detector

                det = get_detector()
                if det is not None and not det.is_running:
                    logger.info(
                        "[Watchdog] Detector stopped unexpectedly — auto-restarting."
                    )
                    mode = getattr(client, "_viewing_mode", None) or "sun"
                    _maybe_auto_start_detector(mode)
            except Exception as exc:
                logger.debug(f"[Watchdog] check error: {exc}")

    t = threading.Thread(target=_watchdog, name="detector-watchdog", daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Real-time Transit Detection Endpoints
# ---------------------------------------------------------------------------


def _build_sun_center_adapter() -> SunCenteringAdapter:
    """Construct adapter callbacks for the experimental Sun-centering loop."""

    def _scope_connected() -> bool:
        client = get_telescope_client()
        return bool(client and client.is_connected())

    def _alpaca_connected() -> bool:
        alpaca = get_alpaca_client()
        return bool(alpaca and alpaca.is_connected())

    def _view_mode() -> Optional[str]:
        client = get_telescope_client()
        return getattr(client, "_viewing_mode", None) if client else None

    def _sun_altaz() -> Optional[tuple]:
        try:
            from tzlocal import get_localzone

            latitude, longitude, elevation = get_observer_coordinates()
            observer_position = get_my_pos(
                lat=latitude, lon=longitude, elevation=elevation, base_ref=EARTH
            )
            local_tz = get_localzone()
            ref_datetime = datetime.now(local_tz)
            sun = CelestialObject(name="sun", observer_position=observer_position)
            sun.update_position(ref_datetime=ref_datetime)
            coords = sun.get_coordinates()
            return float(coords["altitude"]), float(coords["azimuthal"])
        except Exception as exc:
            logger.debug("[SunCenter] Failed to compute sun alt/az: %s", exc)
            return None

    def _goto_altaz(alt: float, az: float) -> Dict[str, Any]:
        alpaca = get_alpaca_client()
        if not alpaca:
            return {"error": "ALPACA unavailable"}
        return alpaca.goto_altaz(float(alt), float(az), timeout_sec=3.0)

    def _is_slewing() -> bool:
        alpaca = get_alpaca_client()
        return bool(alpaca and alpaca.is_connected() and alpaca.is_slewing())

    def _stop_axes() -> Dict[str, Any]:
        alpaca = get_alpaca_client()
        if not alpaca:
            return {"error": "ALPACA unavailable"}
        return alpaca.stop_axes(timeout_sec=2.0)

    def _get_position() -> Dict[str, float]:
        alpaca = get_alpaca_client()
        if not alpaca or not alpaca.is_connected():
            return {}
        try:
            return alpaca.get_position()
        except Exception as exc:
            logger.debug("[SunCenter] get_position failed: %s", exc)
            return {}

    def _set_tracking(enabled: bool) -> Dict[str, Any]:
        alpaca = get_alpaca_client()
        if not alpaca:
            return {"error": "ALPACA unavailable"}
        return alpaca.set_tracking(bool(enabled), timeout_sec=2.0)

    def _detector_status() -> Dict[str, Any]:
        try:
            from src.transit_detector import get_detector

            det = get_detector()
            return det.get_status() if det else {}
        except Exception:
            return {}

    return SunCenteringAdapter(
        is_scope_connected=_scope_connected,
        is_alpaca_connected=_alpaca_connected,
        get_viewing_mode=_view_mode,
        get_sun_altaz=_sun_altaz,
        goto_altaz=_goto_altaz,
        is_slewing=_is_slewing,
        stop_axes=_stop_axes,
        get_detector_status=_detector_status,
        get_position=_get_position,
        set_tracking=_set_tracking,
    )


def start_sun_centering():
    """POST /telescope/sun-center/start - start experimental Sun centering."""
    logger.info("[Telescope] POST /telescope/sun-center/start")

    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Telescope not connected"}), 503

        alpaca = get_alpaca_client()
        if not alpaca or not alpaca.is_connected():
            return (
                jsonify(
                    {
                        "error": "ALPACA not connected — Sun centering needs ALPACA mount control"
                    }
                ),
                503,
            )

        mode = (getattr(client, "_viewing_mode", None) or "").strip().lower()
        if mode not in {"sun", "solar"}:
            msg = (
                "Experimental Sun centering is Solar-only right now. "
                "Switch to Solar mode and try again."
            )
            return (
                jsonify(
                    {
                        "error": msg,
                        "reason": "solar_mode_required_experimental",
                        "current_mode": mode or None,
                        "toast": msg,
                    }
                ),
                409,
            )

        # Ensure detector is available for disk centroid feedback.
        from src.transit_detector import get_detector, start_detector

        det = get_detector()
        if not det or not det.is_running:
            rtsp_url = _ensure_rtsp_ready(client)
            if not rtsp_url:
                return (
                    jsonify(
                        {
                            "error": (
                                "RTSP stream is not ready. Start Solar view and "
                                "retry Sun centering."
                            )
                        }
                    ),
                    503,
                )
            start_detector(rtsp_url, record_on_detect=True)

        adapter = _build_sun_center_adapter()
        service = start_sun_center_service(adapter=adapter)

        body = request.get_json(force=True, silent=True) or {}
        if body:
            service.update_settings(body)

        return jsonify({"success": True, **service.get_status()}), 200
    except Exception as e:
        return handle_error(e)


def stop_sun_centering():
    """POST /telescope/sun-center/stop - stop experimental Sun centering."""
    logger.info("[Telescope] POST /telescope/sun-center/stop")
    try:
        stop_sun_center_service()
        return jsonify({"success": True, "running": False, "state": "stopped"}), 200
    except Exception as e:
        return handle_error(e)


def recenter_sun_centering():
    """POST /telescope/sun-center/recenter - restart acquisition from scratch."""
    logger.info("[Telescope] POST /telescope/sun-center/recenter")
    try:
        service = get_sun_center_service()
        if not service or not service.is_running():
            return (
                jsonify(
                    {
                        "error": "Sun centering is not running",
                        "reason": "sun_center_not_running",
                    }
                ),
                409,
            )

        ok = service.recenter()
        if not ok:
            return jsonify({"error": "Unable to recenter"}), 500
        return jsonify({"success": True, **service.get_status()}), 200
    except Exception as e:
        return handle_error(e)


def get_sun_centering_status():
    """GET /telescope/sun-center/status - read Sun centering service status."""
    try:
        service = get_sun_center_service()
        if not service:
            return (
                jsonify(
                    {
                        "running": False,
                        "state": "stopped",
                        "message": "Sun centering idle",
                        "tolerance_mode": "strict",
                        "error_norm": None,
                        "recovery_attempts": 0,
                    }
                ),
                200,
            )
        return jsonify(service.get_status()), 200
    except Exception as e:
        return handle_error(e)


def update_sun_centering_settings():
    """PATCH /telescope/sun-center/settings - update centering behavior."""
    try:
        service = get_sun_center_service()
        if not service or not service.is_running():
            return (
                jsonify(
                    {
                        "error": "Sun centering is not running",
                        "reason": "sun_center_not_running",
                    }
                ),
                409,
            )
        body = request.get_json(force=True, silent=True) or {}
        settings = service.update_settings(body)
        return jsonify({"success": True, "settings": settings, **service.get_status()}), 200
    except Exception as e:
        return handle_error(e)


def _maybe_auto_start_detector(target: str = "sun") -> None:
    """Auto-start the transit detector when entering solar/lunar mode.

    Called internally after every successful start_solar_mode() /
    start_lunar_mode() call so detection is ALWAYS running when the scope is
    pointed at the Sun or Moon — eliminating the silent-miss failure mode
    where the user watches the preview but never clicks "Start Detection".

    If the detector is already running this is a no-op.
    """
    try:
        from src.transit_detector import get_detector, start_detector

        det = get_detector()
        if det and det.is_running:
            logger.debug(
                "[Telescope] Auto-detect: already running, skipping auto-start"
            )
            return

        client = get_telescope_client()
        if not client or not client.is_connected():
            logger.debug("[Telescope] Auto-detect: scope not connected, skipping")
            return

        rtsp_url = _ensure_rtsp_ready(client, allow_mode_reassert=False)
        if not rtsp_url:
            global _auto_detect_rtsp_warn_ts
            now = time.monotonic()
            if now - _auto_detect_rtsp_warn_ts >= 20.0:
                _auto_detect_rtsp_warn_ts = now
                logger.warning(
                    "[Telescope] Auto-detect: RTSP unavailable, detector auto-start skipped"
                )
            else:
                logger.debug(
                    "[Telescope] Auto-detect: RTSP unavailable, detector auto-start skipped"
                )
            return

        logger.info(
            f"[Telescope] Auto-detect: starting detector for {target} target "
            f"(RTSP {rtsp_url})"
        )
        start_detector(rtsp_url, record_on_detect=True)
    except Exception as exc:
        logger.warning(f"[Telescope] Auto-detect: failed to auto-start detector: {exc}")


def start_detection():
    """POST /telescope/detect/start - Start real-time transit detection."""
    logger.info("[Telescope] POST /telescope/detect/start")

    try:
        from src.transit_detector import get_detector, start_detector

        # Already running?
        det = get_detector()
        if det and det.is_running:
            return (
                jsonify({"error": "Detection already running", **det.get_status()}),
                409,
            )

        # Build RTSP URL from telescope client or env
        client = get_telescope_client()
        if client and client.is_connected():
            host = client.host
        else:
            host = os.getenv("SEESTAR_HOST")
        if not host:
            return jsonify({"error": "No telescope host configured"}), 400

        rtsp_url = (
            _ensure_rtsp_ready(client)
            if client and client.is_connected()
            else _resolve_rtsp_stream_url(host, timeout_seconds=4)
        )
        if not rtsp_url:
            return (
                jsonify(
                    {
                        "error": "RTSP stream is not ready. Set scope to Sun/Moon view and try again."
                    }
                ),
                503,
            )

        # Optional params
        record = True
        extra_settings = {}
        try:
            if request.is_json and request.json:
                body = request.json
                record = body.get("record_on_detect", True)
                # Accept full settings bundle sent by UI on start
                for key in (
                    "disk_margin_pct",
                    "centre_ratio_min",
                    "consec_frames",
                    "sensitivity_scale",
                    "track_min_mag",
                    "track_min_agree_frac",
                ):
                    if key in body:
                        extra_settings[key] = body[key]
        except Exception:
            pass

        sensitivity_scale = float(extra_settings.pop("sensitivity_scale", 1.0))

        det = start_detector(
            rtsp_url, record_on_detect=record, sensitivity_scale=sensitivity_scale
        )
        if extra_settings:
            det.update_settings(**extra_settings)
        return jsonify({"success": True, **det.get_status()}), 200

    except Exception as e:
        logger.error(f"[Telescope] Failed to start detection: {e}", exc_info=True)
        return handle_error(e)


def stop_detection():
    """POST /telescope/detect/stop - Stop real-time transit detection."""
    logger.info("[Telescope] POST /telescope/detect/stop")

    try:
        from src.transit_detector import get_detector, stop_detector

        det = get_detector()
        if not det or not det.is_running:
            return jsonify({"success": True, "running": False}), 200

        stop_detector()
        return jsonify({"success": True, "running": False}), 200

    except Exception as e:
        logger.error(f"[Telescope] Failed to stop detection: {e}", exc_info=True)
        return handle_error(e)


def get_detection_status():
    """GET /telescope/detect/status - Get detection status."""
    try:
        from src.transit_detector import get_detector

        det = get_detector()
        if not det:
            return (
                jsonify(
                    {
                        "running": False,
                        "detections": 0,
                        "recent_events": [],
                        "settings": {
                            "disk_margin_pct": float(
                                os.getenv("DETECTOR_DISK_MARGIN", "0.25")
                            ),
                            "centre_ratio_min": float(
                                os.getenv("CENTRE_EDGE_RATIO_MIN", "2.5")
                            ),
                            "consec_frames": int(
                                os.getenv("CONSEC_FRAMES_REQUIRED", "7")
                            ),
                            "sensitivity_scale": 1.0,
                        },
                    }
                ),
                200,
            )
        return jsonify(det.get_status()), 200

    except Exception as e:
        return handle_error(e)


def update_detection_settings():
    """PATCH /telescope/detect/settings - Update live detection parameters."""
    try:
        from src.transit_detector import (
            CENTRE_EDGE_RATIO_MIN,
            CONSEC_FRAMES_REQUIRED,
            DISK_MARGIN_PCT,
            TRACK_MIN_AGREE_FRAC,
            TRACK_MIN_MAG,
            get_detector,
        )

        body = request.get_json(force=True) or {}
        det = get_detector()

        if det and det.is_running:
            # Update running detector immediately
            settings = det.update_settings(
                disk_margin_pct=body.get("disk_margin_pct"),
                centre_ratio_min=body.get("centre_ratio_min"),
                consec_frames=body.get("consec_frames"),
                sensitivity_scale=body.get("sensitivity_scale"),
                track_min_mag=body.get("track_min_mag"),
                track_min_agree_frac=body.get("track_min_agree_frac"),
                mf_threshold_frac=body.get("mf_threshold_frac"),
            )
        else:
            # No detector running — just echo back what was sent (used by UI
            # to persist values for the next start)
            settings = {
                "disk_margin_pct": body.get("disk_margin_pct", DISK_MARGIN_PCT),
                "centre_ratio_min": body.get("centre_ratio_min", CENTRE_EDGE_RATIO_MIN),
                "consec_frames": body.get("consec_frames", CONSEC_FRAMES_REQUIRED),
                "sensitivity_scale": body.get("sensitivity_scale", 1.0),
                "track_min_mag": body.get("track_min_mag", TRACK_MIN_MAG),
                "track_min_agree_frac": body.get(
                    "track_min_agree_frac", TRACK_MIN_AGREE_FRAC
                ),
                "mf_threshold_frac": body.get("mf_threshold_frac", 0.70),
            }
        return jsonify({"success": True, "settings": settings}), 200

    except Exception as e:
        return handle_error(e)


def get_detection_events():
    """GET /telescope/detect/events - Get all detection events."""
    try:
        from src.transit_detector import get_detector

        det = get_detector()
        if not det:
            return jsonify({"events": []}), 200

        return (
            jsonify(
                {
                    "events": [e.to_dict() for e in det.events],
                    "total": len(det.events),
                }
            ),
            200,
        )

    except Exception as e:
        return handle_error(e)


# ── Detection Test Harness Endpoints ──────────────────────────────────────────


def _harness_profile(preset: str, target: str):
    """Return (preset_name, analyzer_kwargs, description) for harness runs."""
    mode = str(preset or "default").strip().lower()
    tgt = str(target or "auto").strip().lower()
    if mode == "sensitive":
        # Sensitive profile aims to reduce false negatives for slow/small objects.
        # It relaxes coherence gates and disables static filtering.
        analyzer_kwargs = {
            "diff_threshold": 10,
            "min_blob_pixels": 8,
            "min_travel_px": 14.0 if tgt == "moon" else 16.0,
            "min_speed_px_s": 25.0 if tgt == "moon" else 30.0,
            "apply_static_filter": False,
            "static_threshold_pct": 0.95,
        }
        return (
            "sensitive",
            analyzer_kwargs,
            "Lower speed/travel thresholds and disabled static filtering to catch slower transits.",
        )
    return (
        "default",
        None,
        "Baseline thresholds aligned with production analyzer behavior.",
    )


def harness_inject():
    """POST /telescope/harness/inject — inject a synthetic blob and test detection."""
    try:
        from tests.test_detection_harness import InjectionParams, run_injection_test

        req = request.json or {}
        params = InjectionParams(
            blob_diameter=float(req.get("size", 14)),
            speed_px_per_sec=float(req.get("speed", 300)),
            opacity=float(req.get("opacity", 1.0)),
            aspect_ratio=float(req.get("aspect", 1.5)),
            angle_deg=float(req.get("angle", 30)),
        )
        target = req.get("target", "sun")
        preset, analyzer_kwargs, preset_description = _harness_profile(
            req.get("preset", "default"), target
        )

        r = run_injection_test(
            params,
            target=target,
            analyzer_kwargs=analyzer_kwargs,
        )

        return jsonify(
            {
                "success": True,
                "preset": preset,
                "preset_description": preset_description,
                "detected": r.detected,
                "num_events": r.num_events,
                "gt_start": round(r.ground_truth_start_sec, 2),
                "gt_end": round(r.ground_truth_end_sec, 2),
                "matched_event": r.matched_event,
                "params": {
                    "size": params.blob_diameter,
                    "speed": params.speed_px_per_sec,
                    "opacity": params.opacity,
                    "target": target,
                },
            }
        )
    except Exception as e:
        logger.error(f"[Harness] Inject error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def harness_sweep():
    """POST /telescope/harness/sweep — sweep size × speed to map detection boundaries."""
    try:
        from tests.test_detection_harness import run_sweep

        req = request.json or {}
        target = req.get("target", "sun")
        sizes = req.get("sizes", [6, 10, 14, 20])
        speeds = req.get("speeds", [60, 100, 150, 200, 300])
        preset, analyzer_kwargs, preset_description = _harness_profile(
            req.get("preset", "default"), target
        )

        sweep = run_sweep(
            sizes=[float(s) for s in sizes],
            speeds=[float(s) for s in speeds],
            target=target,
            analyzer_kwargs=analyzer_kwargs,
        )

        def _fmt_num(v):
            fv = float(v)
            return str(int(fv)) if fv.is_integer() else str(fv)

        grid = {}
        rows = []
        for r in sweep.results:
            s_key = _fmt_num(r.params.blob_diameter)
            v_key = _fmt_num(r.params.speed_px_per_sec)
            grid[f"{s_key},{v_key}"] = r.detected
            rows.append(
                {
                    "size": float(r.params.blob_diameter),
                    "speed": float(r.params.speed_px_per_sec),
                    "detected": bool(r.detected),
                }
            )

        return jsonify(
            {
                "success": True,
                "target": target,
                "preset": preset,
                "preset_description": preset_description,
                "sizes": sizes,
                "speeds": speeds,
                "grid": grid,
                "results": rows,
                "total": sweep.total,
                "detected": sweep.detected,
                "missed": sweep.missed,
                "detection_rate": round(sweep.detection_rate, 3),
            }
        )
    except Exception as e:
        logger.error(f"[Harness] Sweep error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def harness_validate():
    """POST /telescope/harness/validate — run analyzer on captured MP4s."""
    try:
        from tests.test_detection_harness import validate_real_videos

        req = request.json or {}
        target = req.get("target", "auto")
        preset, analyzer_kwargs, preset_description = _harness_profile(
            req.get("preset", "default"), target
        )

        captures_dir = os.path.join("static", "captures")
        video_paths = []
        for root, _, files in os.walk(captures_dir):
            for f in sorted(files):
                if (
                    f.lower().endswith(".mp4")
                    and not f.startswith("analyzed_")
                    and not f.endswith("_analyzed.mp4")
                    and not f.endswith("_analyzed_tmp.mp4")
                ):
                    video_paths.append(os.path.join(root, f))

        if not video_paths:
            return jsonify(
                {"success": True, "results": [], "message": "No MP4s found in captures"}
            )

        results = validate_real_videos(
            video_paths, target=target, analyzer_kwargs=analyzer_kwargs
        )

        return jsonify(
            {
                "success": True,
                "preset": preset,
                "preset_description": preset_description,
                "total": len(results),
                "with_events": sum(1 for r in results if r["num_events"] > 0),
                "results": results,
            }
        )
    except Exception as e:
        logger.error(f"[Harness] Validate error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def get_transit_status():
    """
    Get upcoming transit information from the cached monitor.

    Returns cached transit data without making new API calls.
    """
    logger.info("[Telescope] GET /telescope/transit/status")

    try:
        from src.transit_monitor import get_monitor

        monitor = get_monitor()
        return jsonify(monitor.get_transits())

    except Exception as e:
        logger.error(f"[Telescope] Error getting transit status: {e}")
        return jsonify({"success": False, "error": str(e), "transits": []}), 500


def get_armed_status():
    """GET /telescope/armed - Return detection + recording armed state.

    Used by the telescope page to show a warning banner when the scope is in
    solar/lunar mode but the transit detector is NOT running.

    Returns:
        {
          "scope_mode":      "sun" | "moon" | "scenery" | null,
          "detector_running": bool,
          "armed":            bool,   # True only when both conditions met
          "warning":          str | null   # Human-readable warning message
        }
    """
    try:
        from src.transit_detector import get_detector

        client = get_telescope_client()
        scope_mode = None
        if client and client.is_connected() and hasattr(client, "_viewing_mode"):
            scope_mode = client._viewing_mode  # "sun", "moon", "scenery", or None

        det = get_detector()
        detector_running = bool(det and det.is_running)

        solar_lunar = scope_mode in ("sun", "moon")
        armed = solar_lunar and detector_running

        warning = None
        if solar_lunar and not detector_running:
            target_label = "solar" if scope_mode == "sun" else "lunar"
            warning = (
                f"Scope is in {target_label} mode but transit detection is OFF. "
                f"Any aircraft crossing the {'Sun' if scope_mode == 'sun' else 'Moon'} "
                f"will NOT be captured. Click 'Start Detection' to arm."
            )

        return (
            jsonify(
                {
                    "scope_mode": scope_mode,
                    "detector_running": detector_running,
                    "armed": armed,
                    "warning": warning,
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"[Telescope] Error getting armed status: {e}")
        return jsonify({"armed": False, "error": str(e)}), 500


def transit_check():
    """POST /telescope/transit/check - Background flight poll from telescope page.

    When the user has ONLY the telescope page open (no map tab), the normal
    /flights endpoint is never called and TransitRecorder timers are never
    scheduled.  This endpoint replicates the critical server-side work:

    1. Calls get_transits() for the current scope target (sun or moon).
    2. Schedules TransitRecorder timers for any HIGH-probability flights.
    3. Returns a lightweight summary so the telescope page can show an alert.

    The telescope JS calls this every 90 seconds when scope is in solar/lunar
    mode, regardless of whether the map tab is open.
    """
    logger.debug("[Telescope] POST /telescope/transit/check")

    try:
        from src.constants import PossibilityLevel
        from src.transit import get_transits

        client = get_telescope_client()
        scope_mode = None
        if client and client.is_connected() and hasattr(client, "_viewing_mode"):
            scope_mode = client._viewing_mode

        if scope_mode not in ("sun", "moon"):
            return (
                jsonify({"target": scope_mode, "high_transits": [], "checked": False}),
                200,
            )

        latitude, longitude, elevation = get_observer_coordinates()

        transit_data = get_transits(
            latitude=latitude,
            longitude=longitude,
            elevation=elevation,
            target_name=scope_mode,
            data_source="opensky-only",
            enrich=False,
        )

        all_flights = transit_data.get("flights", [])
        high_flights = [
            f
            for f in all_flights
            if f.get("possibility_level") == PossibilityLevel.HIGH.value
        ]

        # Schedule recordings for any HIGH transits (same logic as /flights)
        # Import here to avoid circular import at module level
        try:
            from app import get_transit_recorder  # type: ignore[import]

            _schedule_recordings(high_flights, get_transit_recorder())
        except Exception as rec_exc:
            logger.debug(f"[TransitCheck] Recorder scheduling skipped: {rec_exc}")

        # Build compact summary for UI
        summary = [
            {
                "id": f.get("ident") or f.get("id"),
                "eta_min": round(f.get("time", 0), 1),
                "sep_deg": round(f.get("angular_separation", 0), 2),
                "alt_diff": round(f.get("alt_diff", 0), 2),
                "az_diff": round(f.get("az_diff", 0), 2),
            }
            for f in high_flights
        ]

        return (
            jsonify(
                {
                    "target": scope_mode,
                    "high_transits": summary,
                    "total_flights": len(all_flights),
                    "checked": True,
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"[Telescope] transit_check error: {e}", exc_info=True)
        return jsonify({"error": str(e), "checked": False}), 500
