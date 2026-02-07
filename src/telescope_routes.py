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

    def start_solar_mode(self) -> bool:
        """Simulate starting solar viewing mode."""
        if not self._connected:
            raise RuntimeError("Cannot start solar mode: not connected")
        logger.info("[Mock] Started solar viewing mode")
        return True

    def start_lunar_mode(self) -> bool:
        """Simulate starting lunar viewing mode."""
        if not self._connected:
            raise RuntimeError("Cannot start lunar mode: not connected")
        logger.info("[Mock] Started lunar viewing mode")
        return True

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


# Helper Functions

def is_enabled() -> bool:
    """Check if Seestar integration is enabled via environment variable."""
    return os.getenv("ENABLE_SEESTAR", "false").lower() == "true"


def is_mock_mode() -> bool:
    """Check if mock telescope mode is enabled."""
    return os.getenv("MOCK_TELESCOPE", "false").lower() == "true"


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

    try:
        client = get_telescope_client()

        if not client or not client.is_connected():
            return jsonify({
                "connected": False,
                "recording": False,
                "enabled": is_enabled(),
                "mock_mode": is_mock_mode(),
                "host": os.getenv("SEESTAR_HOST") if not is_mock_mode() else "mock.telescope",
                "port": int(os.getenv("SEESTAR_PORT", "4700"))
            }), 200

        status = client.get_status()
        status["enabled"] = is_enabled()
        status["mock_mode"] = is_mock_mode()

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
            "error": str(e)
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
    """POST /telescope/recording/start - Start video recording."""
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

        # Get optional duration from request body
        duration = None
        if request.is_json and "duration" in request.json:
            duration = request.json.get("duration")

        client.start_recording(duration)
        _recording_state = {
            "active": True,
            "start_time": datetime.now()
        }

        logger.info(f"[Telescope] Recording started (duration: {duration}s)")
        return jsonify({
            "success": True,
            "recording": True,
            "start_time": _recording_state["start_time"].isoformat(),
            "message": "Recording started"
        }), 200

    except Exception as e:
        return handle_error(e)


def stop_recording():
    """POST /telescope/recording/stop - Stop video recording."""
    logger.info("[Telescope] POST /telescope/recording/stop")

    try:
        global _recording_state

        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        if not _recording_state["active"]:
            return jsonify({
                "error": "No recording in progress",
                "recording": False
            }), 400

        # Calculate duration
        duration = 0
        if _recording_state["start_time"]:
            duration = (datetime.now() - _recording_state["start_time"]).total_seconds()

        client.stop_recording()
        _recording_state = {
            "active": False,
            "start_time": None
        }

        logger.info(f"[Telescope] Recording stopped (duration: {duration:.1f}s)")
        return jsonify({
            "success": True,
            "recording": False,
            "duration": duration,
            "message": "Recording stopped"
        }), 200

    except Exception as e:
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
    """GET /telescope/files - List recorded files on telescope."""
    logger.info("[Telescope] GET /telescope/files")

    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        files_data = client.list_files()

        # Build download URLs for files
        base_url = f"http://{client.host}"
        parent_path = files_data.get("path", "")
        albums = []
        total_files = 0

        for album in files_data.get("list", []):
            album_name = album.get("name", "")
            album_files = []

            for file_info in album.get("files", []):
                filename = file_info.get("name", "")
                if filename:
                    # Construct download URL
                    url = f"{base_url}/{parent_path}/{album_name}/{filename}"
                    album_files.append({
                        "name": filename,
                        "thumbnail": file_info.get("thn", ""),
                        "url": url
                    })
                    total_files += 1

            albums.append({
                "name": album_name,
                "files": album_files
            })

        logger.info(f"[Telescope] Retrieved {total_files} files from {len(albums)} albums")
        return jsonify({
            "success": True,
            "path": parent_path,
            "albums": albums,
            "total_files": total_files
        }), 200

    except Exception as e:
        return handle_error(e)


# Photo Capture Endpoint

def capture_photo():
    """POST /telescope/capture/photo - Capture a single photo."""
    logger.info("[Telescope] POST /telescope/capture/photo")

    try:
        client = get_telescope_client()
        if not client or not client.is_connected():
            return jsonify({"error": "Not connected to telescope"}), 400

        # Get optional exposure time from request body
        exposure_time = 1.0
        if request.is_json and "exposure_time" in request.json:
            exposure_time = float(request.json.get("exposure_time", 1.0))

        # Capture photo
        result = client.capture_photo(exposure_time)
        
        # Get the latest image from albums
        albums = client.get_albums()
        latest_image = None
        
        if albums.get("list") and len(albums["list"]) > 0:
            album = albums["list"][0]
            if album.get("files") and len(album["files"]) > 0:
                latest_file = album["files"][0]
                parent_path = albums.get("path", "")
                album_name = album.get("name", "")
                filename = latest_file.get("name", "")
                
                # Construct download URL
                latest_image = {
                    "url": f"http://{client.host}/{parent_path}/{album_name}/{filename}",
                    "filename": filename,
                    "thumbnail": latest_file.get("thn", "")
                }

        logger.info(f"[Telescope] Photo captured (exposure: {exposure_time}s)")
        return jsonify({
            "success": True,
            "exposure_time": exposure_time,
            "image": latest_image,
            "message": "Photo captured successfully"
        }), 200

    except Exception as e:
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
            # FFmpeg command to transcode RTSP → MJPEG
            cmd = [
                'ffmpeg',
                '-i', rtsp_url,
                '-f', 'mjpeg',
                '-q:v', '5',  # JPEG quality (2-31, lower is better)
                '-r', '10',   # 10 fps
                '-'  # Output to stdout
            ]
            
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=10**8
                )
                
                # Read MJPEG frames
                while True:
                    # Read frame size (JPEG markers)
                    frame = process.stdout.read(1024 * 100)  # Read up to 100KB chunks
                    if not frame:
                        break
                    
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                
            except Exception as e:
                logger.error(f"[Telescope] FFmpeg stream error: {e}")
            finally:
                if process:
                    process.kill()
                    process.wait()

        return Response(
            generate_mjpeg(),
            mimetype='multipart/x-mixed-replace; boundary=frame'
        )

    except Exception as e:
        logger.error(f"[Telescope] Preview stream failed: {e}")
        return jsonify({"error": str(e)}), 500


# File Management Endpoints

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

    logger.info("[Telescope] Routes registered")
