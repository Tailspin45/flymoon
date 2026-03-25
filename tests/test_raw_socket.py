#!/usr/bin/env python3
"""Raw socket test — bypasses SeestarClient to prove data flows."""
import json
import select
import socket
import time

HOST = "192.168.4.139"
# NOTE: Force-quit the Seestar iPhone app before running this test!
# The UDP reply shows tcp_client_num — if > 0, another client is connected
# and the scope may only send data to the first client.
TCP_PORT = 4700
UDP_PORT = 4720

# 1. UDP handshake
print(f"[1] UDP broadcast scan_iscope to 255.255.255.255:{UDP_PORT}...")
usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
usock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
usock.settimeout(2.0)
usock.bind(("", 0))
msg = json.dumps({"id": 1, "method": "scan_iscope", "params": ""}) + "\r\n"
usock.sendto(msg.encode(), ("255.255.255.255", UDP_PORT))
try:
    data, addr = usock.recvfrom(4096)
    print(f"    UDP reply from {addr}: {data[:300]}")
except socket.timeout:
    print("    No UDP reply (timeout)")
usock.close()

# 2. TCP connect
print(f"\n[2] TCP connect to {HOST}:{TCP_PORT}...")
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(5.0)
sock.connect((HOST, TCP_PORT))
print("    Connected!")

# 3. Wait for any data (5 seconds)
print("\n[3] Waiting for scope to send data (5s)...")
for i in range(10):
    readable, _, _ = select.select([sock], [], [], 0.5)
    if readable:
        chunk = sock.recv(4096)
        print(f"    << {len(chunk)} bytes: {chunk[:300]}")
    else:
        print(f"    ... {(i+1)*0.5:.1f}s no data")

# 4. Send pi_is_verified and wait
print("\n[4] Sending pi_is_verified...")
cmd = json.dumps({"method": "pi_is_verified", "id": 10, "verify": True}) + "\r\n"
sock.sendall(cmd.encode())
print(f"    >> {cmd.strip()}")

print("    Waiting for response (5s)...")
for i in range(10):
    readable, _, _ = select.select([sock], [], [], 0.5)
    if readable:
        chunk = sock.recv(4096)
        print(f"    << {len(chunk)} bytes: {chunk[:500]}")
    else:
        print(f"    ... {(i+1)*0.5:.1f}s no data")

# 5. Send scope_speed_move and wait
print("\n[5] Sending scope_speed_move (speed=4000, angle=270, dur=3)...")
cmd = json.dumps({"method": "scope_speed_move", "id": 20, "params": {"speed": 4000, "angle": 270, "dur_sec": 3}}) + "\r\n"
sock.sendall(cmd.encode())
print(f"    >> {cmd.strip()}")

print("    Waiting for response (5s)...")
for i in range(10):
    readable, _, _ = select.select([sock], [], [], 0.5)
    if readable:
        chunk = sock.recv(4096)
        print(f"    << {len(chunk)} bytes: {chunk[:500]}")
    else:
        print(f"    ... {(i+1)*0.5:.1f}s no data")

# 6. Send set_setting master_cli and wait
print("\n[6] Sending set_setting master_cli=true...")
cmd = json.dumps({"method": "set_setting", "id": 30, "params": {"master_cli": True}}) + "\r\n"
sock.sendall(cmd.encode())
print(f"    >> {cmd.strip()}")

print("    Waiting for response (5s)...")
for i in range(10):
    readable, _, _ = select.select([sock], [], [], 0.5)
    if readable:
        chunk = sock.recv(4096)
        print(f"    << {len(chunk)} bytes: {chunk[:500]}")
    else:
        print(f"    ... {(i+1)*0.5:.1f}s no data")

sock.close()
print("\nDone.")
