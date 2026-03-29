#!/usr/bin/env python3
"""Discover and probe the Seestar S50's native ALPACA server."""
import json
import socket
import urllib.request

SCOPE_IP = "192.168.4.139"
ALPACA_PORT = 32323


def get(path, timeout=3):
    url = f"http://{SCOPE_IP}:{ALPACA_PORT}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data
    except Exception as e:
        return {"error": str(e)}


def put(path, params=None, timeout=5):
    url = f"http://{SCOPE_IP}:{ALPACA_PORT}{path}"
    body = ""
    if params:
        body = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, data=body.encode(), method="PUT")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


# 1. UDP Discovery
print("[1] ALPACA UDP Discovery (port 32227)...")
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(5.0)
    sock.bind(("", 0))
    sock.sendto(b"alpacadiscovery1", ("255.255.255.255", 32227))
    data, addr = sock.recvfrom(4096)
    print(f"  Reply from {addr}: {data.decode()}")
    sock.close()
except socket.timeout:
    print("  No reply (timeout)")
except Exception as e:
    print(f"  Error: {e}")

# 2. TCP port check
print(f"\n[2] TCP port {ALPACA_PORT} check...")
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    result = s.connect_ex((SCOPE_IP, ALPACA_PORT))
    print(
        f"  Port {ALPACA_PORT}: {'OPEN' if result == 0 else f'CLOSED (code {result})'}"
    )
    s.close()
except Exception as e:
    print(f"  Error: {e}")

# 3. Management endpoints
print("\n[3] Management: description...")
print(f"  {json.dumps(get('/management/v1/description'), indent=2)[:500]}")

print("\n[4] Management: configured devices...")
devices = get("/management/v1/configureddevices")
print(f"  {json.dumps(devices, indent=2)[:800]}")

print("\n[5] Management: API versions...")
print(f"  {json.dumps(get('/management/apiversions'), indent=2)[:300]}")

# 4. Telescope capabilities
print("\n[6] Telescope capabilities...")
for prop in [
    "canslew",
    "canslewasync",
    "canslewaltaz",
    "canslewaltazasync",
    "canmoveaxis",
    "canpark",
    "canpulseguide",
    "cansettracking",
    "tracking",
    "atpark",
    "slewing",
    "connected",
]:
    result = get(f"/api/v1/telescope/0/{prop}")
    val = result.get("Value", result.get("error", "?"))
    print(f"  {prop}: {val}")

# 5. Current position
print("\n[7] Current position...")
for prop in [
    "rightascension",
    "declination",
    "altitude",
    "azimuth",
    "siderealtime",
    "utcdate",
]:
    result = get(f"/api/v1/telescope/0/{prop}")
    val = result.get("Value", result.get("error", "?"))
    print(f"  {prop}: {val}")

# 6. Telescope name/description
print("\n[8] Telescope info...")
for prop in ["name", "description", "driverinfo", "driverversion", "interfaceversion"]:
    result = get(f"/api/v1/telescope/0/{prop}")
    val = result.get("Value", result.get("error", "?"))
    print(f"  {prop}: {val}")

print("\nDone.")
