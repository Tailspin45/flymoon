#!/usr/bin/env python3
"""Raw socket test v2 — try ALP's exact startup sequence."""
import json
import select
import socket
import time

HOST = "192.168.4.139"
TCP_PORT = 4700
UDP_PORT = 4720

def send_and_wait(sock, method, params=None, msg_id=1, wait=3.0):
    """Send a command and wait for any response."""
    cmd = {"method": method, "id": msg_id}
    if params is not None:
        cmd["params"] = params
    raw = json.dumps(cmd) + "\r\n"
    sock.sendall(raw.encode())
    print(f"  >> {raw.strip()}")

    deadline = time.time() + wait
    while time.time() < deadline:
        readable, _, _ = select.select([sock], [], [], 0.25)
        if readable:
            chunk = sock.recv(4096)
            if chunk:
                print(f"  << {len(chunk)} bytes: {chunk[:500]}")
                return chunk
    print(f"  (no response in {wait}s)")
    return None

def drain(sock, duration=2.0):
    """Drain any pending data."""
    deadline = time.time() + duration
    got_any = False
    while time.time() < deadline:
        readable, _, _ = select.select([sock], [], [], 0.25)
        if readable:
            chunk = sock.recv(4096)
            if chunk:
                print(f"  << {len(chunk)} bytes: {chunk[:500]}")
                got_any = True
    if not got_any:
        print(f"  (no data in {duration}s)")

# 1. UDP handshake (broadcast)
print("[1] UDP broadcast...")
usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
usock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
usock.settimeout(2.0)
usock.bind(("", 0))
usock.sendto((json.dumps({"id": 1, "method": "scan_iscope", "params": ""}) + "\r\n").encode(),
             ("255.255.255.255", UDP_PORT))
try:
    data, addr = usock.recvfrom(4096)
    print(f"  Reply from {addr[0]}: {data[:200]}")
except socket.timeout:
    print("  No UDP reply")
usock.close()
time.sleep(0.5)

# 2. TCP connect
print("\n[2] TCP connect...")
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(10.0)
sock.connect((HOST, TCP_PORT))
sock.setblocking(False)
print("  Connected!")

# 3. Wait briefly for unsolicited data
print("\n[3] Waiting for initial data (3s)...")
drain(sock, 3.0)

# 4. ALP sequence: get_device_state first
print("\n[4] get_device_state...")
send_and_wait(sock, "get_device_state", params={}, msg_id=100, wait=5.0)

# 5. set_user_location
print("\n[5] set_user_location...")
send_and_wait(sock, "set_user_location",
              params={"lat": 33.111369, "lon": -117.310169, "force": True},
              msg_id=101, wait=3.0)

# 6. pi_set_time
print("\n[6] pi_set_time...")
t = time.gmtime()
send_and_wait(sock, "pi_set_time",
              params=[{"year": t.tm_year, "mon": t.tm_mon, "day": t.tm_mday,
                       "hour": t.tm_hour, "min": t.tm_min, "sec": t.tm_sec,
                       "time_zone": "UTC"}, "verify"],
              msg_id=102, wait=3.0)

# 7. pi_is_verified
print("\n[7] pi_is_verified...")
send_and_wait(sock, "pi_is_verified", msg_id=103, wait=3.0)

# 8. set_setting master_cli
print("\n[8] set_setting master_cli=true...")
send_and_wait(sock, "set_setting", params={"master_cli": True}, msg_id=104, wait=3.0)

# 9. Drain — see if PiStatus events start flowing
print("\n[9] Draining for events (10s)...")
drain(sock, 10.0)

# 10. scope_speed_move
print("\n[10] scope_speed_move (speed=4000, angle=270, dur=5)...")
send_and_wait(sock, "scope_speed_move",
              params={"speed": 4000, "angle": 270, "dur_sec": 5},
              msg_id=200, wait=5.0)

# 11. Final drain
print("\n[11] Final drain (5s)...")
drain(sock, 5.0)

sock.close()
print("\nDone.")
