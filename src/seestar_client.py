"""
Direct Seestar telescope control via JSON-RPC over TCP.

This module provides lightweight, direct communication with Seestar telescopes
without requiring external bridge applications like seestar_alp. It uses the
native JSON-RPC 2.0 protocol over TCP sockets.

Based on protocol reverse-engineering from:
https://github.com/smart-underworld/seestar_alp/blob/main/device/seestar_device.py
"""

import json
import os
import socket
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from src import logger
from src.site_context import get_observer_coordinates

_telemetry_warned_zero_observer = False
_telemetry_warned_scope_offset = False
_DEBUG_LOG_PATH = "/Users/Tom/flymoon/.cursor/debug-616e1a.log"
_DEBUG_SESSION_ID = "616e1a"


def _debug_log(
    run_id: str, hypothesis_id: str, location: str, message: str, data: dict
) -> None:
    try:
        payload = {
            "sessionId": _DEBUG_SESSION_ID,
            "id": f"log_{int(time.time() * 1000)}_{threading.get_ident()}",
            "timestamp": int(time.time() * 1000),
            "location": location,
            "message": message,
            "data": data,
            "runId": run_id,
            "hypothesisId": hypothesis_id,
        }
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _skyfield_target_altaz(
    target: str, lat: float, lon: float, elev: float
) -> tuple[float, float]:
    """Compute alt/az for Sun or Moon using Skyfield ephemeris.

    Returns (alt_degrees, az_degrees) — same convention as
    ``_altaz_from_equatorial_for_goto`` (north=0, clockwise).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from skyfield.api import wgs84
    from tzlocal import get_localzone_name

    from src.constants import ASTRO_EPHEMERIS, EARTH_TIMESCALE

    observer = ASTRO_EPHEMERIS["earth"] + wgs84.latlon(lat, lon, elevation_m=elev)
    body = ASTRO_EPHEMERIS[target]
    t = EARTH_TIMESCALE.now()
    alt, az, _ = observer.at(t).observe(body).apparent().altaz()
    return alt.degrees, az.degrees


def _altaz_from_equatorial_for_goto(
    ra_hours: float,
    dec_degrees: float,
    observer_lat: float,
    observer_lon: float,
    gast_hours: float,
) -> tuple[float, float]:
    """
    Alt/az (degrees) from RA/Dec using the exact inverse of ``goto_altaz``.

    Azimuth: clockwise from true north through east (astronomical); south ≈ 180°.

    ``scope_get_equ_coord`` returns the mount/firmware equatorial system used for
    GoTo. Feeding those numbers into Skyfield ``Star`` (ICRS) + ``apparent().altaz()``
    mis-modeled the frame and could show ~correct altitude but azimuth ~180° wrong.
    """
    import math

    lat_r = math.radians(observer_lat)
    dec_r = math.radians(dec_degrees)
    lst = (gast_hours + observer_lon / 15.0) % 24.0
    ha_h = (lst - ra_hours) % 24.0
    if ha_h > 12.0:
        ha_h -= 24.0
    ha_r = ha_h * (2.0 * math.pi / 24.0)

    sin_alt = (
        math.sin(lat_r) * math.sin(dec_r)
        + math.cos(lat_r) * math.cos(dec_r) * math.cos(ha_r)
    )
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt_r = math.asin(sin_alt)

    y = -math.sin(ha_r) * math.cos(dec_r)
    x = math.cos(lat_r) * math.sin(dec_r) - math.sin(lat_r) * math.cos(
        dec_r
    ) * math.cos(ha_r)
    az_r = math.atan2(y, x)
    az_deg = math.degrees(az_r) % 360.0
    alt_deg = math.degrees(alt_r)
    return alt_deg, az_deg


class SeestarClient:
    """Direct TCP client for Seestar telescope using JSON-RPC 2.0 protocol."""

    # Default connection parameters
    DEFAULT_PORT = 4700
    DEFAULT_TIMEOUT = 10
    DEFAULT_HEARTBEAT_INTERVAL = (
        3  # Ping every 3 seconds to prevent timeout (matches seestar_alp)
    )
    DEFAULT_RETRY_ATTEMPTS = 3
    DEFAULT_RETRY_INITIAL_DELAY = 1  # seconds

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        timeout: int = DEFAULT_TIMEOUT,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        retry_initial_delay: float = DEFAULT_RETRY_INITIAL_DELAY,
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
        retry_attempts : int
            Number of connection retry attempts (default: 3)
        retry_initial_delay : float
            Initial delay in seconds before first retry (default: 1)
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval
        self.retry_attempts = retry_attempts
        self.retry_initial_delay = retry_initial_delay

        self.socket: Optional[socket.socket] = None
        self._connected = False
        self._recording = False
        self._recording_start_time: Optional[datetime] = None
        self._viewing_mode: Optional[str] = (
            None  # Track current viewing mode (sun/moon/None)
        )
        self._message_id = 0
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_running = False
        self._socket_lock = threading.Lock()  # Prevent concurrent socket access
        self._cmd_seq_lock = threading.RLock()  # Prevent heartbeat interleaving multi-step commands
        self._above_horizon_check = (
            None  # Optional[Callable[[], bool]] — set by app startup
        )

        # Cached telemetry — populated by heartbeat loop, served to HTTP clients
        self._cached_telemetry: Dict[str, Any] = {}
        self._cached_telemetry_ts: float = 0.0

        logger.info(f"Initialized Seestar client for {host}:{port}")

    def _get_next_id(self) -> int:
        """Get next message ID for JSON-RPC requests."""
        self._message_id += 1
        return self._message_id

    def _persist_host_to_env(self, new_host: str) -> None:
        """Persist discovered SEESTAR_HOST to .env for future launches."""
        env_path = os.getenv("FLYMOON_ENV_PATH", ".env")
        try:
            lines = []
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as fh:
                    lines = fh.readlines()

            updated = False
            for i, line in enumerate(lines):
                if line.startswith("SEESTAR_HOST="):
                    lines[i] = f"SEESTAR_HOST={new_host}\n"
                    updated = True
                    break

            if not updated:
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.append(f"SEESTAR_HOST={new_host}\n")

            with open(env_path, "w", encoding="utf-8") as fh:
                fh.writelines(lines)

            logger.info(
                f"[Seestar] Persisted discovered host to {env_path}: {new_host}"
            )
        except OSError as e:
            logger.warning(f"[Seestar] Failed to persist SEESTAR_HOST to .env: {e}")

    def _send_command(
        self,
        method: str,
        params: Any = None,
        expect_response: bool = True,
        timeout_override: Optional[int] = None,
        quiet: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Send JSON-RPC command to Seestar.

        Parameters
        ----------
        quiet : bool
            If True, demote timeout/socket errors to DEBUG level instead of
            WARNING/ERROR.  Used by the heartbeat loop to avoid log spam when
            the telescope is unreachable.

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
            # Firmware > v2582 silently drops commands without "verify".
            # seestar_alp injects this on every outgoing message.
            message["verify"] = True

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
                    cmd_timeout = (
                        timeout_override
                        if timeout_override is not None
                        else self.timeout
                    )
                    # Also adjust socket recv timeout to match so it doesn't fire early
                    if timeout_override is not None:
                        self.socket.settimeout(timeout_override)

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

                                # Parse and handle Event messages (state updates)
                                if "Event" in result:
                                    self._handle_event(result)
                                    continue

                                # Check if this is our response
                                if result.get("id") == message["id"]:
                                    if "error" in result:
                                        error = result["error"]
                                        msg = error.get("message", "Unknown error") if isinstance(error, dict) else str(error)
                                        raise RuntimeError(
                                            f"Seestar error: {msg}"
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
                if quiet:
                    logger.debug(f"Command timeout: {method}")
                else:
                    logger.warning(f"Command timeout: {method}")
                raise RuntimeError("timed out")

            except socket.error as e:
                if quiet:
                    logger.debug(f"Socket error: {e}")
                else:
                    logger.warning(f"Socket error in _send_command: {e}")
                # Connection reset by peer — mark disconnected so heartbeat reconnects
                if e.errno in (54, 104):  # ECONNRESET (macOS=54, Linux=104)
                    self._connected = False
                raise RuntimeError(f"Communication failed: {e}")

            finally:
                # Always restore the default socket timeout after command
                if self.socket:
                    self.socket.settimeout(self.timeout)

    # ── Known event names from the Seestar firmware ──────────────────────────
    # Discovered via live traffic capture. New events are logged at DEBUG level
    # so they appear in logs when --debug is active, making future discovery easy.
    _VIEW_START_EVENTS = {"ImagingViewStart", "SolarViewStart", "LunarViewStart", "SceneryViewStart"}
    _VIEW_STOP_EVENTS = {"ImagingViewStop", "SolarViewStop", "LunarViewStop", "SceneryViewStop"}

    def _handle_event(self, event: dict) -> None:
        """Parse unsolicited Event messages from the Seestar firmware.

        Updates internal state (e.g. _viewing_mode) so the app stays in sync
        with whatever mode the scope is actually in — even when it was set via
        the Seestar app before Flymoon connected.
        """
        name = event.get("Event", "")

        # Viewing-mode state changes
        if name in ("SolarViewStart",):
            if self._viewing_mode != "sun":
                self._viewing_mode = "sun"
                logger.info("Seestar event: solar viewing mode active")
        elif name in ("LunarViewStart",):
            if self._viewing_mode != "moon":
                self._viewing_mode = "moon"
                logger.info("Seestar event: lunar viewing mode active")
        elif name in ("SceneryViewStart",):
            if self._viewing_mode != "scenery":
                self._viewing_mode = "scenery"
                logger.info("Seestar event: scenery viewing mode active")
        elif name in self._VIEW_STOP_EVENTS:
            if self._viewing_mode is not None:
                logger.info(
                    f"Seestar event: viewing mode stopped (was {self._viewing_mode})"
                )
                self._viewing_mode = None
        # Recording state changes
        elif name == "RecordingStart":
            if not self._recording:
                self._recording = True
                logger.info("Seestar event: recording started")
        elif name == "RecordingStop":
            if self._recording:
                self._recording = False
                self._recording_start_time = None
                logger.info("Seestar event: recording stopped")
        else:
            # Log unknown events at DEBUG so they're visible during testing
            logger.debug(f"Seestar event: {name} {event}")

    def _reconnect(self) -> bool:
        """Attempt to re-establish the TCP connection without starting a new heartbeat thread.
        Called from within the heartbeat thread after a drop is detected.
        Runs auto-discovery when the configured host fails (e.g. scope got new DHCP lease)."""
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None

        for attempt in range(2):  # First try configured host, then discover
            try:
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.socket.settimeout(self.timeout)
                self.socket.connect((self.host, self.port))
                self._connected = True
                logger.info("Reconnected to Seestar")
                self._notify_scope_online()
                return True
            except socket.error as e:
                if self.socket:
                    try:
                        self.socket.close()
                    except Exception:
                        pass
                    self.socket = None
                if attempt == 0:
                    discovered = self._auto_discover()
                    if discovered and discovered != self.host:
                        logger.warning(
                            f"[Seestar] Reconnect: discovered at {discovered} "
                            f"(was {self.host}). Updating."
                        )
                        self.host = discovered
                        self._persist_host_to_env(discovered)
                        continue
                logger.warning(f"Reconnect attempt failed: {e}")
                return False
        return False

    def _heartbeat_loop(self):
        """Background thread: sends periodic keepalive pings and auto-reconnects on drop."""
        RECONNECT_INTERVAL = 5  # seconds between reconnect attempts
        HARD_FAIL_THRESHOLD = 3  # consecutive hard socket errors → disconnect
        reconnect_wait = 0
        hard_fail_count = 0
        _timeout_logged = False  # log timeout once, not every 3 seconds

        while self._heartbeat_running:
            # Auto-reconnect when connection has dropped
            if not self._connected:
                # Don't attempt reconnect when Sun and Moon are both below the horizon —
                # the scope won't be in use and the warnings would be noise.
                if self._above_horizon_check is not None:
                    try:
                        if not self._above_horizon_check():
                            time.sleep(60)  # check again in a minute
                            continue
                    except Exception:
                        pass  # fail open
                reconnect_wait += 1
                if reconnect_wait >= RECONNECT_INTERVAL:
                    reconnect_wait = 0
                    logger.info("Heartbeat: connection lost — attempting reconnect...")
                    if self._reconnect():  # sets _connected = True on success
                        hard_fail_count = 0
                        _timeout_logged = False
                time.sleep(1)
                continue

            reconnect_wait = 0  # reset backoff once connected

            # Skip heartbeat if a multi-step command sequence holds the lock
            if not self._cmd_seq_lock.acquire(blocking=False):
                time.sleep(1)
                continue
            try:
                # Gather full telemetry on every heartbeat cycle so HTTP
                # clients can read cached data without hitting the socket.
                telemetry = self.get_telemetry()
                self._cached_telemetry = telemetry
                self._cached_telemetry_ts = time.time()
                hard_fail_count = 0  # successful ping
                if _timeout_logged:
                    logger.info("Heartbeat: scope responding again")
                    _timeout_logged = False
            except Exception as e:
                err = str(e).lower()
                is_hard_error = any(
                    kw in err
                    for kw in (
                        "broken pipe",
                        "connection reset",
                        "connection refused",
                        "communication failed",
                    )
                )

                if is_hard_error:
                    hard_fail_count += 1
                    if hard_fail_count >= HARD_FAIL_THRESHOLD:
                        logger.warning(
                            f"Heartbeat: {hard_fail_count} consecutive hard errors — marking disconnected: {e}"
                        )
                        if self._connected:
                            self._connected = False
                            self._notify_scope_offline()
                        else:
                            self._connected = False
                    else:
                        logger.warning(
                            f"Heartbeat: hard error ({hard_fail_count}/{HARD_FAIL_THRESHOLD}): {e}"
                        )
                else:
                    # Timeouts / busy — scope is alive but not answering this command.
                    # Don't count toward disconnect; log once to avoid spam.
                    hard_fail_count = 0  # timeout proves TCP is up, reset hard counter
                    if not _timeout_logged:
                        logger.info(
                            f"Heartbeat: scope not responding to ping (will keep trying quietly): {e}"
                        )
                        _timeout_logged = True
            finally:
                self._cmd_seq_lock.release()

            # Sleep in small intervals to allow quick shutdown
            for _ in range(self.heartbeat_interval):
                if not self._heartbeat_running:
                    break
                time.sleep(1)

    def _notify_scope_offline(self):
        """Fire-and-forget Telegram alert when scope drops off the network."""

        def _send():
            import asyncio

            try:
                from src.telegram_notify import send_telegram_simple

                asyncio.run(
                    send_telegram_simple(
                        "🔴 <b>SCOPE DISCONNECTED</b>\n"
                        "⚠️ Seestar telescope connection lost.\n"
                        "<i>Any pending transit recordings will not be captured.</i>"
                    )
                )
            except Exception as e:
                logger.debug(f"Scope-offline Telegram alert failed: {e}")

        t = threading.Thread(target=_send, daemon=True)
        t.start()

    def _notify_scope_online(self):
        """Fire-and-forget Telegram alert when scope reconnects."""

        def _send():
            import asyncio

            try:
                from src.telegram_notify import send_telegram_simple

                asyncio.run(
                    send_telegram_simple(
                        "🟢 <b>Scope reconnected</b>\n"
                        "✅ Seestar telescope connection re-established.\n"
                        "<i>Transit recording is active again.</i>"
                    )
                )
            except Exception as e:
                logger.debug(f"Scope-online Telegram alert failed: {e}")

        t = threading.Thread(target=_send, daemon=True)
        t.start()

    def _quick_reachable(self, host: str, timeout: float = 2.0) -> bool:
        """Return True if host:port accepts a TCP connection within timeout."""
        try:
            with socket.create_connection((host, self.port), timeout=timeout):
                return True
        except Exception:
            return False

    def _send_init_sequence(self) -> None:
        """
        Send ALP-style post-connect initialization to the scope.

        ALP always sends these three commands right after TCP connect:
          1. set_user_location  — syncs the scope's GPS/location
          2. pi_set_time        — syncs the scope's RTC to UTC
          3. pi_is_verified     — session handshake (some firmware requires this)

        Failures are logged at WARNING (not DEBUG) so init problems are
        visible without ``--debug``.  A failed init is the most common
        cause of 180° azimuth errors (scope falls back to stale/wrong
        internal coordinates).
        """
        from datetime import datetime, timezone

        # 1. Location sync (same site as map → /api/settings, else .env)
        lat, lon, _elev = get_observer_coordinates()
        try:
            result = self._send_command(
                "set_user_location",
                params={"lat": lat, "lon": lon, "force": True},
                quiet=True,
                timeout_override=5,
            )
            logger.info(f"[Init] set_user_location lat={lat} lon={lon} → {result}")
        except Exception as e:
            logger.warning(f"[Init] set_user_location FAILED (lat={lat} lon={lon}): {e}")

        # 2. Clock sync
        try:
            now = datetime.now(timezone.utc)
            result = self._send_command(
                "pi_set_time",
                params=[{
                    "year": now.year, "mon": now.month, "day": now.day,
                    "hour": now.hour, "min": now.minute, "sec": now.second,
                    "time_zone": "UTC",
                }],
                quiet=True,
                timeout_override=5,
            )
            logger.info(f"[Init] pi_set_time {now.strftime('%Y-%m-%dT%H:%M:%SZ')} → {result}")
        except Exception as e:
            logger.warning(f"[Init] pi_set_time FAILED: {e}")

        # 3. Session verification
        try:
            self._send_command(
                "pi_is_verified",
                quiet=True,
                timeout_override=5,
            )
            logger.debug("[Init] pi_is_verified sent")
        except Exception as e:
            logger.debug(f"[Init] pi_is_verified failed (non-fatal): {e}")

        # 4. Read back scope's stored location to confirm it matches
        try:
            r = self._send_command(
                "get_device_state",
                params={"keys": ["location_lon_lat"]},
                quiet=True,
                timeout_override=5,
            )
            if r:
                scope_loc = r.get("location_lon_lat") or r.get("result", {}).get("location_lon_lat")
                if scope_loc:
                    logger.info(f"[Init] Scope location readback: {scope_loc}")
                else:
                    logger.info(f"[Init] Scope device_state (no location_lon_lat): {r}")
        except Exception as e:
            logger.debug(f"[Init] get_device_state readback failed (non-fatal): {e}")

    def connect(self) -> bool:
        """
        Connect to Seestar telescope with exponential backoff retry.

        Returns
        -------
        bool
            True if connection successful

        Raises
        ------
        RuntimeError
            If connection fails after all retry attempts
        """
        if self._connected:
            logger.warning("Already connected")
            return True

        # Fast pre-check: if the configured host doesn't respond quickly,
        # run auto-discover before the slow retry loop.
        if not self._quick_reachable(self.host):
            logger.info(
                f"[Seestar] {self.host}:{self.port} not immediately reachable, "
                "scanning subnet for Seestar…"
            )
            discovered = self._auto_discover()
            if discovered:
                if discovered != self.host:
                    logger.warning(
                        f"[Seestar] Auto-discovered at {discovered} "
                        f"(was {self.host}). Persisting to .env."
                    )
                    self.host = discovered
                    self._persist_host_to_env(discovered)
            else:
                logger.warning(
                    "[Seestar] Auto-discover found nothing; trying configured host anyway."
                )

        last_error = None
        delay = self.retry_initial_delay

        for attempt in range(1, self.retry_attempts + 1):
            try:
                if attempt > 1:
                    logger.info(
                        f"[Seestar] Connection attempt {attempt}/{self.retry_attempts} "
                        f"(after {delay}s delay)"
                    )
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff

                # Create TCP socket
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.socket.settimeout(self.timeout)

                # Connect to Seestar
                logger.info(
                    f"Connecting to Seestar at {self.host}:{self.port} "
                    f"(attempt {attempt}/{self.retry_attempts})..."
                )
                self.socket.connect((self.host, self.port))
                self._connected = True
                logger.info("Connected to Seestar")

                # Send initialization sequence (mirrors ALP's startup behaviour).
                # This syncs the scope's location and clock, and verifies the
                # session — skipping it can cause commands to be silently ignored
                # on some firmware versions.
                self._send_init_sequence()

                # Start heartbeat thread
                self._heartbeat_running = True
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop, daemon=True
                )
                self._heartbeat_thread.start()

                return True

            except Exception as e:
                last_error = e
                logger.warning(f"[Seestar] Connection attempt {attempt} failed: {e}")

                # Always clean up the socket on ANY failure — not just
                # socket.error.  Leaked sockets consume one of the Seestar's
                # 8 connection slots and are never recovered.
                self._connected = False
                if self.socket:
                    try:
                        self.socket.close()
                    except Exception:
                        pass
                    self.socket = None

                # On host-down / unreachable errors, try auto-discovering the
                # Seestar on the local subnet before retrying.
                if isinstance(e, (socket.error, socket.timeout)):
                    import errno as _errno

                    host_down = hasattr(e, "errno") and e.errno in (
                        _errno.EHOSTDOWN,
                        _errno.EHOSTUNREACH,
                        _errno.ECONNREFUSED,
                        64,
                    )
                    timed_out = isinstance(e, socket.timeout) or (
                        hasattr(e, "errno") and e.errno == _errno.ETIMEDOUT
                    )

                    if (host_down or timed_out) and attempt < self.retry_attempts:
                        discovered = self._auto_discover()
                        if discovered and discovered != self.host:
                            logger.warning(
                                f"[Seestar] Auto-discovered at {discovered} "
                                f"(was {self.host}). Persisting to .env."
                            )
                            self.host = discovered
                            self._persist_host_to_env(discovered)
                        continue

        # All retries exhausted
        error_msg = f"Connection failed after {self.retry_attempts} attempts"
        if last_error:
            error_msg += f": {last_error}"
        raise RuntimeError(error_msg)

    def _udp_discover(self, timeout: float = 2.0) -> Optional[str]:
        """
        Send a UDP broadcast scan_iscope on port 4720 (ALP protocol).

        The Seestar replies to this broadcast regardless of which subnet it is
        on, so this works even when the scope is in AP mode on 192.168.7.x
        while the host machine is on a different /24.  Returns the responding
        IP, or None if no reply within timeout.
        """
        import json as _json
        import socket as _socket

        UDP_PORT = 4720
        message = _json.dumps({"id": 1, "method": "scan_iscope", "params": ""}) + "\r\n"
        payload = message.encode()

        sock = None
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)
            sock.settimeout(timeout)
            sock.bind(("", 0))
            sock.sendto(payload, ("255.255.255.255", UDP_PORT))
            logger.info(f"[Seestar] UDP scan_iscope broadcast sent on port {UDP_PORT}")

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(1024)
                    responder_ip = addr[0]
                    # Ignore our own loopback
                    if not responder_ip.startswith("127."):
                        logger.info(f"[Seestar] UDP discovery: scope replied from {responder_ip}")
                        return responder_ip
                except _socket.timeout:
                    break
        except Exception as e:
            logger.debug(f"[Seestar] UDP discovery error (non-fatal): {e}")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        return None

    def _auto_discover(self) -> str:
        """
        Discover the Seestar on the local network.

        Tries UDP broadcast first (fast, works across subnets/AP mode), then
        falls back to a TCP /24 port scan of all local interfaces.
        Returns the first found IP, or None.
        """
        import concurrent.futures
        import socket as _socket

        # --- Pass 1: UDP broadcast (ALP scan_iscope protocol) ---
        # This reaches the scope even if it is on its own AP subnet (192.168.7.x)
        # because the broadcast goes out on all interfaces at L2.
        udp_ip = self._udp_discover(timeout=2.0)
        if udp_ip:
            return udp_ip

        logger.info("[Seestar] UDP discovery found nothing; falling back to TCP subnet scan…")

        # --- Pass 2: TCP /24 port scan on all local interface subnets ---
        # Collect all unique /24 prefixes from local interfaces.
        bases = set()
        try:
            hostname = _socket.gethostname()
            for info in _socket.getaddrinfo(hostname, None, _socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127."):
                    bases.add(ip.rsplit(".", 1)[0])
        except Exception:
            pass

        # Fallback: probe outbound interface
        if not bases:
            try:
                probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                probe.connect(("8.8.8.8", 80))
                ip = probe.getsockname()[0]
                probe.close()
                bases.add(ip.rsplit(".", 1)[0])
            except Exception:
                return None

        # Try the previously working host's subnet first
        if self.host:
            known_base = self.host.rsplit(".", 1)[0]
            bases.discard(known_base)
            bases = [known_base] + list(bases)
        else:
            bases = list(bases)

        def _probe(ip):
            try:
                with socket.create_connection((ip, self.port), timeout=0.4):
                    return ip
            except Exception:
                return None

        for base in bases:
            logger.info(f"[Seestar] Scanning {base}.0/24 for port {self.port}…")
            hosts = [f"{base}.{i}" for i in range(1, 255)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
                for ip in pool.map(_probe, hosts):
                    if ip:
                        logger.info(f"[Seestar] Found at {ip}")
                        return ip

        logger.warning("[Seestar] Auto-discover: no host found on any local subnet")
        return None

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
            # Stop recording if active — best-effort, must not prevent socket cleanup
            if self._recording:
                try:
                    self.stop_recording()
                except Exception as e:
                    logger.warning(f"Error stopping recording during disconnect: {e}")

            # Stop heartbeat thread
            self._heartbeat_running = False
            if self._heartbeat_thread:
                self._heartbeat_thread.join(timeout=5)

            logger.info("Disconnected from Seestar")
            return True

        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
            return False

        finally:
            # Socket cleanup runs no matter what — a leaked socket consumes
            # one of the Seestar's 8 connection slots permanently.
            self._connected = False
            self._viewing_mode = None
            if self.socket:
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.socket = None

    def is_connected(self) -> bool:
        """
        Check if connected to telescope.

        Returns
        -------
        bool
            True if connected and socket is alive
        """
        return self._connected and self.socket is not None

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

        # Refuse to record if scope is known to be in a non-solar/lunar mode.
        # If mode is None (unknown, e.g. after reconnect), allow with a warning.
        if self._viewing_mode is not None and self._viewing_mode not in ("sun", "moon", "scenery"):
            raise RuntimeError(
                f"Cannot record: scope is in mode '{self._viewing_mode}' "
                "(must be 'sun' or 'moon'). Point the scope at the target first."
            )
        if self._viewing_mode is None:
            logger.info(
                "Recording in unknown viewing mode (scope connected externally or reconnected) — proceeding"
            )

        try:
            # Start video recording - fire-and-forget: Seestar acks via event, not RPC response
            params = {"raw": False}
            _ = self._send_command(
                "start_record_avi", params=params, expect_response=False
            )

            self._recording = True
            self._recording_start_time = datetime.now()
            logger.info(
                f"Started video recording (MP4 format, duration: {duration_seconds}s)"
            )

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

            # Stop video recording - fire-and-forget like start
            _ = self._send_command("stop_record_avi", expect_response=False)

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
            self._send_command(
                "iscope_start_view", params={"mode": "sun"}, expect_response=False
            )
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
            self._send_command(
                "iscope_start_view", params={"mode": "moon"}, expect_response=False
            )
            self._viewing_mode = "moon"
            logger.info("Started lunar viewing mode (async)")
            # Give telescope time to process the command
            time.sleep(1)
            return True
        except Exception as e:
            logger.error(f"Failed to start lunar mode: {e}")
            raise

    def start_scenery_mode(self) -> bool:
        """Start scenery viewing mode (no sidereal tracking — for manual positioning)."""
        try:
            self._send_command(
                "iscope_start_view", params={"mode": "scenery"}, expect_response=False
            )
            self._viewing_mode = "scenery"
            logger.info("Started scenery viewing mode (async)")
            time.sleep(1)
            return True
        except Exception as e:
            logger.error(f"Failed to start scenery mode: {e}")
            raise

    def stop_view_mode(self) -> bool:
        """Stop current viewing mode (live view, stack, or slew)."""
        import time as _time
        try:
            self._send_command("iscope_stop_view")
            _time.sleep(0.3)
        except Exception:
            pass
        try:
            self._send_command("iscope_stop_view", params={"stage": "Stack"})
            _time.sleep(1.0)
            self._viewing_mode = None
            logger.info("Stopped viewing mode")
            return True
        except Exception as e:
            logger.error(f"Failed to stop view mode: {e}")
            self._viewing_mode = None
            return False

    # ------------------------------------------------------------------ #
    #  Extended telescope control (Option A — native JSON-RPC)           #
    # ------------------------------------------------------------------ #

    def goto_radec(self, ra: float, dec: float) -> dict:
        """Slew to equatorial coordinates (J2000 RA hours, Dec degrees).

        Uses iscope_start_view with mode=star which performs a GoTo + sidereal
        tracking.  The command sequence lock prevents heartbeat interleaving.
        """
        with self._cmd_seq_lock:
            if self._viewing_mode is not None:
                self.stop_view_mode()
                time.sleep(0.5)
            return self.start_view_star(ra, dec, target_name="GoTo Target")

    def goto_altaz(
        self,
        alt: float,
        az: float,
        observer_lat: float,
        observer_lon: float,
        observer_elev: float = 0,
    ) -> dict:
        """
        Slew to an alt/az position by first converting to RA/Dec.

        Parameters
        ----------
        alt, az : float
            Altitude and azimuth in degrees.
        observer_lat, observer_lon : float
            Observer location in decimal degrees.
        observer_elev : float
            Observer elevation in metres.
        """
        from src.constants import EARTH_TIMESCALE
        import math

        t = EARTH_TIMESCALE.now()
        lat_r = math.radians(observer_lat)
        alt_r = math.radians(alt)
        az_r = math.radians(az)

        sin_dec = (math.sin(alt_r) * math.sin(lat_r) +
                   math.cos(alt_r) * math.cos(lat_r) * math.cos(az_r))
        dec_r = math.asin(max(-1.0, min(1.0, sin_dec)))

        cos_ha = (math.sin(alt_r) - math.sin(lat_r) * sin_dec) / (
            math.cos(lat_r) * math.cos(dec_r) + 1e-12
        )
        ha_r = math.acos(max(-1.0, min(1.0, cos_ha)))
        if math.sin(az_r) > 0:
            ha_r = 2 * math.pi - ha_r

        # Local Sidereal Time: GAST + longitude (degrees→hours)
        lst = (t.gast + observer_lon / 15.0) % 24
        ra_h = (lst - ha_r * 12 / math.pi) % 24
        dec_d = math.degrees(dec_r)

        logger.info(
            f"AltAz ({alt:.2f}°, {az:.2f}°) → RA {ra_h:.4f}h Dec {dec_d:.4f}° (LST {lst:.4f}h)"
        )
        return self.goto_radec(ra_h, dec_d)

    # ── Manual joystick slew (bypasses firmware horizon limit) ──────────

    # Mechanical safety limits (degrees)
    _SLEW_ALT_MIN = -45
    _SLEW_ALT_MAX = 85
    # Speed thresholds
    _SLEW_FAST_SPEED = 80
    _SLEW_SLOW_SPEED = 20
    _SLEW_SLOW_THRESHOLD = 5.0   # switch to slow within this many degrees
    _SLEW_TOLERANCE = 0.5        # stop when within this many degrees
    _SLEW_MAX_DURATION = 120     # hard timeout (seconds)
    _SLEW_POLL_INTERVAL = 1.0    # telemetry poll interval during slew

    def speed_move(self, speed: int, angle: int, dur_sec: int = 3) -> dict:
        """Raw motor move — no horizon check.

        Firmware angle convention (empirically confirmed, Phase 4):
          90=up  270=down  0=right  180=left
        API angle convention (after +180 firmware offset applied here):
          270=up  90=down  180=right  0=left

        Always switches to scenery mode first.  Tracking modes (sun/moon/star)
        fight speed_move — the tracking loop snaps the scope back after each
        nudge — so scenery mode is required for persistent manual movement.
        """
        if self._viewing_mode != "scenery":
            if self._viewing_mode is not None:
                logger.info(
                    f"Stopping {self._viewing_mode} tracking before manual slew"
                )
                try:
                    self.stop_view_mode()
                    time.sleep(0.5)
                except Exception as _e:
                    logger.warning(f"[speed_move] stop_view_mode failed (non-fatal): {_e}")
            logger.info("Starting scenery mode for manual slew")
            self.start_scenery_mode()
            time.sleep(1)
        # Firmware motor-angle frame is reversed by 180° from the logical
        # joystick frame used by API/UI. Convert here centrally so all callers
        # (nudge + manual_goto) behave consistently.
        fw_angle = (int(angle) + 180) % 360
        self._send_command(
            "scope_speed_move",
            params={"speed": int(speed), "angle": fw_angle, "dur_sec": int(dur_sec)},
            expect_response=False,
        )
        logger.info(
            f"Speed move: speed={speed} angle={int(angle)}° fw_angle={fw_angle}° dur={dur_sec}s"
        )
        return {"success": True}

    def speed_stop(self) -> bool:
        """Stop an in-progress speed/manual move (preserves current viewing mode)."""
        try:
            self._send_command(
                "scope_speed_move",
                params={"speed": 0, "angle": 0, "dur_sec": 0},
                expect_response=False,
            )
        except Exception:
            pass
        logger.info("Speed move stopped")
        return True

    def manual_goto(self, target_alt: float, target_az: float) -> dict:
        """
        Slew to target alt/az using motor speed control.

        Bypasses the firmware horizon limit. Uses fast speed for large
        distances, slow speed for fine approach, and enforces mechanical
        safety limits.

        Returns dict with status info.
        """
        import math
        import time as _time

        with self._cmd_seq_lock:
            return self._manual_goto_inner(target_alt, target_az)

    def _manual_goto_inner(self, target_alt: float, target_az: float) -> dict:
        import math
        import time as _time

        run_id = f"manual_goto_{int(_time.time() * 1000)}"

        # Clamp target to safe range
        if target_alt < self._SLEW_ALT_MIN or target_alt > self._SLEW_ALT_MAX:
            msg = f"Target alt {target_alt:.1f}° outside safe range [{self._SLEW_ALT_MIN}°, {self._SLEW_ALT_MAX}°]"
            logger.warning(f"[ManualGoTo] {msg}")
            return {"error": msg}

        start = _time.time()
        stall_count = 0
        prev_distance = None
        loop_count = 0

        logger.info(f"[ManualGoTo] Starting: target alt={target_alt:.1f}° az={target_az:.1f}°")
        # region agent log
        _debug_log(
            run_id,
            "H11",
            "src/seestar_client.py:_manual_goto_inner:start",
            "ManualGoTo started",
            {"target_alt": target_alt, "target_az": target_az, "viewing_mode": self._viewing_mode},
        )
        # endregion

        while _time.time() - start < self._SLEW_MAX_DURATION:
            loop_count += 1
            # Get current position
            telemetry = self.get_telemetry()
            cur_alt = telemetry.get("alt")
            cur_az = telemetry.get("az")

            if cur_alt is None or cur_az is None:
                logger.warning("[ManualGoTo] No alt/az in telemetry, retrying…")
                _time.sleep(self._SLEW_POLL_INTERVAL)
                continue

            # Compute deltas
            d_alt = target_alt - cur_alt
            # Shortest azimuth path (handle 360° wrap)
            d_az = (target_az - cur_az + 180) % 360 - 180

            distance = math.sqrt(d_alt ** 2 + d_az ** 2)
            logger.debug(
                f"[ManualGoTo] cur=({cur_alt:.1f}°, {cur_az:.1f}°) "
                f"delta=({d_alt:.1f}°, {d_az:.1f}°) dist={distance:.1f}°"
            )

            # Arrived?
            if distance < self._SLEW_TOLERANCE:
                self.speed_stop()
                elapsed = _time.time() - start
                logger.info(f"[ManualGoTo] Arrived in {elapsed:.1f}s")
                return {
                    "status": "arrived",
                    "alt": cur_alt,
                    "az": cur_az,
                    "elapsed": round(elapsed, 1),
                }

            # Safety: would the next move push us past mechanical limits?
            projected_alt = cur_alt + d_alt * 0.3  # rough estimate of next step
            if projected_alt < self._SLEW_ALT_MIN + 2:
                self.speed_stop()
                msg = f"Stopped: approaching lower alt limit ({self._SLEW_ALT_MIN}°)"
                logger.warning(f"[ManualGoTo] {msg}")
                return {"error": msg, "alt": cur_alt, "az": cur_az}
            if projected_alt > self._SLEW_ALT_MAX - 2:
                self.speed_stop()
                msg = f"Stopped: approaching upper alt limit ({self._SLEW_ALT_MAX}°)"
                logger.warning(f"[ManualGoTo] {msg}")
                return {"error": msg, "alt": cur_alt, "az": cur_az}

            # Stall detection — not making progress
            if prev_distance is not None:
                if abs(prev_distance - distance) < 0.05:
                    stall_count += 1
                    if stall_count >= 5:
                        self.speed_stop()
                        msg = "Stopped: no progress (possible hard stop)"
                        logger.warning(f"[ManualGoTo] {msg}")
                        return {"error": msg, "alt": cur_alt, "az": cur_az}
                else:
                    stall_count = 0
            prev_distance = distance

            # Choose speed
            speed = self._SLEW_SLOW_SPEED if distance < self._SLEW_SLOW_THRESHOLD else self._SLEW_FAST_SPEED

            # Compute angle for scope_speed_move.
            # Empirically confirmed firmware convention (Phase 4 diag):
            #   fw 90=up, 270=down, 0=right, 180=left
            # speed_move adds +180 to convert API→firmware, so API convention is:
            #   api 270=up, 90=down, 180=right, 0=left
            # Formula: negate d_alt so atan2 maps (up=270, right=180) correctly.
            angle = (math.degrees(math.atan2(d_az, -d_alt)) + 90) % 360
            if loop_count % 3 == 1:
                # region agent log
                _debug_log(
                    run_id,
                    "H11_H12",
                    "src/seestar_client.py:_manual_goto_inner:loop",
                    "ManualGoTo step",
                    {
                        "cur_alt": cur_alt,
                        "cur_az": cur_az,
                        "d_alt": d_alt,
                        "d_az": d_az,
                        "distance": distance,
                        "angle_cmd": angle,
                        "speed_cmd": speed,
                        "view_mode": self._viewing_mode,
                    },
                )
                # endregion

            # Move for a short burst then re-check
            dur = 2 if distance >= self._SLEW_SLOW_THRESHOLD else 1
            try:
                self.speed_move(speed, int(round(angle)), dur)
            except Exception as e:
                self.speed_stop()
                logger.error(f"[ManualGoTo] Move failed: {e}")
                return {"error": str(e), "alt": cur_alt, "az": cur_az}

            _time.sleep(dur + 0.2)  # wait for move to complete then poll

        # Timeout
        self.speed_stop()
        logger.warning("[ManualGoTo] Timed out")
        # region agent log
        _debug_log(
            run_id,
            "H11_H12",
            "src/seestar_client.py:_manual_goto_inner:timeout",
            "ManualGoTo timed out",
            {"elapsed_s": round(_time.time() - start, 1), "target_alt": target_alt, "target_az": target_az},
        )
        # endregion
        return {"error": "Timed out", "elapsed": round(_time.time() - start, 1)}

    def open_arm(self) -> bool:
        """Open (unfold) the telescope arm."""
        self._send_command("scope_open", expect_response=False)
        logger.info("Open arm command sent")
        return True

    def park(self) -> bool:
        """Park the telescope."""
        self._send_command("scope_park", expect_response=False)
        logger.info("Park command sent")
        return True

    def autofocus(self) -> dict:
        """Trigger autofocus. Note: firmware method is 'start_auto_focuse' (sic)."""
        result = self._send_command("start_auto_focuse", timeout_override=60)
        logger.info("Autofocus triggered")
        return result or {}

    def shutdown(self) -> bool:
        """Shutdown the Seestar (parks first, then powers off)."""
        self._send_command("pi_shutdown", expect_response=False)
        logger.info("Shutdown command sent")
        self._connected = False
        return True

    def move_step_focus(self, steps: int) -> dict:
        """Move focuser by the given number of steps (positive = out, negative = in)."""
        result = self._send_command(
            "move_focuser", params={"step": steps, "ret_step": True}
        )
        logger.info(f"Focus step: {steps}")
        return result or {}

    def set_gain(self, gain: int) -> dict:
        """Set camera gain (0–120, default 80)."""
        result = self._send_command("set_control_value", params=["gain", int(gain)])
        logger.info(f"Gain set to {gain}")
        return result or {}

    def set_manual_exp(self, enabled: bool) -> dict:
        """Enable or disable manual exposure mode."""
        result = self._send_command(
            "set_setting", params={"manual_exp": bool(enabled)}
        )
        logger.info(f"Manual exposure {'on' if enabled else 'off'}")
        return result or {}

    def set_exposure(
        self, stack_ms: Optional[int] = None, preview_ms: Optional[int] = None
    ) -> dict:
        """
        Set exposure times.

        Parameters
        ----------
        stack_ms : int, optional
            Stacking exposure in milliseconds (e.g. 10000).
        preview_ms : int, optional
            Live preview exposure in milliseconds (e.g. 500).
        """
        exp = {}
        if stack_ms is not None:
            exp["stack_l"] = int(stack_ms)
        if preview_ms is not None:
            exp["continuous"] = int(preview_ms)
        if not exp:
            return {}
        result = self._send_command("set_setting", params={"exp_ms": exp})
        logger.info(f"Exposure set: {exp}")
        return result or {}

    def set_lp_filter(self, enabled: bool) -> dict:
        """Enable or disable the light-pollution filter."""
        result = self._send_command(
            "set_setting", params={"stack_lenhance": bool(enabled)}
        )
        logger.info(f"LP filter {'on' if enabled else 'off'}")
        return result or {}

    def set_dew_heater(self, enabled: bool, power: int = 50) -> dict:
        """
        Control the dew heater.

        Parameters
        ----------
        enabled : bool
            Turn heater on or off.
        power : int
            Heater power 0–100 (only used when enabled=True).
        """
        result = self._send_command(
            "pi_output_set2",
            params={"heater": {"state": bool(enabled), "value": int(power)}},
        )
        self._heater_on = bool(enabled)
        logger.info(f"Dew heater {'on' if enabled else 'off'} power={power}")
        return result or {}

    def get_telemetry(self) -> dict:
        """
        Fetch live telescope telemetry.

        Merges results from scope_get_equ_coord, get_view_state, and
        get_device_state into a single flat dict.  Sub-calls that time out
        or fail are skipped rather than raising.
        """
        telemetry: dict = {}
        run_id = f"telemetry_{int(time.time() * 1000)}"

        # region agent log
        _debug_log(
            run_id,
            "H2_H3",
            "src/seestar_client.py:get_telemetry:start",
            "Telemetry entry state",
            {
                "client_viewing_mode": self._viewing_mode,
                "connected": self._connected,
            },
        )
        # endregion

        # Direct alt/az from firmware (no RA/Dec conversion needed).
        # scope_get_horiz_coord returns [alt_deg, az_deg] — faster and immune
        # to pi_set_time / LST errors.  Used as primary in scenery mode.
        try:
            h = self._send_command(
                "scope_get_horiz_coord", quiet=True, timeout_override=5
            )
            if isinstance(h, (list, tuple)) and len(h) >= 2:
                telemetry["horiz_alt"] = round(float(h[0]), 3)
                telemetry["horiz_az"] = round(float(h[1]), 3)
        except Exception:
            pass

        # RA / Dec
        try:
            r = self._send_command(
                "scope_get_equ_coord", quiet=True, timeout_override=5
            )
            if r:
                telemetry["ra"] = r.get("ra")
                telemetry["dec"] = r.get("dec")
                # region agent log
                _debug_log(
                    run_id,
                    "H4",
                    "src/seestar_client.py:get_telemetry:equ",
                    "scope_get_equ_coord response",
                    {
                        "ra": telemetry.get("ra"),
                        "dec": telemetry.get("dec"),
                    },
                )
                # endregion
        except Exception:
            pass

        # Compute Alt/Az.
        # Primary telemetry alt/az should represent actual mount pointing from
        # scope RA/Dec so values move during manual slews in any mode.
        # For sun/moon tracking we also compute target_alt/target_az for
        # diagnostics (where the ephemeris target is), but we do not overwrite
        # mount pointing alt/az with those values.
        lat, lon, _elev = get_observer_coordinates()
        # region agent log
        _debug_log(
            run_id,
            "H3",
            "src/seestar_client.py:get_telemetry:observer",
            "Observer coordinates used for telemetry",
            {"lat": lat, "lon": lon, "elev": _elev},
        )
        # endregion
        global _telemetry_warned_zero_observer
        if lat == 0.0 and lon == 0.0 and not _telemetry_warned_zero_observer:
            _telemetry_warned_zero_observer = True
            logger.warning(
                "[Telemetry] Observer at 0°,0° — set .env OBSERVER_* or load the "
                "map page (syncs lat/lon to server). Wrong azimuth until site is set."
            )

        # Scope RA/Dec → alt/az (always computed for diagnostics when available)
        if telemetry.get("ra") is not None and telemetry.get("dec") is not None:
            try:
                from src.constants import EARTH_TIMESCALE

                t = EARTH_TIMESCALE.now()
                scope_alt, scope_az = _altaz_from_equatorial_for_goto(
                    float(telemetry["ra"]),
                    float(telemetry["dec"]),
                    lat,
                    lon,
                    float(t.gast),
                )
                telemetry["scope_alt"] = round(scope_alt, 3)
                telemetry["scope_az"] = round(scope_az, 3)
                # region agent log
                _debug_log(
                    run_id,
                    "H4",
                    "src/seestar_client.py:get_telemetry:scope_altaz",
                    "Scope RA/Dec converted to Alt/Az",
                    {
                        "scope_alt": telemetry.get("scope_alt"),
                        "scope_az": telemetry.get("scope_az"),
                    },
                )
                # endregion
            except Exception as _altaz_err:
                logger.warning(f"[Telemetry] Scope Alt/Az computation failed: {_altaz_err}")

        # Optional target ephemeris in sun/moon mode (diagnostic only).
        if self._viewing_mode in ("sun", "moon"):
            try:
                sf_alt, sf_az = _skyfield_target_altaz(
                    self._viewing_mode, lat, lon, _elev
                )
                telemetry["target_alt"] = round(sf_alt, 3)
                telemetry["target_az"] = round(sf_az, 3)
                # region agent log
                _debug_log(
                    run_id,
                    "H1_H2",
                    "src/seestar_client.py:get_telemetry:branch_skyfield",
                    "Using Skyfield Alt/Az branch",
                    {
                        "client_viewing_mode": self._viewing_mode,
                        "skyfield_alt": telemetry.get("target_alt"),
                        "skyfield_az": telemetry.get("target_az"),
                        "scope_alt": telemetry.get("scope_alt"),
                        "scope_az": telemetry.get("scope_az"),
                    },
                )
                # endregion

                # Diagnostic: compare scope-derived vs Skyfield
                if telemetry.get("scope_az") is not None:
                    global _telemetry_warned_scope_offset
                    delta_az = ((telemetry["scope_az"] - sf_az + 180) % 360) - 180
                    delta_alt = telemetry["scope_alt"] - sf_alt
                    telemetry["scope_delta_az"] = round(delta_az, 1)
                    telemetry["scope_delta_alt"] = round(delta_alt, 1)
                    if abs(delta_az) > 90 and not _telemetry_warned_scope_offset:
                        _telemetry_warned_scope_offset = True
                        logger.warning(
                            f"[Telemetry] Scope az={telemetry['scope_az']:.1f}° vs "
                            f"Skyfield {self._viewing_mode} az={sf_az:.1f}° "
                            f"(delta={delta_az:+.1f}°) — possible compass/init error"
                        )
            except Exception as _sf_err:
                logger.warning(f"[Telemetry] Skyfield alt/az failed: {_sf_err}")

        # Primary alt/az for UI and servo loop.
        # Priority:
        #   1. sun/moon mode → Skyfield ephemeris (immune to stale RA/Dec)
        #   2. scenery mode  → scope_get_horiz_coord (direct firmware alt/az,
        #                       no RA/Dec conversion, updates on every move)
        #   3. fallback      → RA/Dec → alt/az conversion via scope_alt/scope_az
        if self._viewing_mode in ("sun", "moon") and telemetry.get("target_alt") is not None:
            telemetry["alt"] = telemetry["target_alt"]
            telemetry["az"] = telemetry["target_az"]
        elif telemetry.get("horiz_alt") is not None:
            # Scenery mode (or any mode): use direct firmware alt/az
            telemetry["alt"] = telemetry["horiz_alt"]
            telemetry["az"] = telemetry["horiz_az"]
        elif telemetry.get("scope_alt") is not None:
            telemetry["alt"] = telemetry["scope_alt"]
            telemetry["az"] = telemetry["scope_az"]
        elif telemetry.get("target_alt") is not None:
            telemetry["alt"] = telemetry["target_alt"]
            telemetry["az"] = telemetry["target_az"]

        # View state
        try:
            r = self._send_command("get_view_state", quiet=True, timeout_override=5)
            if r:
                view = r.get("View") or r
                telemetry["view_state"] = view
                # region agent log
                _debug_log(
                    run_id,
                    "H2",
                    "src/seestar_client.py:get_telemetry:view_state",
                    "View state fetched",
                    {
                        "view_mode_from_state": (
                            view.get("mode") if isinstance(view, dict) else None
                        ),
                        "client_viewing_mode": self._viewing_mode,
                    },
                )
                # endregion
        except Exception:
            pass

        # Device state — rich nested response, flatten into telemetry
        try:
            r = self._send_command(
                "get_device_state", quiet=True, timeout_override=5
            )
            if r:
                # pi_status: CPU temp, battery, charging, overtemp
                pi = r.get("pi_status") or {}
                telemetry["cpu_temp"] = pi.get("temp")
                telemetry["battery_capacity"] = pi.get("battery_capacity")
                telemetry["charger_status"] = pi.get("charger_status")
                telemetry["charge_online"] = pi.get("charge_online")
                telemetry["battery_temp"] = pi.get("battery_temp")
                telemetry["is_overtemp"] = pi.get("is_overtemp")
                telemetry["battery_overtemp"] = pi.get("battery_overtemp")

                # Focuser (absolute position from device state)
                foc = r.get("focuser") or {}
                telemetry["focuser_step"] = foc.get("step")
                telemetry["focuser_state"] = foc.get("state")
                telemetry["focuser_max_step"] = foc.get("max_step")
                # Also set focus_pos for the Manual Focus panel
                if foc.get("step") is not None:
                    telemetry["focus_pos"] = foc["step"]

                # Mount
                mt = r.get("mount") or {}
                telemetry["mount_tracking"] = mt.get("tracking")
                telemetry["mount_closed"] = mt.get("close")
                telemetry["mount_move_type"] = mt.get("move_type")

                # Storage
                sto = r.get("storage") or {}
                vols = sto.get("storage_volume") or []
                if vols:
                    telemetry["storage_free_mb"] = vols[0].get("free_mb")
                    telemetry["storage_used_pct"] = vols[0].get("used_percent")

                # WiFi / station
                sta = r.get("station") or {}
                telemetry["wifi_ssid"] = sta.get("ssid")
                telemetry["wifi_signal"] = sta.get("sig_lev")

                # Device info
                dev = r.get("device") or {}
                telemetry["firmware_ver"] = dev.get("firmware_ver_string")
                telemetry["serial_number"] = dev.get("sn")

                # Sensors
                bal = (r.get("balance_sensor") or {}).get("data") or {}
                telemetry["tilt_angle"] = bal.get("angle")
                comp = (r.get("compass_sensor") or {}).get("data") or {}
                telemetry["compass_direction"] = comp.get("direction")

                # Settings of interest
                sett = r.get("setting") or {}
                if hasattr(self, "_heater_on"):
                    telemetry["heater_enable"] = self._heater_on
                else:
                    telemetry["heater_enable"] = sett.get("heater_enable")
        except Exception:
            pass

        # Flatten view_state for easier JS access
        vs = telemetry.get("view_state")
        if isinstance(vs, dict):
            telemetry["view_mode"] = vs.get("mode")
            telemetry["view_target"] = vs.get("target_name")
            telemetry["view_stage"] = vs.get("stage")
            telemetry["lp_filter"] = vs.get("lp_filter")
            telemetry["manual_exp"] = vs.get("manual_exp")
            rtsp = vs.get("RTSP") or {}
            telemetry["rtsp_state"] = rtsp.get("state")
            af = vs.get("AutoFocus") or {}
            telemetry["autofocus_state"] = af.get("state")
            # Keep internal mode in sync with live view-state to avoid stale
            # status reporting (e.g. stuck on star while scope is in scenery).
            vm = telemetry.get("view_mode")
            if vm == "scenery":
                self._viewing_mode = "scenery"
            elif vm == "star":
                self._viewing_mode = "star"
            elif vm in ("solar_sys", "solar"):
                # Firmware uses "solar_sys" for solar tracking mode
                self._viewing_mode = "sun"
            elif vm == "lunar":
                self._viewing_mode = "moon"
            elif vm == "none":
                self._viewing_mode = None

        # region agent log
        _debug_log(
            run_id,
            "H1_H4_H5",
            "src/seestar_client.py:get_telemetry:return",
            "Final telemetry payload highlights",
            {
                "ra": telemetry.get("ra"),
                "dec": telemetry.get("dec"),
                "alt": telemetry.get("alt"),
                "az": telemetry.get("az"),
                "horiz_alt": telemetry.get("horiz_alt"),
                "horiz_az": telemetry.get("horiz_az"),
                "scope_alt": telemetry.get("scope_alt"),
                "scope_az": telemetry.get("scope_az"),
                "target_alt": telemetry.get("target_alt"),
                "target_az": telemetry.get("target_az"),
                "view_mode": telemetry.get("view_mode"),
            },
        )
        # endregion

        return telemetry

    def get_cached_telemetry(self) -> Dict[str, Any]:
        """Return telemetry cached by the heartbeat loop.

        Returns the last successful ``get_telemetry()`` result with an
        ``age_ms`` field indicating staleness.  Falls back to a live call
        if the cache is empty (first connect, before heartbeat has run).
        """
        if self._cached_telemetry:
            data = dict(self._cached_telemetry)
            data["age_ms"] = int((time.time() - self._cached_telemetry_ts) * 1000)
            return data
        # Cache not yet populated — do a live call (only happens once)
        return self.get_telemetry()

    def start_view_star(
        self,
        ra: float,
        dec: float,
        target_name: str = "",
        lp_filter: bool = False,
    ) -> dict:
        """Start viewing a star/DSO target at given RA/Dec."""
        self._send_command(
            "iscope_start_view",
            params={
                "mode": "star",
                "target_ra_dec": [ra, dec],
                "target_name": target_name,
                "lp_filter": lp_filter,
            },
            expect_response=False,
        )
        self._viewing_mode = "star"
        logger.info(f"Started star view: {target_name or f'RA={ra} Dec={dec}'}")
        return {"success": True}

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
            result = self._send_command(
                "pi_output_set_target", params=params, timeout_override=90
            )
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
        viewing_mode = self._viewing_mode
        try:
            t = self.get_telemetry()
            vm = t.get("view_mode")
            if vm == "scenery":
                viewing_mode = "scenery"
            elif vm == "star":
                viewing_mode = "star"
            elif vm == "none":
                viewing_mode = None
            elif vm == "solar_sys" and viewing_mode not in ("sun", "moon"):
                viewing_mode = "sun"
            self._viewing_mode = viewing_mode
        except Exception:
            pass

        status = {
            "connected": self._connected,
            "recording": self._recording,
            "viewing_mode": viewing_mode,
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
        self, flight_id: str, eta_seconds: float, transit_duration_estimate: float = 2.0
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

        Duplicate detection: if a recording for the same flight_id is already
        scheduled, this call is ignored to prevent timer accumulation.
        """
        try:
            # Duplicate detection: skip if already scheduled for this flight
            if flight_id in self._scheduled_recordings:
                existing_timer = self._scheduled_recordings[flight_id]
                if existing_timer.is_alive():
                    logger.debug(
                        f"Recording already scheduled for {flight_id}, skipping duplicate"
                    )
                    return True  # Already scheduled, return success
                else:
                    # Timer finished or cancelled, remove stale entry
                    del self._scheduled_recordings[flight_id]

            # Calculate timing
            start_delay = max(0, eta_seconds - self.pre_buffer)
            total_duration = (
                self.pre_buffer + transit_duration_estimate + self.post_buffer
            )

            logger.info(
                f"Scheduling transit {flight_id}: start in {start_delay:.1f}s, "
                f"record for {total_duration:.1f}s"
            )

            # Schedule start
            start_timer = threading.Timer(
                start_delay, self._start_recording, args=[flight_id, total_duration]
            )
            start_timer.daemon = True
            start_timer.start()

            self._scheduled_recordings[flight_id] = start_timer
            return True

        except Exception as e:
            logger.error(f"Failed to schedule transit recording: {e}")
            return False

    def cleanup_stale_timers(self):
        """Remove completed or cancelled timers from the scheduled recordings dict."""
        stale_keys = [
            fid
            for fid, timer in self._scheduled_recordings.items()
            if not timer.is_alive()
        ]
        for fid in stale_keys:
            del self._scheduled_recordings[fid]
        if stale_keys:
            logger.debug(f"Cleaned up {len(stale_keys)} stale timer(s)")

    def _start_recording(self, flight_id: str, duration: float):
        """Internal method to start recording and schedule stop."""
        try:
            # OpenSky last-mile refinement: get fresh position right before recording
            self._opensky_refine(flight_id)

            logger.info(f"Starting recording for transit {flight_id}")
            self.client.start_recording()

            # Schedule stop
            stop_timer = threading.Timer(
                duration, self._stop_recording, args=[flight_id]
            )
            stop_timer.daemon = True
            stop_timer.start()

        except Exception as e:
            logger.warning(f"Skipped recording for {flight_id}: {e}")

    def _opensky_refine(self, flight_id: str):
        """Query OpenSky for latest position before recording (last-mile refinement)."""
        try:
            import os

            from src.opensky import fetch_opensky_positions

            bbox = {
                "lamin": float(os.getenv("LAT_LOWER_LEFT", 0)),
                "lomin": float(os.getenv("LONG_LOWER_LEFT", 0)),
                "lamax": float(os.getenv("LAT_UPPER_RIGHT", 0)),
                "lomax": float(os.getenv("LONG_UPPER_RIGHT", 0)),
            }
            positions = fetch_opensky_positions(**bbox)
            if flight_id in positions:
                pos = positions[flight_id]
                logger.info(
                    f"[OpenSky Refinement] {flight_id}: "
                    f"({pos['lat']:.4f}, {pos['lon']:.4f}) alt={pos.get('altitude_m',0):.0f}m"
                )
            else:
                logger.debug(
                    f"[OpenSky Refinement] {flight_id}: not found in OpenSky data"
                )
        except Exception as e:
            # OpenSky refinement is best-effort, don't fail recording if it errors
            logger.debug(f"[OpenSky Refinement] Error for {flight_id}: {e}")

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

    host = os.getenv("SEESTAR_HOST", "")
    if not host:
        logger.info("SEESTAR_HOST not configured — auto-discovery will locate the scope at connect time")

    try:
        port = int(os.getenv("SEESTAR_PORT", str(SeestarClient.DEFAULT_PORT)))
        timeout = int(os.getenv("SEESTAR_TIMEOUT", str(SeestarClient.DEFAULT_TIMEOUT)))
        heartbeat = int(
            os.getenv(
                "SEESTAR_HEARTBEAT_INTERVAL",
                str(SeestarClient.DEFAULT_HEARTBEAT_INTERVAL),
            )
        )

        client = SeestarClient(
            host=host, port=port, timeout=timeout, heartbeat_interval=heartbeat
        )
        logger.info(
            f"Created Seestar client from environment: {host}:{port} (heartbeat: {heartbeat}s)"
        )
        return client

    except Exception as e:
        logger.error(f"Failed to create Seestar client from environment: {e}")
        return None
