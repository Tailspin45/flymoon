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
from typing import Optional, Dict, Any, Callable
from datetime import datetime

from src import logger


class SeestarClient:
    """Direct TCP client for Seestar telescope using JSON-RPC 2.0 protocol."""

    # Default connection parameters
    DEFAULT_PORT = 4700
    DEFAULT_TIMEOUT = 10

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        timeout: int = DEFAULT_TIMEOUT,
        heartbeat_interval: int = 30,
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
            Seconds between heartbeat messages (default: 30)
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval

        self.socket: Optional[socket.socket] = None
        self._connected = False
        self._recording = False
        self._recording_start_time: Optional[datetime] = None
        self._message_id = 0
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_running = False

        logger.info(f"Initialized Seestar client for {host}:{port}")

    def _get_next_id(self) -> int:
        """Get next message ID for JSON-RPC requests."""
        self._message_id += 1
        return self._message_id

    def _send_command(
        self,
        method: str,
        params: Any = None,
        expect_response: bool = True
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

                while time.time() - start_time < self.timeout:
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

                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse message: {line[:100]}")
                            continue

                # If we get here, we timed out waiting for response
                raise RuntimeError(f"Timeout waiting for response to {method}")

            return None

        except socket.error as e:
            logger.error(f"Socket error: {e}")
            self._connected = False
            raise RuntimeError(f"Communication failed: {e}")

    def _heartbeat_loop(self):
        """Background thread that sends periodic heartbeat messages."""
        logger.info("Heartbeat thread started")
        while self._heartbeat_running:
            try:
                # Send heartbeat command (based on seestar_alp implementation)
                self._send_command("scope_get_equ_coord", expect_response=False)
                logger.debug("Heartbeat sent")
            except Exception as e:
                logger.warning(f"Heartbeat failed: {e}")

            # Sleep in small intervals to allow quick shutdown
            for _ in range(self.heartbeat_interval):
                if not self._heartbeat_running:
                    break
                time.sleep(1)

        logger.info("Heartbeat thread stopped")

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
            response = self._send_command("start_record_avi", params=params)

            self._recording = True
            self._recording_start_time = datetime.now()
            logger.info(f"Started video recording (MP4 format)")

            # TODO: If duration specified, schedule auto-stop
            # if duration_seconds:
            #     threading.Timer(duration_seconds, self.stop_recording).start()

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
            response = self._send_command("stop_record_avi")

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
        """
        try:
            response = self._send_command("iscope_start_view", params={"mode": "sun"})
            logger.info("Started solar viewing mode")
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
        """
        try:
            response = self._send_command("iscope_start_view", params={"mode": "moon"})
            logger.info("Started lunar viewing mode")
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
            response = self._send_command("iscope_stop_view")
            logger.info("Stopped viewing mode")
            return True
        except Exception as e:
            logger.error(f"Failed to stop view mode: {e}")
            return False

    def list_files(self) -> dict:
        """
        List recorded files on Seestar.

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
        try:
            response = self._send_command("get_albums")
            logger.info(f"Retrieved file list: {len(response.get('list', []))} albums")
            return response
        except Exception as e:
            logger.error(f"Failed to list files: {e}")
            raise

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
            f"Transit recorder initialized (pre={pre_buffer}s, post={post_buffer}s)"
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

        client = SeestarClient(host=host, port=port, timeout=timeout)
        logger.info(f"Created Seestar client from environment: {host}:{port}")
        return client

    except Exception as e:
        logger.error(f"Failed to create Seestar client from environment: {e}")
        return None
