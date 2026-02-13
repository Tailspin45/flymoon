"""
Direct Seestar telescope control via JSON-RPC over TCP.

This module provides lightweight, direct communication with Seestar telescopes
without requiring external bridge applications like seestar_alp. It uses the
native JSON-RPC 2.0 protocol over TCP sockets.

Based on protocol reverse-engineering from:
https://github.com/smart-underworld/seestar_alp/blob/main/device/seestar_device.py
"""

import json
import socket
import threading
import time
from typing import Optional, Dict, Any
from datetime import datetime

from src import logger


class SeestarClient:
    """Direct TCP client for Seestar telescope using JSON-RPC 2.0 protocol."""

    # Default connection parameters
    DEFAULT_PORT = 4700
    DEFAULT_TIMEOUT = 10
    DEFAULT_HEARTBEAT_INTERVAL = 3  # Ping every 3 seconds to prevent timeout (matches seestar_alp)

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        timeout: int = DEFAULT_TIMEOUT,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
    ):
        """
        Initialize Seestar client.

        Parameters
        ----------
        host : str
            IP address of the Seestar telescope
        port : int
            TCP port (default: 4700, may vary by firmware)
        timeout : int
            Socket timeout in seconds (default: 10)
        heartbeat_interval : int
            Seconds between heartbeat messages (default: 3)
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval

        self.socket: Optional[socket.socket] = None
        self._connected = False
        self._recording = False
        self._recording_start_time: Optional[datetime] = None
        self._viewing_mode: Optional[str] = None  # Track current viewing mode (sun/moon/None)
        self._message_id = 0
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_running = False
        self._socket_lock = threading.Lock()  # Prevent concurrent socket access

        logger.info(f"Initialized Seestar client for {host}:{port}")

    def _get_next_id(self) -> int:
        """Get next message ID for JSON-RPC requests."""
        self._message_id += 1
        return self._message_id

    def _send_command(
        self,
        method: str,
        params: Any = None,
        expect_response: bool = True,
        timeout_override: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Send JSON-RPC command to Seestar.

        Parameters
        ----------
        method : str
            JSON-RPC method name (e.g., "iscope_start_view")
        params : any, optional
            Method parameters (dict, list, or simple value)
        expect_response : bool
            Whether to wait for and return response (default: True)
        timeout_override : int, optional
            Override the default timeout for this command

        Returns
        -------
        dict or None
            Response data if expect_response=True, None otherwise

        Raises
        ------
        RuntimeError
            If not connected or communication fails
        """
        if not self._connected or not self.socket:
            raise RuntimeError("Not connected to Seestar")

        # Use lock to prevent concurrent socket access (heartbeat vs commands)
        with self._socket_lock:
            # Build Seestar JSON message
            # Note: Seestar does NOT use standard JSON-RPC 2.0 format for requests
            # The "jsonrpc" field only appears in responses, not requests
            message = {
                "method": method,
                "id": self._get_next_id(),
            }

            if params is not None:
                message["params"] = params

            try:
                # Send message with \r\n delimiter
                data = json.dumps(message) + "\r\n"
                self.socket.sendall(data.encode())
                logger.debug(f"Sent: {method} (id={message['id']})")

                if expect_response:
                    # Seestar sends multiple types of messages:
                    # 1. Responses with "jsonrpc" field (what we want)
                    # 2. Event messages with "Event" field (unsolicited)
                    # We need to loop and skip Event messages until we find our response

                    start_time = time.time()
                    buffer = ""
                    
                    # Use timeout override if provided, otherwise use instance timeout
                    cmd_timeout = timeout_override if timeout_override is not None else self.timeout

                    while time.time() - start_time < cmd_timeout:
                        # Receive data in chunks
                        chunk = self.socket.recv(4096).decode()
                        buffer += chunk

                        # Process complete messages (delimited by \r\n)
                        while "\r\n" in buffer:
                            line, buffer = buffer.split("\r\n", 1)
                            if not line.strip():
                                continue

                            try:
                                result = json.loads(line)

                                # Skip Event messages
                                if "Event" in result:
                                    logger.debug(f"Skipping event: {result.get('Event')}")
                                    continue

                                # Check if this is our response
                                if result.get("id") == message["id"]:
                                    if "error" in result:
                                        error = result["error"]
                                        raise RuntimeError(
                                            f"Seestar error: {error.get('message', 'Unknown error')}"
                                        )
                                    return result.get("result")

                            except json.JSONDecodeError:
                                logger.warning(f"Failed to parse message: {line[:100]}")
                                continue

                    # If we get here, we timed out waiting for response
                    raise RuntimeError(f"Timeout waiting for response to {method}")

                return None

            except socket.timeout:
                # Socket timeout - command took too long, but connection may still be alive
                logger.warning(f"Command timeout: {method}")
                raise RuntimeError("timed out")

            except socket.error as e:
                # Actual socket error - connection is broken
                logger.error(f"Socket error: {e}")
                self._connected = False
                raise RuntimeError(f"Communication failed: {e}")

    def _heartbeat_loop(self):
        """Background thread that sends periodic heartbeat messages."""
        while self._heartbeat_running:
            try:
                # Send heartbeat command and read response to prevent buffer buildup
                # Use scope_get_equ_coord as a simple status check
                self._send_command("scope_get_equ_coord", expect_response=True)
            except Exception as e:
                # Use debug level to avoid spamming logs when telescope is disconnected
                logger.debug(f"Heartbeat failed: {e}")
                # If heartbeat fails, mark as disconnected
                if "broken pipe" in str(e).lower() or "connection" in str(e).lower():
                    self._connected = False

            # Sleep in small intervals to allow quick shutdown
            for _ in range(self.heartbeat_interval):
                if not self._heartbeat_running:
                    break
                time.sleep(1)

    def connect(self) -> bool:
        """
        Connect to Seestar telescope.

        Returns
        -------
        bool
            True if connection successful

        Raises
        ------
        RuntimeError
            If connection fails
        """
        if self._connected:
            logger.warning("Already connected")
            return True

        try:
            # Create TCP socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(self.timeout)

            # Connect to Seestar
            logger.info(f"Connecting to Seestar at {self.host}:{self.port}...")
            self.socket.connect((self.host, self.port))
            self._connected = True
            logger.info("Connected to Seestar")

            # NOTE: Initialization may be needed depending on use case
            # For solar/lunar video: send iscope_start_view after connect
            # Example: self.start_solar_mode() or self.start_lunar_mode()

            # Start heartbeat thread
            self._heartbeat_running = True
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()

            return True

        except socket.error as e:
            logger.error(f"Failed to connect to Seestar: {e}")
            if self.socket:
                self.socket.close()
                self.socket = None
            raise RuntimeError(f"Connection failed: {e}")

    def disconnect(self) -> bool:
        """
        Disconnect from Seestar telescope.

        Returns
        -------
        bool
            True if disconnection successful
        """
        if not self._connected:
            logger.warning("Not connected")
            return True

        try:
            # Stop recording if active
            if self._recording:
                self.stop_recording()

            # Stop heartbeat thread
            if self._heartbeat_running:
                self._heartbeat_running = False
                if self._heartbeat_thread:
                    self._heartbeat_thread.join(timeout=5)

            # TODO: Send disconnect/cleanup command if required
            # Example: self._send_command("iscope_stop_view")

            # Close socket
            if self.socket:
                self.socket.close()
                self.socket = None

            self._connected = False
            logger.info("Disconnected from Seestar")
            return True

        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
            return False

    def is_connected(self) -> bool:
        """
        Check if connected to telescope.

        Returns
        -------
        bool
            True if connected
        """
        return self._connected

    def start_recording(self, duration_seconds: Optional[int] = None) -> bool:
        """
        Start video recording on Seestar.

        Parameters
        ----------
        duration_seconds : int, optional
            Duration to record in seconds. If None, records until stop_recording() is called.

        Returns
        -------
        bool
            True if recording started successfully

        Raises
        ------
        RuntimeError
            If not connected or recording fails to start

        Notes
        -----
        Uses the 'start_record_avi' JSON-RPC method to begin video recording.
        The Seestar must already be in viewing mode (solar or lunar) before calling this.

        Workflow:
        1. Call start_solar_mode() or start_lunar_mode()
        2. Call this method to start recording
        3. Call stop_recording() when done
        4. Use list_files() to get recorded file paths
        5. Download via HTTP: http://<host>/<path>/<filename>

        The recording is saved as MP4 (processed video) on the Seestar.
        """
        if not self._connected:
            raise RuntimeError("Cannot start recording: not connected to telescope")

        if self._recording:
            logger.warning("Recording already in progress")
            return True

        try:
            # Start video recording with start_record_avi
            # raw=False gives us processed MP4 video
            params = {"raw": False}
            _ = self._send_command("start_record_avi", params=params)

            self._recording = True
            self._recording_start_time = datetime.now()
            logger.info(f"Started video recording (MP4 format, duration: {duration_seconds}s)")

            # If duration specified, schedule auto-stop
            if duration_seconds:
                timer = threading.Timer(duration_seconds, self.stop_recording)
                timer.daemon = True
                timer.start()
                logger.info(f"⏱️ Auto-stop scheduled in {duration_seconds}s")

            return True

        except Exception as e:
            logger.error(f"Failed to start recording: {e}")
            raise

    def stop_recording(self) -> bool:
        """
        Stop video recording on Seestar.

        Returns
        -------
        bool
            True if recording stopped successfully

        Notes
        -----
        Uses the 'stop_record_avi' JSON-RPC method to stop video recording.
        The recorded video is saved on the Seestar and can be retrieved via HTTP
        by first calling list_files() to get the file path.
        """
        if not self._recording:
            logger.warning("No recording in progress")
            return True

        try:
            duration = None
            if self._recording_start_time:
                duration = (datetime.now() - self._recording_start_time).total_seconds()

            # Stop video recording with stop_record_avi
            _ = self._send_command("stop_record_avi")

            logger.info(f"Stopped recording (duration: {duration:.1f}s)")

            self._recording = False
            self._recording_start_time = None
            return True

        except Exception as e:
            logger.error(f"Failed to stop recording: {e}")
            return False

    def is_recording(self) -> bool:
        """
        Check if currently recording.

        Returns
        -------
        bool
            True if recording in progress
        """
        return self._recording

    def start_solar_mode(self) -> bool:
        """
        Start solar viewing mode.

        Returns
        -------
        bool
            True if mode started successfully

        Notes
        -----
        This must be called before video recording for solar transits.
        Seestar will switch to solar viewing mode with appropriate filter.
        iscope_start_view may not send immediate response, so we don't wait.
        """
        try:
            # Don't expect immediate response - Seestar may take time to switch modes
            self._send_command("iscope_start_view", params={"mode": "sun"}, expect_response=False)
            self._viewing_mode = "sun"
            logger.info("Started solar viewing mode (async)")
            # Give telescope time to process the command
            time.sleep(1)
            return True
        except Exception as e:
            logger.error(f"Failed to start solar mode: {e}")
            raise

    def start_lunar_mode(self) -> bool:
        """
        Start lunar viewing mode.

        Returns
        -------
        bool
            True if mode started successfully

        Notes
        -----
        This must be called before video recording for lunar transits.
        Seestar will switch to lunar viewing mode.
        iscope_start_view may not send immediate response, so we don't wait.
        """
        try:
            # Don't expect immediate response - Seestar may take time to switch modes
            self._send_command("iscope_start_view", params={"mode": "moon"}, expect_response=False)
            self._viewing_mode = "moon"
            logger.info("Started lunar viewing mode (async)")
            # Give telescope time to process the command
            time.sleep(1)
            return True
        except Exception as e:
            logger.error(f"Failed to start lunar mode: {e}")
            raise

    def stop_view_mode(self) -> bool:
        """
        Stop current viewing mode.

        Returns
        -------
        bool
            True if mode stopped successfully
        """
        try:
            _ = self._send_command("iscope_stop_view")
            logger.info("Stopped viewing mode")
            return True
        except Exception as e:
            logger.error(f"Failed to stop view mode: {e}")
            return False

    def capture_photo(self, exposure_time: float = 1.0) -> dict:
        """
        Capture a single photo (light frame).

        Parameters
        ----------
        exposure_time : float
            Exposure time in seconds (default: 1.0)

        Returns
        -------
        dict
            Response from telescope with result info

        Raises
        ------
        RuntimeError
            If not connected or not in viewing mode

        Notes
        -----
        Uses the 'pi_output_set_target' JSON-RPC method to capture current view.
        This works in Solar, Lunar, and Scenery viewing modes.

        Workflow:
        1. Ensure telescope is in Solar, Lunar, or Scenery mode (via Seestar app or Flymoon)
        2. Call this method to capture a single photo
        3. Use get_albums() to retrieve the saved image
        4. Download via HTTP: http://<host>/<path>/<filename>

        The photo is saved to "My Album" on the Seestar device.
        """
        if not self._connected:
            raise RuntimeError("Cannot capture photo: not connected to telescope")

        try:
            # Use pi_output_set_target to capture current view (works in viewing modes)
            # This is the command used by Seestar app during solar/lunar viewing
            params = {"target_name": ""}
            
            # Extended timeout - Seestar takes time to capture and save
            result = self._send_command("pi_output_set_target", params=params, timeout_override=90)
            logger.info(f"📸 Captured photo")
            
            # Alternative: If above doesn't work, the telescope might need to be triggered differently
            # The Seestar saves frames automatically during viewing, so we might not need explicit capture
            return result if result else {"success": True}

        except Exception as e:
            logger.error(f"Failed to capture photo: {e}")
            raise

    def get_albums(self) -> dict:
        """
        Get albums and images from Seestar storage.

        Returns
        -------
        dict
            Albums structure with images

        Notes
        -----
        Returns format:
        {
            "path": "<parent_folder>",
            "list": [
                {
                    "name": "<album_name>",
                    "files": [
                        {
                            "name": "<filename>",
                            "thn": "<thumbnail_path>",
                            "size": <file_size>,
                            ...
                        },
                        ...
                    ]
                },
                ...
            ]
        }

        Use this after capture_photo() to retrieve the saved image path.
        Files can be downloaded via HTTP: http://<host>/<path>/<filename>
        
        If telescope times out or has no albums, returns empty structure.
        """
        try:
            response = self._send_command("get_albums")
            if response is None:
                # Command succeeded but no response data
                logger.warning("get_albums returned None, returning empty structure")
                return {"path": "", "list": []}
            
            logger.info(f"Retrieved albums: {len(response.get('list', []))} albums")
            return response
        except RuntimeError as e:
            if "timed out" in str(e).lower() or "timeout" in str(e).lower():
                # Timeout is not fatal - just means no albums or telescope busy
                logger.warning(f"get_albums timed out, returning empty structure")
                return {"path": "", "list": []}
            else:
                logger.error(f"Failed to get albums: {e}")
                raise
        except Exception as e:
            logger.error(f"Failed to get albums: {e}")
            raise

    def list_files(self) -> dict:
        """
        List recorded files on Seestar (alias for get_albums).

        Returns
        -------
        dict
            Albums/files structure with paths

        Notes
        -----
        Returns format:
        {
            "path": "<parent_folder>",
            "list": [
                {
                    "name": "<album_name>",
                    "files": [{"name": "<filename>", "thn": "<thumbnail>", ...}, ...]
                },
                ...
            ]
        }

        Files can be downloaded via HTTP:
        http://<seestar_host>/<path>/<filename>
        """
        return self.get_albums()

    def get_status(self) -> Dict[str, Any]:
        """
        Get telescope status information.

        Returns
        -------
        dict
            Status information including connection and recording state
        """
        status = {
            "connected": self._connected,
            "recording": self._recording,
            "viewing_mode": self._viewing_mode,
            "host": self.host,
            "port": self.port,
        }

        if self._recording_start_time:
            status["recording_duration"] = (
                datetime.now() - self._recording_start_time
            ).total_seconds()

        return status


class TransitRecorder:
    """Helper class for automated transit recording with timing buffers."""

    def __init__(
        self,
        seestar_client: SeestarClient,
        pre_buffer_seconds: int = 10,
        post_buffer_seconds: int = 10,
    ):
        """
        Initialize transit recorder.

        Parameters
        ----------
        seestar_client : SeestarClient
            Connected Seestar client instance
        pre_buffer_seconds : int
            Seconds to start recording before predicted transit (default: 10)
        post_buffer_seconds : int
            Seconds to continue recording after predicted transit (default: 10)
        """
        self.client = seestar_client
        self.pre_buffer = pre_buffer_seconds
        self.post_buffer = post_buffer_seconds
        self._scheduled_recordings: Dict[str, threading.Timer] = {}

        logger.info(
            f"Transit recorder initialized (pre={pre_buffer_seconds}s, post={post_buffer_seconds}s)"
        )

    def schedule_transit_recording(
        self,
        flight_id: str,
        eta_seconds: float,
        transit_duration_estimate: float = 2.0
    ) -> bool:
        """
        Schedule automated recording for a predicted transit.

        Parameters
        ----------
        flight_id : str
            Unique identifier for the flight/transit
        eta_seconds : float
            Estimated time until transit in seconds
        transit_duration_estimate : float
            Estimated duration of the transit event in seconds (default: 2.0)

        Returns
        -------
        bool
            True if recording was scheduled successfully

        Notes
        -----
        Aircraft transits are typically 0.5-2 seconds long. The total recording
        duration will be: pre_buffer + transit_duration + post_buffer
        """
        try:
            # Cancel existing timer for this flight if any (prevents memory leak)
            if flight_id in self._scheduled_recordings:
                old_timer = self._scheduled_recordings[flight_id]
                old_timer.cancel()
                logger.debug(f"Cancelled previous timer for {flight_id}")

            # Calculate timing
            start_delay = max(0, eta_seconds - self.pre_buffer)
            total_duration = self.pre_buffer + transit_duration_estimate + self.post_buffer

            logger.info(
                f"Scheduling transit {flight_id}: start in {start_delay:.1f}s, "
                f"record for {total_duration:.1f}s"
            )

            # Schedule start
            start_timer = threading.Timer(start_delay, self._start_recording, args=[flight_id, total_duration])
            start_timer.daemon = True
            start_timer.start()

            self._scheduled_recordings[flight_id] = start_timer
            return True

        except Exception as e:
            logger.error(f"Failed to schedule transit recording: {e}")
            return False

    def _start_recording(self, flight_id: str, duration: float):
        """Internal method to start recording and schedule stop."""
        try:
            logger.info(f"Starting recording for transit {flight_id}")
            self.client.start_recording()

            # Schedule stop
            stop_timer = threading.Timer(duration, self._stop_recording, args=[flight_id])
            stop_timer.daemon = True
            stop_timer.start()

        except Exception as e:
            logger.error(f"Failed to start recording for {flight_id}: {e}")

    def _stop_recording(self, flight_id: str):
        """Internal method to stop recording."""
        try:
            logger.info(f"Stopping recording for transit {flight_id}")
            self.client.stop_recording()

            # Remove from scheduled recordings
            if flight_id in self._scheduled_recordings:
                del self._scheduled_recordings[flight_id]

        except Exception as e:
            logger.error(f"Failed to stop recording for {flight_id}: {e}")

    def cancel_all(self):
        """Cancel all scheduled recordings."""
        for flight_id, timer in list(self._scheduled_recordings.items()):
            timer.cancel()
            logger.info(f"Cancelled recording for {flight_id}")
        self._scheduled_recordings.clear()


def create_client_from_env() -> Optional[SeestarClient]:
    """
    Create Seestar client from environment variables.

    Reads configuration from .env file:
    - SEESTAR_HOST: IP address of Seestar (required)
    - SEESTAR_PORT: TCP port (default: 4700)
    - SEESTAR_TIMEOUT: Socket timeout in seconds (default: 10)
    - ENABLE_SEESTAR: Enable/disable Seestar integration (default: false)

    Returns
    -------
    SeestarClient or None
        Client instance if enabled, None if disabled or config missing
    """
    import os

    if os.getenv("ENABLE_SEESTAR", "false").lower() != "true":
        logger.info("Seestar integration disabled (ENABLE_SEESTAR=false)")
        return None

    host = os.getenv("SEESTAR_HOST")
    if not host:
        logger.error("SEESTAR_HOST not configured")
        return None

    try:
        port = int(os.getenv("SEESTAR_PORT", str(SeestarClient.DEFAULT_PORT)))
        timeout = int(os.getenv("SEESTAR_TIMEOUT", str(SeestarClient.DEFAULT_TIMEOUT)))
        heartbeat = int(os.getenv("SEESTAR_HEARTBEAT_INTERVAL", str(SeestarClient.DEFAULT_HEARTBEAT_INTERVAL)))

        client = SeestarClient(host=host, port=port, timeout=timeout, heartbeat_interval=heartbeat)
        logger.info(f"Created Seestar client from environment: {host}:{port} (heartbeat: {heartbeat}s)")
        return client

    except Exception as e:
        logger.error(f"Failed to create Seestar client from environment: {e}")
        return None
