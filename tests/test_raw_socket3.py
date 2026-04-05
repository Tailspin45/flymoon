#!/usr/bin/env python3
"""Raw socket test v3 — wait for events before sending motor commands.
Reboot the scope before running this test."""
import json
import select
import socket
import time

HOST = "192.168.4.139"
TCP_PORT = 4700
UDP_PORT = 4720


def send(sock, method, params=None, msg_id=1):
    cmd = {"method": method, "id": msg_id}
    if params is not None:
        cmd["params"] = params
    raw = json.dumps(cmd) + "\r\n"
    sock.sendall(raw.encode())
    print(f"  >> {raw.strip()}")


def drain_until_event(sock, timeout=60.0):
    """Read until we get an Event message or timeout."""
    print(f"  Waiting up to {timeout}s for scope to start sending events...")
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline:
        readable, _, _ = select.select([sock], [], [], 0.5)
        if readable:
            chunk = sock.recv(4096)
            if not chunk:
                print("  Socket closed!")
                return False
            buf += chunk
            text = buf.decode("utf-8", errors="replace")
            while "\r\n" in text:
                line, text = text.split("\r\n", 1)
                buf = text.encode()
                if line.strip():
                    print(f"  << {line.strip()}")
                    if '"Event"' in line:
                        return True
        time.time() + timeout - deadline + timeout
    print(f"  No events after {timeout}s")
    return False


def read_all(sock, duration=5.0):
    """Read everything for duration seconds."""
    deadline = time.time() + duration
    while time.time() < deadline:
        readable, _, _ = select.select([sock], [], [], 0.25)
        if readable:
            chunk = sock.recv(4096)
            if chunk:
                for line in (
                    chunk.decode("utf-8", errors="replace").strip().split("\r\n")
                ):
                    if line.strip():
                        print(f"  << {line.strip()}")


# 1. UDP handshake
print("[1] UDP broadcast...")
usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
usock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
usock.settimeout(2.0)
usock.bind(("", 0))
usock.sendto(
    (json.dumps({"id": 1, "method": "scan_iscope", "params": ""}) + "\r\n").encode(),
    ("255.255.255.255", UDP_PORT),
)
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

# 3. Send init commands (fire-and-forget)
print("\n[3] Sending init commands...")
send(
    sock, "set_user_location", {"lat": 33.111369, "lon": -117.310169, "force": True}, 1
)
t = time.gmtime()
send(
    sock,
    "pi_set_time",
    [
        {
            "year": t.tm_year,
            "mon": t.tm_mon,
            "day": t.tm_mday,
            "hour": t.tm_hour,
            "min": t.tm_min,
            "sec": t.tm_sec,
            "time_zone": "UTC",
        },
        "verify",
    ],
    2,
)
send(sock, "pi_is_verified", msg_id=3)
send(sock, "set_setting", {"master_cli": True}, 4)
send(sock, "set_setting", {"cli_name": "Zipcatcher/test"}, 5)

# 4. Wait for events to start
print("\n[4] Waiting for scope to start sending events...")
got_events = drain_until_event(sock, timeout=45.0)

if not got_events:
    print("\n  Scope never sent events. Exiting.")
    sock.close()
    exit(1)

# 5. Re-claim master now that events are flowing
print("\n[5] Re-claiming master after events started...")
send(sock, "set_setting", {"master_cli": True}, 10)
time.sleep(1.0)
read_all(sock, 2.0)

# 6. Try scope_speed_move
print("\n[6] scope_speed_move (speed=4000, angle=270, dur=5)...")
print("  >>> WATCH THE SCOPE — does it move? <<<")
send(sock, "scope_speed_move", {"speed": 4000, "angle": 270, "dur_sec": 5}, 20)
read_all(sock, 8.0)

# 7. Try a different angle
print("\n[7] scope_speed_move (speed=4000, angle=90, dur=5)...")
print("  >>> WATCH THE SCOPE — does it move? <<<")
send(sock, "scope_speed_move", {"speed": 4000, "angle": 90, "dur_sec": 5}, 21)
read_all(sock, 8.0)

# 8. Try with iscope_start_view sun first, then move
print("\n[8] Starting sun mode, then trying move...")
send(sock, "iscope_start_view", {"mode": "sun"}, 30)
time.sleep(3)
read_all(sock, 2.0)
print("  Now trying scope_speed_move in sun mode...")
send(sock, "scope_speed_move", {"speed": 4000, "angle": 0, "dur_sec": 5}, 31)
read_all(sock, 8.0)

sock.close()
print("\nDone. Did the scope move at any point?")
