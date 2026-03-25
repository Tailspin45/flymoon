#!/usr/bin/env python3
"""Raw socket test v4 — test GoTo via iscope_start_view mode=star.
This is a completely different motor code path than scope_speed_move.
Also tests scope_goto (another firmware command some versions support)."""
import json
import math
import select
import socket
import time

HOST = "192.168.4.139"
TCP_PORT = 4700
UDP_PORT = 4720

# Observer location
OBS_LAT = 33.111369
OBS_LON = -117.310169

def send(sock, method, params=None, msg_id=1):
    cmd = {"method": method, "id": msg_id}
    if params is not None:
        cmd["params"] = params
    raw = json.dumps(cmd) + "\r\n"
    sock.sendall(raw.encode())
    print(f"  >> {raw.strip()}")

def read_all(sock, duration=5.0):
    deadline = time.time() + duration
    while time.time() < deadline:
        readable, _, _ = select.select([sock], [], [], 0.25)
        if readable:
            chunk = sock.recv(4096)
            if chunk:
                for line in chunk.decode("utf-8", errors="replace").strip().split("\r\n"):
                    if line.strip():
                        print(f"  << {line.strip()}")

def wait_for_events(sock, timeout=45.0):
    print(f"  Waiting up to {timeout}s for events...")
    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        readable, _, _ = select.select([sock], [], [], 0.5)
        if readable:
            chunk = sock.recv(4096)
            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                print(f"  << {text.strip()[:200]}")
                if '"Event"' in text:
                    return True
    return False

# 1. UDP
print("[1] UDP broadcast...")
usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
usock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
usock.settimeout(2.0)
usock.bind(("", 0))
usock.sendto((json.dumps({"id": 1, "method": "scan_iscope", "params": ""}) + "\r\n").encode(),
             ("255.255.255.255", UDP_PORT))
try:
    data, addr = usock.recvfrom(4096)
    resp = json.loads(data.decode())
    clients = resp.get("result", {}).get("tcp_client_num", "?")
    print(f"  Scope replied. tcp_client_num={clients}")
except socket.timeout:
    print("  No UDP reply")
usock.close()
time.sleep(0.3)

# 2. TCP connect
print("\n[2] TCP connect...")
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(10.0)
sock.connect((HOST, TCP_PORT))
sock.setblocking(False)
print("  Connected!")

# 3. Init
print("\n[3] Init...")
send(sock, "set_user_location", {"lat": OBS_LAT, "lon": OBS_LON, "force": True}, 1)
t = time.gmtime()
send(sock, "pi_set_time", [{"year": t.tm_year, "mon": t.tm_mon, "day": t.tm_mday,
     "hour": t.tm_hour, "min": t.tm_min, "sec": t.tm_sec, "time_zone": "UTC"}, "verify"], 2)
send(sock, "pi_is_verified", msg_id=3)
send(sock, "set_setting", {"master_cli": True}, 4)

# 4. Wait for events
print("\n[4] Waiting for events...")
if not wait_for_events(sock, 45):
    print("  No events. Continuing anyway...")

# 5. Test GoTo via iscope_start_view mode=star
# Pick a target well away from sun — Polaris (RA ~2.53h, Dec ~89.26°)
print("\n[5] GoTo via iscope_start_view mode=star (Polaris)...")
print("  >>> WATCH THE SCOPE — does it slew? <<<")
send(sock, "iscope_start_view", {
    "mode": "star",
    "target_ra_dec": [2.53, 89.26],
    "target_name": "Polaris",
    "lp_filter": False,
}, 50)
read_all(sock, 10.0)

# 6. Now try GoTo to a completely different target — Sirius
print("\n[6] GoTo via iscope_start_view mode=star (Sirius)...")
print("  >>> WATCH THE SCOPE — does it slew? <<<")
send(sock, "iscope_start_view", {
    "mode": "star",
    "target_ra_dec": [6.75, -16.72],
    "target_name": "Sirius",
    "lp_filter": False,
}, 51)
read_all(sock, 10.0)

# 7. Try scope_goto (some firmware versions have this)
print("\n[7] Trying scope_goto (alt=45, az=180)...")
send(sock, "scope_goto", {"alt": 45.0, "az": 180.0}, 60)
read_all(sock, 10.0)

# 8. Try iscope_start_view mode=sun (should track the sun)
print("\n[8] iscope_start_view mode=sun...")
print("  >>> Does it slew to the sun? <<<")
send(sock, "iscope_start_view", {"mode": "sun"}, 70)
read_all(sock, 10.0)

sock.close()
print("\nDone. Did the scope move at ANY point (steps 5-8)?")
