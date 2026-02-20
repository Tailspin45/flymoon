"""
Telescope control endpoints for Seestar integration.

Provides RESTful web API for controlling the Seestar telescope, including
connection management, viewing modes, video recording, photo capture, 
live preview, and file management.
"""

import os
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any
from flask import request, jsonify, Response
from zoneinfo import ZoneInfo

from src.seestar_client import SeestarClient
from src.astro import CelestialObject
from src.position import get_my_pos
from src.constants import ASTRO_EPHEMERIS
from src import logger

# Get EARTH reference for position calculations
EARTH = ASTRO_EPHEMERIS['earth']


# Mock Telescope Client for Testing

class MockSeestarClient:
    """Mock Seestar client for testing without hardware."""

    def __init__(self, host: str = "mock.telescope", port: int = 4700, timeout: int = 10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._connected = False
        self._recording = False
        self._recording_start_time: Optional[datetime] = None
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
        logger.info("[Mock] Started solar viewing mode")
        return True

    def start_lunar_mode(self) -> bool:
        """Simulate starting lunar mode."""
        import time
        time.sleep(0.3)
        logger.info("[Mock] Started lunar viewing mode")
        return True

    def stop_view_mode(self) -> bool:
        """Simulate stopping view mode."""
        import time
        time.sleep(0.2)
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
                            "size": 1024000
                        }
                    ]
                }
            ]
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
                    ]
                },
                {
                    "name": "Lunar_2026-02-02",
                    "files": [
                        {"name": "moon_213000.mp4", "thn": "moon_213000_thn.jpg"},
                    ]
                }
            ]
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


# Module-level state (singleton pattern)
_telescope_client: Optional[SeestarClient] = None
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

        # Regular mode - use real Seestar client
        host = os.getenv("SEESTAR_HOST")
        if not host:
            return None

        try:
            port = int(os.getenv("SEESTAR_PORT", "4700"))
            timeout = int(os.getenv("SEESTAR_TIMEOUT", "30"))  # Increased from 10 to 30 seconds
            heartbeat = int(os.getenv("SEESTAR_HEARTBEAT_INTERVAL", "3"))  # 3 seconds matches seestar_alp
            _telescope_client = SeestarClient(host=host, port=port, timeout=timeout, heartbeat_interval=heartbeat)
        except Exception as e:
            logger.error(f"[Telescope] Failed to create client: {e}")
            return None

    return _telescope_client


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
        status_code = 500
    elif "already in progress" in error_msg.lower():
        status_code = 409
    elif "no recording" in error_msg.lower():
        status_code = 400
    else:
        status_code = default_code

    logger.error(f"[Telescope] Error: {error_msg}", exc_info=True)
    return jsonify({"error": error_msg}), status_code


# Connection Management Endpoints

def connect_telescope():
    """POST /telescope/connect - Connect to Seestar telescope."""
    logger.info("[Telescope] POST /telescope/connect")

    if not is_enabled():
        return jsonify({
            "error": "Seestar integration is disabled. Set ENABLE_SEESTAR=true in .env"
        }), 400

    try:
        client = get_telescope_client()
        if not client:
            return jsonify({
                "error": "Failed to initialize telescope client. Check SEESTAR_HOST in .env"
            }), 400

        client.connect()

        logger.info(f"[Telescope] Connected to {client.host}:{client.port}")
        return jsonify({
            "success": True,
            "connected": True,
            "host": client.host,
            "port": client.port,
            "message": "Connected to Seestar telescope"
        }), 200

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

        logger.info("[Telescope] Disconnected from telescope")
        return jsonify({
            "success": True,
            "connected": False,
            "message": "Disconnected from telescope"
        }), 200

    except Exception as e:
        return handle_error(e)


def get_telescope_status():
    """GET /telescope/status - Get current telescope status."""
    logger.debug("[Telescope] GET /telescope/status")

    def _get_eclipse_data():
        """Return upcoming eclipse dict or None (never raises)."""
        try:
            from src.eclipse_monitor import get_eclipse_monitor
            lat  = float(os.getenv("OBSERVER_LATITUDE",  "0"))
            lon  = float(os.getenv("OBSERVER_LONGITUDE", "0"))
            elev = float(os.getenv("OBSERVER_ELEVATION", "0"))
            return get_eclipse_monitor().get_upcoming_eclipse(lat, lon, elev)
        except Exception as ex:
            logger.warning(f"[Telescope] Eclipse check failed: {ex}")
            return None

    try:
        client = get_telescope_client()

        if not client or not client.is_connected():
            return jsonify({
                "connected": False,
                "recording": False,
                "enabled": is_enabled(),
                "mock_mode": is_mock_mode(),
                "host": os.getenv("SEESTAR_HOST") if not is_mock_mode() else "mock.telescope",
                "port": int(os.getenv("SEESTAR_PORT", "4700")),
                "eclipse": _get_eclipse_data(),
            }), 200

        status = client.get_status()
        status["enabled"] = is_enabled()
        status["mock_mode"] = is_mock_mode()
        status["eclipse"] = _get_eclipse_data()

        # Add recording duration if recording
        if _recording_state["active"] and _recording_state["start_time"]:
            status["recording_duration"] = (
                datetime.now() - _recording_state["start_time"]
            ).total_seconds()

        return jsonify(status), 200

    except Exception as e:
        # Status endpoint should never fail, return error state
        logger.error(f"[Telescope] Status check error: {e}")
        return jsonify({
            "connected": False,
            "recording": False,
            "enabled": is_enabled(),
            "error": str(e),
            "eclipse": None,
        }), 200


# Viewing Mode Endpoints

def get_current_target():
    """GET /telescope/target - Get current target based on time of day."""
    from datetime import datetime

    hour = datetime.now().hour
    is_daytime = 6 <= hour < 18

    return jsonify({
        "target": "sun" if is_daytime else "moon",
        "is_daytime": is_daytime
    }), 200




# Recording Endpoints

def start_recording():
    """POST /telescope/recording/start - Start video recording from RTSP stream."""
    logger.info("[Telescope] POST /telescope/recording/start")

    try:
        global _recording_state

        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        if _recording_state["active"]:
            return jsonify({
                "error": "Recording already in progress",
                "recording": True
            }), 409

        # Get parameters from request
        duration = 30  # default
        interval = 0   # default (normal video)
        
        if request.is_json:
            duration = int(request.json.get("duration", 30))
            interval = float(request.json.get("interval", 0))

        # Get RTSP stream URL
        rtsp_port = int(os.getenv("SEESTAR_RTSP_PORT", "4554"))
        rtsp_url = f"rtsp://{client.host}:{rtsp_port}/stream"
        
        # Generate filename with timestamp
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mode_suffix = f"_timelapse_{interval}s" if interval > 0 else ""
        filename = f"recording_{timestamp}{mode_suffix}.mp4"
        
        # Create year/month directories
        now = datetime.now()
        year_month_path = os.path.join('static/captures', str(now.year), f"{now.month:02d}")
        os.makedirs(year_month_path, exist_ok=True)
        
        filepath = os.path.join(year_month_path, filename)
        
        logger.info(f"[Telescope] Starting recording: {filepath} (duration={duration}s, interval={interval}s)")
        
        # Build FFmpeg command
        if interval > 0:
            # Timelapse mode: capture frames at specified interval
            cmd = [
                'ffmpeg',
                '-rtsp_transport', 'tcp',
                '-i', rtsp_url,
                '-t', str(duration),
                '-vf', f'fps=1/{interval}',  # 1 frame every N seconds
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-y',
                filepath
            ]
        else:
            # Normal video mode: record at full framerate
            cmd = [
                'ffmpeg',
                '-rtsp_transport', 'tcp',
                '-i', rtsp_url,
                '-t', str(duration),
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-y',
                filepath
            ]
        
        # Start FFmpeg in background
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        _recording_state = {
            "active": True,
            "start_time": datetime.now(),
            "process": process,
            "filepath": filepath,
            "filename": filename,
            "duration": duration
        }

        logger.info(f"[Telescope] Recording started (PID: {process.pid})")
        return jsonify({
            "success": True,
            "recording": True,
            "start_time": _recording_state["start_time"].isoformat(),
            "duration": duration,
            "interval": interval,
            "message": "Recording started"
        }), 200

    except Exception as e:
        logger.error(f"[Telescope] Failed to start recording: {e}", exc_info=True)
        return handle_error(e)


def stop_recording():
    """POST /telescope/recording/stop - Stop video recording."""
    logger.info("[Telescope] POST /telescope/recording/stop")

    try:
        global _recording_state

        if not _recording_state["active"]:
            return jsonify({
                "error": "No recording in progress",
                "recording": False
            }), 400

        # Calculate duration
        duration = 0
        if _recording_state["start_time"]:
            duration = (datetime.now() - _recording_state["start_time"]).total_seconds()

        # Terminate FFmpeg process
        if "process" in _recording_state and _recording_state["process"]:
            process = _recording_state["process"]
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            logger.info(f"[Telescope] FFmpeg process terminated")
        
        filename = _recording_state.get("filename", "unknown")
        filepath = _recording_state.get("filepath", "")
        
        # Create metadata
        if filepath and os.path.exists(filepath):
            metadata = {
                "timestamp": _recording_state["start_time"].isoformat() if _recording_state["start_time"] else None,
                "duration": duration,
                "source": "rtsp_stream",
                "type": "video"
            }
            
            metadata_path = filepath.rsplit('.', 1)[0] + '.json'
            with open(metadata_path, 'w') as f:
                import json
                json.dump(metadata, f, indent=2)

        _recording_state = {
            "active": False,
            "start_time": None
        }

        logger.info(f"[Telescope] Recording stopped: {filename} (duration: {duration:.1f}s)")
        return jsonify({
            "success": True,
            "recording": False,
            "duration": duration,
            "filename": filename,
            "message": "Recording stopped"
        }), 200

    except Exception as e:
        logger.error(f"[Telescope] Failed to stop recording: {e}", exc_info=True)
        return handle_error(e)


def get_recording_status():
    """GET /telescope/recording/status - Get current recording status."""
    logger.debug("[Telescope] GET /telescope/recording/status")

    try:
        status = {
            "recording": _recording_state["active"]
        }

        if _recording_state["active"] and _recording_state["start_time"]:
            status["duration"] = (
                datetime.now() - _recording_state["start_time"]
            ).total_seconds()
            status["start_time"] = _recording_state["start_time"].isoformat()

        return jsonify(status), 200

    except Exception as e:
        return handle_error(e)


# File Management Endpoint

def list_telescope_files():
    """GET /telescope/files - List locally captured files."""
    logger.info("[Telescope] GET /telescope/files")

    try:
        # List files from local captures directory
        captures_path = 'static/captures'
        files = []
        
        if os.path.exists(captures_path):
            # Walk through captures directory
            for root, dirs, filenames in os.walk(captures_path):
                for filename in filenames:
                    if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.mp4', '.avi')):
                        full_path = os.path.join(root, filename)
                        rel_path = os.path.relpath(full_path, 'static')
                        
                        # Get file modification time
                        mtime = os.path.getmtime(full_path)
                        
                        files.append({
                            "name": filename,
                            "url": f"/static/{rel_path.replace(os.sep, '/')}",
                            "mtime": mtime
                        })
        
        # Sort by modification time (newest first)
        files.sort(key=lambda x: x['mtime'], reverse=True)
        
        logger.info(f"[Telescope] Retrieved {len(files)} local files")
        return jsonify({
            "files": files,
            "total": len(files),
            "source": "local_captures"
        }), 200

    except Exception as e:
        logger.error(f"[Telescope] Error listing files: {e}")
        return handle_error(e)


def delete_telescope_file():
    """POST /telescope/files/delete - Delete a captured file."""
    logger.info("[Telescope] POST /telescope/files/delete")

    try:
        if not request.is_json:
            return jsonify({"error": "Request must be JSON"}), 400
        
        file_path = request.json.get('path')
        if not file_path:
            return jsonify({"error": "Missing 'path' parameter"}), 400
        
        # Security: ensure path is within captures directory
        full_path = os.path.join('static', file_path)
        abs_path = os.path.abspath(full_path)
        captures_abs = os.path.abspath('static/captures')
        
        if not abs_path.startswith(captures_abs):
            logger.warning(f"[Telescope] Attempted to delete file outside captures: {file_path}")
            return jsonify({"error": "Invalid file path"}), 403
        
        # Delete image file
        if os.path.exists(abs_path):
            os.remove(abs_path)
            logger.info(f"[Telescope] Deleted file: {file_path}")
        else:
            return jsonify({"error": "File not found"}), 404
        
        # Delete metadata file if exists
        metadata_path = abs_path.rsplit('.', 1)[0] + '.json'
        if os.path.exists(metadata_path):
            os.remove(metadata_path)
            logger.info(f"[Telescope] Deleted metadata: {metadata_path}")
        
        return jsonify({
            "success": True,
            "message": f"Deleted {os.path.basename(file_path)}"
        }), 200

    except Exception as e:
        logger.error(f"[Telescope] Error deleting file: {e}")
        return handle_error(e)


# Photo Capture Endpoint

def capture_photo():
    """POST /telescope/capture/photo - Capture a single photo from live stream."""
    logger.info("[Telescope] POST /telescope/capture/photo")

    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        # Get RTSP stream URL
        rtsp_port = int(os.getenv("SEESTAR_RTSP_PORT", "4554"))
        rtsp_url = f"rtsp://{client.host}:{rtsp_port}/stream"
        
        # Generate filename with timestamp
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"capture_{timestamp}.jpg"
        
        # Create year/month directories
        now = datetime.now()
        year_month_path = os.path.join('static/captures', str(now.year), f"{now.month:02d}")
        os.makedirs(year_month_path, exist_ok=True)
        
        filepath = os.path.join(year_month_path, filename)
        
        logger.info(f"[Telescope] Capturing frame from RTSP stream to {filepath}")
        
        # Use FFmpeg to grab a single frame from RTSP stream
        cmd = [
            'ffmpeg',
            '-rtsp_transport', 'tcp',
            '-i', rtsp_url,
            '-frames:v', '1',  # Capture only 1 frame
            '-update', '1',  # Required for single image output
            '-q:v', '2',  # High quality JPEG
            '-y',  # Overwrite if exists
            filepath
        ]
        
        # Run FFmpeg with timeout
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10
        )
        
        if result.returncode != 0:
            logger.error(f"[Telescope] FFmpeg capture failed: {result.stderr.decode()}")
            return jsonify({"error": "Failed to capture frame from stream"}), 500
        
        # Create metadata
        metadata = {
            "timestamp": now.isoformat(),
            "source": "live_stream",
            "telescope": client.host,
            "viewing_mode": client._viewing_mode if hasattr(client, '_viewing_mode') else None
        }
        
        metadata_path = filepath.rsplit('.', 1)[0] + '.json'
        with open(metadata_path, 'w') as f:
            import json
            json.dump(metadata, f, indent=2)
        
        # Build web path for response
        rel_path = os.path.relpath(filepath, 'static').replace('\\', '/')
        
        logger.info(f"[Telescope] Photo captured successfully: {filename}")
        return jsonify({
            "success": True,
            "filename": filename,
            "path": rel_path,
            "url": f"/static/{rel_path}",
            "message": "Photo captured from live stream"
        }), 200

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
        
        # Get observer position from environment
        latitude = float(os.getenv("OBSERVER_LATITUDE", "0"))
        longitude = float(os.getenv("OBSERVER_LONGITUDE", "0"))
        elevation = float(os.getenv("OBSERVER_ELEVATION", "0"))
        
        # Create observer position
        observer_position = get_my_pos(
            lat=latitude,
            lon=longitude,
            elevation=elevation,
            base_ref=EARTH
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
        
        logger.debug(f"[Telescope] Sun: {sun_coords['altitude']:.1f}°, Moon: {moon_coords['altitude']:.1f}°")
        
        return jsonify({
            "sun": {
                "altitude": float(sun_coords["altitude"]),
                "azimuth": float(sun_coords["azimuthal"]),
                "visible": sun_visible
            },
            "moon": {
                "altitude": float(moon_coords["altitude"]),
                "azimuth": float(moon_coords["azimuthal"]),
                "visible": moon_visible
            },
            "timestamp": ref_datetime.isoformat()
        }), 200

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

        # Get observer position
        latitude = float(os.getenv("OBSERVER_LATITUDE", "0"))
        longitude = float(os.getenv("OBSERVER_LONGITUDE", "0"))
        elevation = float(os.getenv("OBSERVER_ELEVATION", "0"))
        
        observer_position = get_my_pos(
            lat=latitude,
            lon=longitude,
            elevation=elevation,
            base_ref=EARTH
        )

        # Check if Sun is visible
        from tzlocal import get_localzone
        local_tz = get_localzone()
        ref_datetime = datetime.now(local_tz)
        
        sun = CelestialObject(name="sun", observer_position=observer_position)
        sun.update_position(ref_datetime=ref_datetime)
        sun_coords = sun.get_coordinates()
        
        if sun_coords["altitude"] <= 0:
            return jsonify({
                "error": "Sun is below horizon",
                "altitude": sun_coords["altitude"],
                "visible": False
            }), 400

        # Switch to solar mode
        client.start_solar_mode()

        logger.info("[Telescope] Switched to solar viewing mode")
        return jsonify({
            "success": True,
            "target": "sun",
            "altitude": sun_coords["altitude"],
            "azimuth": sun_coords["azimuthal"],
            "message": "⚠️ SOLAR FILTER REQUIRED - Ensure solar filter is installed before viewing!",
            "warning": "solar_filter_required"
        }), 200

    except Exception as e:
        return handle_error(e)


def switch_to_moon():
    """POST /telescope/target/moon - Switch telescope to lunar viewing mode."""
    logger.info("[Telescope] POST /telescope/target/moon")

    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        # Get observer position
        latitude = float(os.getenv("OBSERVER_LATITUDE", "0"))
        longitude = float(os.getenv("OBSERVER_LONGITUDE", "0"))
        elevation = float(os.getenv("OBSERVER_ELEVATION", "0"))
        
        observer_position = get_my_pos(
            lat=latitude,
            lon=longitude,
            elevation=elevation,
            base_ref=EARTH
        )

        # Check if Moon is visible
        from tzlocal import get_localzone
        local_tz = get_localzone()
        ref_datetime = datetime.now(local_tz)
        
        moon = CelestialObject(name="moon", observer_position=observer_position)
        moon.update_position(ref_datetime=ref_datetime)
        moon_coords = moon.get_coordinates()
        
        if moon_coords["altitude"] <= 0:
            return jsonify({
                "error": "Moon is below horizon",
                "altitude": moon_coords["altitude"],
                "visible": False
            }), 400

        # Switch to lunar mode
        client.start_lunar_mode()

        logger.info("[Telescope] Switched to lunar viewing mode")
        return jsonify({
            "success": True,
            "target": "moon",
            "altitude": moon_coords["altitude"],
            "azimuth": moon_coords["azimuthal"],
            "message": "✓ Remove solar filter if installed - Lunar viewing safe without filter",
            "warning": "remove_solar_filter"
        }), 200

    except Exception as e:
        return handle_error(e)


# Live Preview Stream Endpoint

def telescope_preview_stream():
    """GET /telescope/preview/stream.mjpg - MJPEG live preview stream from RTSP."""
    logger.info("[Telescope] GET /telescope/preview/stream.mjpg - Starting MJPEG stream")

    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        # Get RTSP stream URL
        rtsp_port = int(os.getenv("SEESTAR_RTSP_PORT", "4554"))
        rtsp_url = f"rtsp://{client.host}:{rtsp_port}/stream"
        
        logger.info(f"[Telescope] Transcoding RTSP stream: {rtsp_url}")

        def generate_mjpeg():
            """Generate MJPEG frames from RTSP stream using FFmpeg."""
            # FFmpeg command to transcode RTSP → individual JPEG frames
            cmd = [
                'ffmpeg',
                '-rtsp_transport', 'tcp',
                '-i', rtsp_url,
                '-f', 'image2pipe',
                '-vcodec', 'mjpeg',
                '-q:v', '5',
                '-r', '10',
                '-update', '1',
                'pipe:1'
            ]
            
            process = None
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0
                )
                
                logger.info(f"[Telescope] FFmpeg process started (PID: {process.pid})")
                
                # Buffer for accumulating data
                buffer = b''
                
                while True:
                    # Read data in small chunks
                    chunk = process.stdout.read(4096)
                    if not chunk:
                        break
                    
                    buffer += chunk
                    
                    # Look for complete JPEG frames (start: FF D8, end: FF D9)
                    while True:
                        # Find JPEG start
                        start_idx = buffer.find(b'\xff\xd8')
                        if start_idx == -1:
                            break
                        
                        # Find JPEG end after start
                        end_idx = buffer.find(b'\xff\xd9', start_idx + 2)
                        if end_idx == -1:
                            # Incomplete frame, keep buffering
                            break
                        
                        # Extract complete JPEG frame
                        jpeg_frame = buffer[start_idx:end_idx + 2]
                        buffer = buffer[end_idx + 2:]
                        
                        # Yield MJPEG frame
                        yield (b'--frame\r\n'
                               b'Content-Type: image/jpeg\r\n'
                               b'Content-Length: ' + str(len(jpeg_frame)).encode() + b'\r\n'
                               b'\r\n' + jpeg_frame + b'\r\n')
                
            except GeneratorExit:
                logger.info("[Telescope] Client disconnected from stream")
            except Exception as e:
                logger.error(f"[Telescope] FFmpeg stream error: {e}")
            finally:
                if process:
                    process.kill()
                    process.wait()
                    logger.info("[Telescope] FFmpeg process terminated")

        return Response(
            generate_mjpeg(),
            mimetype='multipart/x-mixed-replace; boundary=frame'
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
        except:
            pass
        _telescope_client = None
    
    logger.info(f"[Telescope] Simulation mode {'enabled' if _simulate_mode else 'disabled'}")
    
    return jsonify({
        "success": True,
        "simulate_mode": _simulate_mode,
        "message": f"Simulation mode {'enabled' if _simulate_mode else 'disabled'}"
    }), 200


def get_simulate_status():
    """GET /telescope/simulate - Get simulation mode status."""
    return jsonify({
        "simulate_mode": _simulate_mode
    }), 200


# Route Registration Helper

def register_routes(app):
    """
    Register all telescope routes with the Flask app.

    Args:
        app: Flask application instance
    """
    # Connection management
    app.add_url_rule("/telescope/connect", "telescope_connect",
                     connect_telescope, methods=["POST"])
    app.add_url_rule("/telescope/disconnect", "telescope_disconnect",
                     disconnect_telescope, methods=["POST"])
    app.add_url_rule("/telescope/status", "telescope_status",
                     get_telescope_status, methods=["GET"])

    # Target info and visibility
    app.add_url_rule("/telescope/target", "telescope_get_target",
                     get_current_target, methods=["GET"])
    app.add_url_rule("/telescope/target/visibility", "telescope_target_visibility",
                     get_target_visibility, methods=["GET"])
    app.add_url_rule("/telescope/target/sun", "telescope_switch_sun",
                     switch_to_sun, methods=["POST"])
    app.add_url_rule("/telescope/target/moon", "telescope_switch_moon",
                     switch_to_moon, methods=["POST"])

    # Recording
    app.add_url_rule("/telescope/recording/start", "telescope_recording_start",
                     start_recording, methods=["POST"])
    app.add_url_rule("/telescope/recording/stop", "telescope_recording_stop",
                     stop_recording, methods=["POST"])
    app.add_url_rule("/telescope/recording/status", "telescope_recording_status",
                     get_recording_status, methods=["GET"])

    # Photo capture
    app.add_url_rule("/telescope/capture/photo", "telescope_capture_photo",
                     capture_photo, methods=["POST"])

    # Live preview
    app.add_url_rule("/telescope/preview/stream.mjpg", "telescope_preview_stream",
                     telescope_preview_stream, methods=["GET"])

    # File management
    app.add_url_rule("/telescope/files", "telescope_files",
                     list_telescope_files, methods=["GET"])
    app.add_url_rule("/telescope/files/delete", "telescope_files_delete",
                     delete_telescope_file, methods=["POST"])
    
    # Transit monitoring
    app.add_url_rule("/telescope/transit/status", "telescope_transit_status",
                     get_transit_status, methods=["GET"])

    # Simulation mode
    app.add_url_rule("/telescope/simulate", "telescope_simulate_toggle",
                     toggle_simulate_mode, methods=["POST"])
    app.add_url_rule("/telescope/simulate", "telescope_simulate_status",
                     get_simulate_status, methods=["GET"])

    logger.info("[Telescope] Routes registered")


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
        return jsonify({
            'success': False,
            'error': str(e),
            'transits': []
        }), 500
