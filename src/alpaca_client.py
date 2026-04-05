"""
ASCOM ALPACA HTTP client for Seestar S50 motor control.

Firmware 3.0+ locked out JSON-RPC motor commands for third-party apps.
The Seestar exposes a native ALPACA REST server on port 32323 that
handles slewing, GoTo, tracking, park/unpark, and position readout.

This module is strictly for motor control and telemetry.  All other
operations (viewing modes, recording, camera settings, focus, heartbeat)
remain on the JSON-RPC SeestarClient.

ALPACA API reference:
  https://ascom-standards.org/api/
"""

import json
import math
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from src import logger
from src.site_context import get_observer_coordinates


class AlpacaClient:
    """HTTP client for ASCOM ALPACA telescope control on Seestar S50."""

    DEFAULT_PORT = 32323
    DISCOVERY_PORT = 32227
    DISCOVERY_MSG = b"alpacadiscovery1"
    DEVICE_TYPE = "telescope"
    DEVICE_NUMBER = 0
    CLIENT_ID = 1
    DEFAULT_TIMEOUT = 5  # seconds per HTTP request

    def __init__(
        self,
        host: str = "",
        port: int = DEFAULT_PORT,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._connected = False
        self._txn_id = 0
        self._txn_lock = threading.Lock()
        self._capabilities: Dict[str, Any] = {}
        self._device_info: Dict[str, str] = {}
        # Polling thread for telemetry
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_running = False
        # Seestar ALPACA can stall if we hit it with many GETs too often (8+ per cycle).
        _env_pi = os.getenv("SEESTAR_ALPACA_POLL_INTERVAL")
        if _env_pi:
            try:
                self._poll_interval = max(2.0, min(120.0, float(_env_pi.strip())))
            except ValueError:
                self._poll_interval = 5.0
        else:
            self._poll_interval = 5.0
        self._poll_cycle_failures = 0
        self._last_position: Dict[str, float] = {}
        self._last_state: Dict[str, Any] = {}

    @staticmethod
    def _alpaca_bool(v: Any) -> bool:
        """Normalize ALPACA JSON Value to bool (some devices use 1/0 or strings)."""
        if v is True or v == 1:
            return True
        if v is False or v == 0 or v is None:
            return False
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "1", "yes"):
                return True
            if s in ("false", "0", "no", ""):
                return False
        return bool(v)

    # ── URL helpers ────────────────────────────────────────────────────

    @property
    def _base_url(self) -> str:
        return f"http://{self.host}:{self.port}/api/v1/{self.DEVICE_TYPE}/{self.DEVICE_NUMBER}"

    def _next_txn(self) -> int:
        with self._txn_lock:
            self._txn_id += 1
            return self._txn_id

    # ── Low-level HTTP ─────────────────────────────────────────────────

    def _get(
        self,
        endpoint: str,
        extra_params: Optional[Dict] = None,
        *,
        quiet: bool = False,
        timeout_override: Optional[float] = None,
    ) -> Dict:
        """GET an ALPACA property.  Returns the parsed JSON response."""
        txn = self._next_txn()
        params = {
            "ClientID": str(self.CLIENT_ID),
            "ClientTransactionID": str(txn),
        }
        if extra_params:
            params.update(extra_params)
        qs = urllib.parse.urlencode(params)
        url = f"{self._base_url}/{endpoint}?{qs}"
        logfn = logger.debug if quiet else logger.warning
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Zipcatcher/1.0 (ASCOM Alpaca client)"},
            )
            with urllib.request.urlopen(
                req,
                timeout=(
                    timeout_override if timeout_override is not None else self.timeout
                ),
            ) as resp:
                raw = resp.read().decode("utf-8-sig")
                data = json.loads(raw)
                err = data.get("ErrorNumber", 0)
                if err:
                    logfn(
                        f"ALPACA GET {endpoint}: error {err} — {data.get('ErrorMessage', '')}"
                    )
                return data
        except urllib.error.URLError as e:
            logfn(f"ALPACA GET {endpoint} failed: {e}")
            return {"error": str(e)}
        except Exception as e:
            logfn(f"ALPACA GET {endpoint} unexpected: {e}")
            return {"error": str(e)}

    def _put(
        self,
        endpoint: str,
        params: Optional[Dict] = None,
        timeout_override: Optional[float] = None,
        *,
        quiet: bool = False,
    ) -> Dict:
        """PUT an ALPACA command.  Returns the parsed JSON response."""
        txn = self._next_txn()
        form: Dict[str, str] = {
            "ClientID": str(self.CLIENT_ID),
            "ClientTransactionID": str(txn),
        }
        if params:
            form.update({k: str(v) for k, v in params.items()})
        body = urllib.parse.urlencode(form).encode()
        url = f"{self._base_url}/{endpoint}"
        req = urllib.request.Request(url, data=body, method="PUT")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("User-Agent", "Zipcatcher/1.0 (ASCOM Alpaca client)")
        logfn = logger.debug if quiet else logger.warning
        try:
            with urllib.request.urlopen(
                req,
                timeout=(
                    timeout_override if timeout_override is not None else self.timeout
                ),
            ) as resp:
                data = json.loads(resp.read().decode())
                err = data.get("ErrorNumber", 0)
                if err:
                    logfn(
                        f"ALPACA PUT {endpoint}: error {err} — {data.get('ErrorMessage', '')}"
                    )
                return data
        except urllib.error.URLError as e:
            logfn(f"ALPACA PUT {endpoint} failed: {e}")
            return {"error": str(e)}
        except Exception as e:
            logfn(f"ALPACA PUT {endpoint} unexpected: {e}")
            return {"error": str(e)}

    def _mgmt_get(self, path: str) -> Dict:
        """GET a management endpoint (outside /api/v1/telescope)."""
        url = f"http://{self.host}:{self.port}{path}"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Zipcatcher/1.0 (ASCOM Alpaca client)"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8-sig"))
        except Exception as e:
            return {"error": str(e)}

    # ── Discovery ──────────────────────────────────────────────────────

    def discover(self, timeout: float = 3.0) -> Optional[str]:
        """Broadcast ALPACA discovery on UDP 32227.  Returns IP or None."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(timeout)
            sock.bind(("", 0))
            sock.sendto(self.DISCOVERY_MSG, ("255.255.255.255", self.DISCOVERY_PORT))
            data, addr = sock.recvfrom(4096)
            sock.close()
            ip = addr[0]
            logger.info(f"ALPACA discovery: found device at {ip} — {data.decode()}")
            # Parse the reply for the AlpacaPort if present
            try:
                reply = json.loads(data.decode())
                port = reply.get("AlpacaPort", self.port)
                if port != self.port:
                    logger.info(f"ALPACA discovery: device reports port {port}")
                    self.port = int(port)
            except (json.JSONDecodeError, ValueError):
                pass
            return ip
        except socket.timeout:
            logger.debug("ALPACA discovery: no reply (timeout)")
            return None
        except Exception as e:
            logger.warning(f"ALPACA discovery error: {e}")
            return None

    # ── Connection ─────────────────────────────────────────────────────

    def _persist_host_to_env(self, new_host: str) -> None:
        """Persist discovered SEESTAR_HOST to .env for future launches."""
        env_path = os.getenv("FLYMOON_ENV_PATH", ".env")
        try:
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
            logger.info(f"ALPACA: persisted SEESTAR_HOST={new_host} to {env_path}")
        except OSError as e:
            logger.warning(f"ALPACA: failed to persist SEESTAR_HOST to .env: {e}")

    def connect(self) -> bool:
        """Connect to the ALPACA telescope server.

        If host is empty, runs discovery first.  If the configured host is
        unreachable (e.g. Seestar rebooted and got a new DHCP address),
        falls back to UDP discovery automatically.
        Returns True on success.
        """
        self.host = (self.host or "").strip()
        if not self.host:
            ip = self.discover()
            if ip:
                self.host = ip.strip()
                self._persist_host_to_env(self.host)
            else:
                logger.error("ALPACA connect: no host and discovery failed")
                return False

        # Quick TCP reachability check; if stale IP, try discovery
        if not self._tcp_reachable():
            stale = self.host
            logger.warning(
                f"ALPACA connect: {stale}:{self.port} unreachable — trying discovery"
            )
            ip = self.discover()
            if ip and ip.strip() != stale:
                self.host = ip.strip()
                logger.info(f"ALPACA discovery: new host {self.host} (was {stale})")
                self._persist_host_to_env(self.host)
            elif ip:
                self.host = ip.strip()
            if not self._tcp_reachable():
                logger.error(
                    f"ALPACA connect: {self.host}:{self.port} not reachable after discovery"
                )
                return False

        result = self._put("connected", {"Connected": "true"})
        if "error" in result:
            logger.error(f"ALPACA connect failed: {result['error']}")
            self._connected = False
            return False

        check = self._get("connected")
        if self._alpaca_bool(check.get("Value")):
            self._connected = True
            self._poll_cycle_failures = 0
            logger.info(f"ALPACA connected to {self.host}:{self.port}")
            self._load_capabilities()
            self._load_device_info()
            self._start_polling()
            return True
        else:
            logger.error(
                f"ALPACA connect: connected=true sent but Value={check.get('Value')}"
            )
            self._connected = False
            return False

    def disconnect(self) -> bool:
        """Disconnect from the ALPACA server."""
        self._stop_polling()
        if not self._connected:
            return True
        result = self._put("connected", {"Connected": "false"})
        self._connected = False
        if "error" in result:
            logger.warning(f"ALPACA disconnect error: {result['error']}")
        else:
            logger.info("ALPACA disconnected")
        return True

    def is_connected(self) -> bool:
        return self._connected

    def _tcp_reachable(self, timeout: float = 2.0) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            result = s.connect_ex((self.host, self.port))
            s.close()
            return result == 0
        except Exception:
            return False

    # ── Capabilities & device info ─────────────────────────────────────

    def _load_capabilities(self):
        # canmoveaxis requires an Axis parameter; query axis 0 (RA/Az)
        props_plain = [
            "canslew",
            "canslewasync",
            "canslewaltaz",
            "canslewaltazasync",
            "canpark",
            "canpulseguide",
            "cansettracking",
        ]
        for prop in props_plain:
            r = self._get(prop)
            self._capabilities[prop] = self._alpaca_bool(r.get("Value"))
        r = self._get("canmoveaxis", {"Axis": "0"})
        self._capabilities["canmoveaxis"] = self._alpaca_bool(r.get("Value"))
        logger.info(f"ALPACA capabilities: {self._capabilities}")

    def _load_device_info(self):
        for prop in [
            "name",
            "description",
            "driverinfo",
            "driverversion",
            "interfaceversion",
        ]:
            r = self._get(prop)
            self._device_info[prop] = str(r.get("Value", ""))
        # Also grab management description
        mgmt = self._mgmt_get("/management/v1/description")
        if "Value" in mgmt:
            self._device_info["server"] = str(mgmt["Value"])
        logger.info(
            f"ALPACA device: {self._device_info.get('name', '?')} — {self._device_info.get('driverinfo', '?')}"
        )

    def get_capabilities(self) -> Dict[str, Any]:
        return dict(self._capabilities)

    def get_device_info(self) -> Dict[str, str]:
        return dict(self._device_info)

    # ── Position readout ───────────────────────────────────────────────

    def get_position(self) -> Dict[str, float]:
        """Read current RA/Dec/Alt/Az from the scope.

        Returns dict with keys: ra, dec, alt, az, sidereal_time.
        All angles in degrees except ra in hours (ALPACA convention).
        """
        pos: Dict[str, float] = {}
        for key, endpoint in [
            ("ra", "rightascension"),
            ("dec", "declination"),
            ("alt", "altitude"),
            ("az", "azimuth"),
            ("sidereal_time", "siderealtime"),
        ]:
            r = self._get(endpoint)
            if "Value" in r:
                pos[key] = float(r["Value"])
        self._last_position = pos
        return pos

    def get_cached_position(self) -> Dict[str, float]:
        """Return the most recent polled position (no HTTP call)."""
        return dict(self._last_position)

    # ── Motor control: MoveAxis ────────────────────────────────────────

    def move_axis(
        self, axis: int, rate: float, timeout_sec: Optional[float] = 2.0
    ) -> Dict:
        """Start moving an axis at the given rate (degrees/sec).

        axis: 0 = RA/Az (primary), 1 = Dec/Alt (secondary)
        rate: positive or negative for direction, 0 to stop.
        """
        if not self._connected:
            return {"error": "not connected"}
        result = self._put(
            "moveaxis", {"Axis": axis, "Rate": rate}, timeout_override=timeout_sec
        )
        if "error" not in result:
            logger.debug(f"ALPACA moveaxis: axis={axis} rate={rate}")
        return result

    def get_max_move_rate(self, axis: int = 0) -> float:
        """Return the maximum supported MoveAxis rate (deg/s) for an axis.

        Falls back to 6.0°/s when the device does not expose axisrates.
        """
        if not self._connected:
            return 6.0
        cache_key = f"maxrate_axis_{int(axis)}"
        cached = self._capabilities.get(cache_key)
        if isinstance(cached, (int, float)) and cached > 0:
            return float(cached)

        max_rate = 6.0
        try:
            r = self._get("axisrates", {"Axis": str(int(axis))})
            ranges = r.get("Value")
            if isinstance(ranges, list):
                maxima = []
                for item in ranges:
                    if isinstance(item, dict):
                        v = item.get("Maximum")
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        v = item[1]
                    else:
                        v = None
                    if isinstance(v, (int, float)):
                        maxima.append(float(v))
                if maxima:
                    max_rate = max(maxima)
        except Exception as e:
            logger.debug(f"ALPACA axisrates lookup failed (axis={axis}): {e}")

        self._capabilities[cache_key] = max_rate
        # Keep a convenience global max as well.
        prev_global = self._capabilities.get("maxrate")
        if not isinstance(prev_global, (int, float)) or max_rate > float(prev_global):
            self._capabilities["maxrate"] = max_rate
        return max_rate

    def stop_axes(self, timeout_sec: Optional[float] = 2.0) -> Dict:
        """Stop motion on both axes."""
        r0 = self.move_axis(0, 0, timeout_sec=timeout_sec)
        r1 = self.move_axis(1, 0, timeout_sec=timeout_sec)
        logger.info("ALPACA: stopped both axes")
        errs = [r.get("error") for r in [r0, r1] if r.get("error")]
        if errs:
            return {"error": "; ".join(errs)}
        return {"success": True}

    # ── GoTo ───────────────────────────────────────────────────────────

    def goto_radec(
        self, ra_hours: float, dec_degrees: float, timeout_sec: Optional[float] = 3.0
    ) -> Dict:
        """Async slew to RA/Dec.  RA in hours [0,24), Dec in degrees."""
        if not self._connected:
            return {"error": "not connected"}
        result = self._put(
            "slewtocoordinatesasync",
            {
                "RightAscension": ra_hours,
                "Declination": dec_degrees,
            },
            timeout_override=timeout_sec,
        )
        if "error" not in result:
            logger.info(f"ALPACA GoTo: RA={ra_hours:.4f}h Dec={dec_degrees:.4f}°")
        return result

    def goto_altaz(
        self, alt: float, az: float, timeout_sec: Optional[float] = 3.0
    ) -> Dict:
        """Slew to Alt/Az by converting to RA/Dec first.

        The Seestar ALPACA server reports canslewaltaz=False,
        so we must convert using the observer's coordinates.
        """
        if not self._connected:
            return {"error": "not connected"}

        lat, lon, elev = get_observer_coordinates()
        ra_h, dec_d = self._altaz_to_radec(alt, az, lat, lon)
        logger.info(
            f"ALPACA AltAz ({alt:.2f}°, {az:.2f}°) → RA {ra_h:.4f}h Dec {dec_d:.4f}°"
        )
        return self.goto_radec(ra_h, dec_d, timeout_sec=timeout_sec)

    def is_slewing(self, timeout_sec: Optional[float] = 1.2) -> bool:
        """Check if the scope is currently slewing."""
        r = self._get("slewing", quiet=True, timeout_override=timeout_sec)
        if "Value" in r:
            return self._alpaca_bool(r.get("Value"))
        return bool(self._last_state.get("slewing", False))

    def abort_slew(self) -> Dict:
        """Abort any in-progress slew."""
        return self._put("abortslew", timeout_override=2.0)

    # ── Tracking ───────────────────────────────────────────────────────

    def set_tracking(self, enabled: bool, timeout_sec: Optional[float] = 2.0) -> Dict:
        """Enable or disable sidereal tracking."""
        if not self._connected:
            return {"error": "not connected"}
        result = self._put(
            "tracking",
            {"Tracking": str(enabled).lower()},
            timeout_override=timeout_sec,
            quiet=True,
        )
        err = result.get("ErrorNumber")
        msg = str(result.get("ErrorMessage", "")).lower()
        if err == 1279 and "below the horizon" in msg:
            logger.info("ALPACA tracking change skipped: scope below horizon")
            return {"ignored": True, "reason": "below_horizon", **result}
        if "error" not in result and not err:
            logger.info(f"ALPACA tracking: {'on' if enabled else 'off'}")
        return result

    def get_tracking(self, timeout_sec: Optional[float] = 1.2) -> bool:
        r = self._get("tracking", quiet=True, timeout_override=timeout_sec)
        if "Value" in r:
            return self._alpaca_bool(r.get("Value"))
        return bool(self._last_state.get("tracking", False))

    # ── Park / Unpark ──────────────────────────────────────────────────

    def park(self) -> Dict:
        """Park the mount (close arm)."""
        if not self._connected:
            return {"error": "not connected"}
        result = self._put("park")
        if "error" not in result:
            logger.info("ALPACA: park command sent")
        return result

    def unpark(self) -> Dict:
        """Unpark the mount (open arm)."""
        if not self._connected:
            return {"error": "not connected"}
        result = self._put("unpark")
        if "error" not in result:
            logger.info("ALPACA: unpark command sent")
        return result

    def is_parked(self) -> bool:
        r = self._get("atpark")
        return self._alpaca_bool(r.get("Value"))

    # ── Telemetry polling ──────────────────────────────────────────────

    def _start_polling(self):
        """Start background thread that polls position + state."""
        if self._poll_running:
            return
        self._poll_cycle_failures = 0
        self._poll_running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="alpaca-poll"
        )
        self._poll_thread.start()
        logger.debug("ALPACA telemetry polling started")

    def _stop_polling(self):
        self._poll_running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None

    def _poll_loop(self):
        while True:
            if not self._poll_running or not self._connected:
                break
            try:
                self._poll_once()
            except Exception as e:
                logger.debug(f"ALPACA poll error: {e}")
            if not self._poll_running or not self._connected:
                break
            time.sleep(self._poll_interval)

    @staticmethod
    def _get_network_error(r: Dict) -> bool:
        """True if _get failed before reaching the device (timeout, refused, etc.)."""
        return "error" in r and "Value" not in r

    def _abort_poll_after_failures(self, detail: str) -> None:
        """Stop hammering a dead or overloaded ALPACA server."""
        self._poll_cycle_failures += 1
        if self._poll_cycle_failures == 1:
            logger.warning(f"ALPACA telemetry: {detail}")
        if self._poll_cycle_failures >= 6:
            logger.warning(
                "ALPACA telemetry polling stopped after repeated failures — "
                "motor panel disabled until you disconnect and reconnect the telescope."
            )
            self._connected = False
            self._poll_running = False

    def _poll_once(self):
        """Single poll cycle: position + state flags."""
        pos: Dict[str, float] = {}
        for key, endpoint in [
            ("ra", "rightascension"),
            ("dec", "declination"),
            ("alt", "altitude"),
            ("az", "azimuth"),
        ]:
            r = self._get(endpoint, quiet=True)
            if self._get_network_error(r):
                self._abort_poll_after_failures(
                    f"{endpoint} unreachable ({r.get('error', '')})"
                )
                return
            if "Value" in r and r["Value"] is not None:
                try:
                    pos[key] = float(r["Value"])
                except (TypeError, ValueError):
                    pass
        self._last_position = pos

        state: Dict[str, Any] = {}
        for key, endpoint in [
            ("tracking", "tracking"),
            ("slewing", "slewing"),
            ("parked", "atpark"),
        ]:
            r = self._get(endpoint, quiet=True)
            if self._get_network_error(r):
                self._abort_poll_after_failures(
                    f"{endpoint} unreachable ({r.get('error', '')})"
                )
                return
            if "Value" in r and r["Value"] is not None:
                state[key] = self._alpaca_bool(r["Value"])
        r = self._get("siderealtime", quiet=True)
        if self._get_network_error(r):
            self._abort_poll_after_failures(
                f"siderealtime unreachable ({r.get('error', '')})"
            )
            return
        if "Value" in r and r["Value"] is not None:
            try:
                state["sidereal_time"] = float(r["Value"])
            except (TypeError, ValueError):
                pass
        self._last_state = state
        self._poll_cycle_failures = 0

    def get_cached_state(self) -> Dict[str, Any]:
        """Return most recent polled state (no HTTP call)."""
        return dict(self._last_state)

    def get_telemetry(self) -> Dict[str, Any]:
        """Combined position + state for UI display."""
        return {
            "connected": self._connected,
            "position": dict(self._last_position),
            "state": dict(self._last_state),
            "device_info": dict(self._device_info),
            "capabilities": dict(self._capabilities),
        }

    # ── Alt/Az → RA/Dec conversion ─────────────────────────────────────

    @staticmethod
    def _altaz_to_radec(
        alt: float, az: float, lat: float, lon: float
    ) -> Tuple[float, float]:
        """Convert Alt/Az to RA(hours)/Dec(degrees) for the current time.

        Uses the same spherical trig as SeestarClient.goto_altaz.
        """
        from src.constants import EARTH_TIMESCALE

        t = EARTH_TIMESCALE.now()
        lat_r = math.radians(lat)
        alt_r = math.radians(alt)
        az_r = math.radians(az)

        sin_dec = math.sin(alt_r) * math.sin(lat_r) + math.cos(alt_r) * math.cos(
            lat_r
        ) * math.cos(az_r)
        dec_r = math.asin(max(-1.0, min(1.0, sin_dec)))

        cos_ha = (math.sin(alt_r) - math.sin(lat_r) * sin_dec) / (
            math.cos(lat_r) * math.cos(dec_r) + 1e-12
        )
        ha_r = math.acos(max(-1.0, min(1.0, cos_ha)))
        if math.sin(az_r) > 0:
            ha_r = 2 * math.pi - ha_r

        lst = (t.gast + lon / 15.0) % 24
        ra_h = (lst - ha_r * 12 / math.pi) % 24
        dec_d = math.degrees(dec_r)
        return ra_h, dec_d

    # ── Status (for routes) ────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Concise status dict for /telescope/status merging."""
        return {
            "alpaca_connected": self._connected,
            "alpaca_host": self.host,
            "alpaca_port": self.port,
            "position": dict(self._last_position),
            "tracking": self._last_state.get("tracking"),
            "slewing": self._last_state.get("slewing"),
            "parked": self._last_state.get("parked"),
            "maxrate": self._capabilities.get("maxrate"),
        }

    def get_poll_interval(self) -> float:
        """Seconds between full ALPACA telemetry poll cycles."""
        return float(self._poll_interval)

    def set_poll_interval(self, seconds: float) -> float:
        """Clamp and apply poll spacing (reduces load on the Seestar HTTP server)."""
        s = max(2.0, min(120.0, float(seconds)))
        self._poll_interval = s
        return s


class MockAlpacaClient:
    """Drop-in mock for testing without hardware."""

    def __init__(self, host: str = "mock", port: int = 32323, timeout: int = 5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._connected = False
        self._tracking = False
        self._parked = True
        self._slewing = False
        self._ra = 12.0
        self._dec = 45.0
        self._alt = 60.0
        self._az = 180.0
        self._capabilities = {
            "canslew": True,
            "canslewasync": True,
            "canslewaltaz": False,
            "canslewaltazasync": False,
            "canmoveaxis": True,
            "canpark": True,
            "canpulseguide": False,
            "cansettracking": True,
        }
        self._device_info = {
            "name": "Mock Seestar S50",
            "description": "Simulated ALPACA device",
            "driverinfo": "MockAlpacaClient",
            "driverversion": "1.0",
            "interfaceversion": "3",
        }
        self._last_position: Dict[str, float] = {}
        self._last_state: Dict[str, Any] = {}
        _env_pi = os.getenv("SEESTAR_ALPACA_POLL_INTERVAL")
        if _env_pi:
            try:
                self._poll_interval = max(2.0, min(120.0, float(_env_pi.strip())))
            except ValueError:
                self._poll_interval = 5.0
        else:
            self._poll_interval = 5.0

    def discover(self, timeout: float = 3.0) -> Optional[str]:
        logger.debug("MockAlpaca: discover → mock")
        return "127.0.0.1"

    def connect(self) -> bool:
        self._connected = True
        self._update_cached()
        logger.info("MockAlpaca: connected")
        return True

    def disconnect(self) -> bool:
        self._connected = False
        logger.info("MockAlpaca: disconnected")
        return True

    def is_connected(self) -> bool:
        return self._connected

    def get_position(self) -> Dict[str, float]:
        pos = {"ra": self._ra, "dec": self._dec, "alt": self._alt, "az": self._az}
        self._last_position = pos
        return pos

    def get_cached_position(self) -> Dict[str, float]:
        return dict(self._last_position)

    def move_axis(self, axis: int, rate: float) -> Dict:
        logger.debug(f"MockAlpaca: moveaxis axis={axis} rate={rate}")
        return {"success": True}

    def get_max_move_rate(self, axis: int = 0) -> float:
        return 6.0

    def stop_axes(self) -> Dict:
        logger.debug("MockAlpaca: stop_axes")
        return {"success": True}

    def goto_radec(self, ra_hours: float, dec_degrees: float) -> Dict:
        self._ra = ra_hours
        self._dec = dec_degrees
        logger.info(f"MockAlpaca: GoTo RA={ra_hours:.4f}h Dec={dec_degrees:.4f}°")
        self._update_cached()
        return {"success": True}

    def goto_altaz(self, alt: float, az: float) -> Dict:
        self._alt = alt
        self._az = az
        logger.info(f"MockAlpaca: GoTo Alt={alt:.2f}° Az={az:.2f}°")
        self._update_cached()
        return {"success": True}

    def is_slewing(self) -> bool:
        return self._slewing

    def abort_slew(self) -> Dict:
        self._slewing = False
        return {"success": True}

    def set_tracking(self, enabled: bool) -> Dict:
        self._tracking = enabled
        logger.info(f"MockAlpaca: tracking={'on' if enabled else 'off'}")
        self._update_cached()
        return {"success": True}

    def get_tracking(self) -> bool:
        return self._tracking

    def park(self) -> Dict:
        self._parked = True
        logger.info("MockAlpaca: parked")
        self._update_cached()
        return {"success": True}

    def unpark(self) -> Dict:
        self._parked = False
        logger.info("MockAlpaca: unparked")
        self._update_cached()
        return {"success": True}

    def is_parked(self) -> bool:
        return self._parked

    def get_capabilities(self) -> Dict[str, Any]:
        return dict(self._capabilities)

    def get_device_info(self) -> Dict[str, str]:
        return dict(self._device_info)

    def get_cached_state(self) -> Dict[str, Any]:
        return dict(self._last_state)

    def get_telemetry(self) -> Dict[str, Any]:
        return {
            "connected": self._connected,
            "position": dict(self._last_position),
            "state": dict(self._last_state),
            "device_info": dict(self._device_info),
            "capabilities": dict(self._capabilities),
        }

    def get_status(self) -> Dict[str, Any]:
        return {
            "alpaca_connected": self._connected,
            "alpaca_host": self.host,
            "alpaca_port": self.port,
            "position": dict(self._last_position),
            "tracking": self._tracking,
            "slewing": self._slewing,
            "parked": self._parked,
            "maxrate": 6.0,
        }

    def get_poll_interval(self) -> float:
        return float(self._poll_interval)

    def set_poll_interval(self, seconds: float) -> float:
        s = max(2.0, min(120.0, float(seconds)))
        self._poll_interval = s
        return s

    def _update_cached(self):
        self._last_position = {
            "ra": self._ra,
            "dec": self._dec,
            "alt": self._alt,
            "az": self._az,
        }
        self._last_state = {
            "tracking": self._tracking,
            "slewing": self._slewing,
            "parked": self._parked,
        }


def create_alpaca_client_from_env():
    """Factory: create AlpacaClient or MockAlpacaClient from env vars.

    Env vars:
        ENABLE_SEESTAR       — must be 'true' (shared with JSON-RPC client)
        SEESTAR_HOST         — IP (shared; auto-discovery if empty)
        SEESTAR_ALPACA_PORT       — default 32323
        SEESTAR_ALPACA_POLL_INTERVAL — seconds between telemetry poll cycles (2–120, default 5)
        MOCK_TELESCOPE            — 'true' to use MockAlpacaClient
    """
    if os.getenv("ENABLE_SEESTAR", "false").lower() != "true":
        logger.info("ALPACA client disabled (ENABLE_SEESTAR=false)")
        return None

    if os.getenv("MOCK_TELESCOPE", "false").lower() == "true":
        logger.info("Using MockAlpacaClient")
        return MockAlpacaClient()

    host = os.getenv("SEESTAR_HOST", "")
    port = int(os.getenv("SEESTAR_ALPACA_PORT", str(AlpacaClient.DEFAULT_PORT)))
    timeout = int(
        os.getenv("SEESTAR_ALPACA_TIMEOUT", str(AlpacaClient.DEFAULT_TIMEOUT))
    )

    client = AlpacaClient(host=host, port=port, timeout=timeout)
    logger.info(f"Created ALPACA client: {host or '(auto-discover)'}:{port}")
    return client
