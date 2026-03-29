#!/usr/bin/env python3
"""
Phase 0 Connection Diagnostic — Seestar S50

Tests:
  1. TCP connect + init sequence (set_user_location, pi_set_time, pi_is_verified)
  2. scope_get_equ_coord raw response
  3. get_device_state (firmware version, device info)
  4. Listen for unsolicited Event messages for 30 seconds
  5. Disconnect
"""

import json
import logging
import os
import socket
import sys
import time
from datetime import datetime, timezone

# ── Bootstrap ────────────────────────────────────────────────────────────────
# Add project root to path so we can import src modules
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Load .env before any src imports
from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# ── Logging at DEBUG level ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("diag")

# ── Read config ──────────────────────────────────────────────────────────────
HOST = os.getenv("SEESTAR_HOST", "192.168.4.112")
PORT = int(os.getenv("SEESTAR_PORT", "4700"))
TIMEOUT = int(os.getenv("SEESTAR_TIMEOUT", "10"))

log.info(f"Config: host={HOST}  port={PORT}  timeout={TIMEOUT}s")

# ── Low-level helpers (no SeestarClient dependency for raw capture) ──────────
msg_id = 0


def next_id():
    global msg_id
    msg_id += 1
    return msg_id


def send_and_recv(sock, method, params=None, timeout=10):
    """Send one JSON-RPC message and return (response_dict | None, raw_lines[])."""
    mid = next_id()
    message = {"method": method, "id": mid, "verify": True}
    if params is not None:
        message["params"] = params
    payload = json.dumps(message) + "\r\n"
    log.info(f">>> SEND  {payload.rstrip()}")
    sock.sendall(payload.encode())

    raw_lines = []
    response = None
    buf = ""
    start = time.time()
    sock.settimeout(timeout)

    while time.time() - start < timeout:
        try:
            chunk = sock.recv(4096).decode()
        except socket.timeout:
            break
        if not chunk:
            log.warning("Socket closed by remote")
            break
        buf += chunk
        while "\r\n" in buf:
            line, buf = buf.split("\r\n", 1)
            if not line.strip():
                continue
            raw_lines.append(line)
            log.info(f"<<< RECV  {line}")
            try:
                parsed = json.loads(line)
                if parsed.get("id") == mid:
                    response = parsed
            except json.JSONDecodeError:
                log.warning(f"<<< UNPARSEABLE  {line[:200]}")
    return response, raw_lines


def listen_events(sock, duration=30):
    """Listen for unsolicited messages for `duration` seconds."""
    log.info(f"--- Listening for events ({duration}s) ---")
    buf = ""
    start = time.time()
    count = 0
    sock.settimeout(2)  # short recv timeout for polling
    while time.time() - start < duration:
        try:
            chunk = sock.recv(4096).decode()
        except socket.timeout:
            continue
        if not chunk:
            log.warning("Socket closed by remote during listen")
            break
        buf += chunk
        while "\r\n" in buf:
            line, buf = buf.split("\r\n", 1)
            if not line.strip():
                continue
            count += 1
            try:
                parsed = json.loads(line)
                event_name = parsed.get("Event", "(no Event field)")
                log.info(
                    f"<<< EVENT #{count}  {event_name}: {json.dumps(parsed, indent=2)}"
                )
            except json.JSONDecodeError:
                log.info(f"<<< RAW #{count}  {line[:300]}")
    log.info(f"--- Event listen done: {count} messages in {duration}s ---")


# ── Main diagnostic ─────────────────────────────────────────────────────────
def main():
    sock = None
    try:
        # 1. TCP connect
        log.info(f"Connecting to {HOST}:{PORT} ...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        sock.connect((HOST, PORT))
        log.info("TCP connected")

        # 2. Init sequence — set_user_location
        lat = float(os.getenv("OBSERVER_LATITUDE", "0"))
        lon = float(os.getenv("OBSERVER_LONGITUDE", "0"))
        log.info(f"=== set_user_location (lat={lat}, lon={lon}) ===")
        resp, _ = send_and_recv(
            sock, "set_user_location", params={"lat": lat, "lon": lon, "force": True}
        )
        log.info(f"set_user_location result: {resp}")

        # 3. Init sequence — pi_set_time
        now = datetime.now(timezone.utc)
        time_params = [
            {
                "year": now.year,
                "mon": now.month,
                "day": now.day,
                "hour": now.hour,
                "min": now.minute,
                "sec": now.second,
                "time_zone": "UTC",
            }
        ]
        log.info(f"=== pi_set_time ({now.strftime('%Y-%m-%dT%H:%M:%SZ')}) ===")
        resp, _ = send_and_recv(sock, "pi_set_time", params=time_params)
        log.info(f"pi_set_time result: {resp}")

        # 4. Init sequence — pi_is_verified
        log.info("=== pi_is_verified ===")
        resp, _ = send_and_recv(sock, "pi_is_verified")
        log.info(f"pi_is_verified result: {resp}")

        # 5. scope_get_equ_coord
        log.info("=== scope_get_equ_coord ===")
        resp, _ = send_and_recv(sock, "scope_get_equ_coord")
        if resp:
            result = resp.get("result", {})
            log.info(f"RA={result.get('ra')}  Dec={result.get('dec')}")
        else:
            log.warning("No response to scope_get_equ_coord")

        # 6. get_device_state (firmware, device name, etc.)
        log.info("=== get_device_state ===")
        resp, _ = send_and_recv(sock, "get_device_state")
        if resp:
            log.info(f"device_state full: {json.dumps(resp, indent=2)}")
            result = resp.get("result", resp)
            fw = result.get("firmware_ver") or result.get("firmware_version")
            if fw:
                log.info(f"Firmware version: {fw}")
        else:
            log.warning("No response to get_device_state")

        # 7. Listen for unsolicited events for 30 seconds
        listen_events(sock, duration=30)

    except Exception as e:
        log.error(f"DIAGNOSTIC FAILED: {type(e).__name__}: {e}", exc_info=True)

    finally:
        # 8. Disconnect
        if sock:
            try:
                sock.close()
                log.info("Socket closed")
            except Exception:
                pass

    log.info("=== Diagnostic complete ===")


if __name__ == "__main__":
    main()
