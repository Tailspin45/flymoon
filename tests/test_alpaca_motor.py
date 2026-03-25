#!/usr/bin/env python3
"""Test ALPACA motor control on the Seestar S50.
Connects, syncs UTC, reads position, tries moveaxis and GoTo."""
import json
import socket
import time
import urllib.request
from datetime import datetime, timezone

SCOPE_IP = "192.168.4.139"
PORT = 32323
BASE = f"http://{SCOPE_IP}:{PORT}/api/v1/telescope/0"
TXN = 0

def get(path):
    global TXN
    TXN += 1
    url = f"{BASE}/{path}?ClientID=1&ClientTransactionID={TXN}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def put(path, params=None):
    global TXN
    TXN += 1
    url = f"{BASE}/{path}"
    p = {"ClientID": "1", "ClientTransactionID": str(TXN)}
    if params:
        p.update(params)
    body = "&".join(f"{k}={v}" for k, v in p.items())
    req = urllib.request.Request(url, data=body.encode(), method="PUT")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def pos():
    ra = get("rightascension").get("Value", "?")
    dec = get("declination").get("Value", "?")
    alt = get("altitude").get("Value", "?")
    az = get("azimuth").get("Value", "?")
    return f"RA={ra} Dec={dec} Alt={alt} Az={az}"

# 1. Connect
print("[1] Connecting...")
result = put("connected", {"Connected": "true"})
print(f"  {result}")
connected = get("connected").get("Value")
print(f"  connected={connected}")
if not connected:
    print("  FAILED to connect. Exiting.")
    exit(1)

# 1b. Unpark (open arm)
print("\n[1b] Unparking (open arm)...")
result = put("unpark")
print(f"  unpark: {result}")
atpark = get("atpark").get("Value")
print(f"  atpark={atpark}")
time.sleep(3)  # give arm time to open

# 2. Sync UTC
print("\n[2] Syncing UTC time...")
utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
result = put("utcdate", {"UTCDate": utc_now})
print(f"  Set UTC to {utc_now}: {result}")

# 3. Read position
print(f"\n[3] Current position: {pos()}")

# 4. Check moveaxis capability
print("\n[4] Checking moveaxis...")
for axis in [0, 1]:
    r = get(f"canmoveaxis?Axis={axis}")
    print(f"  canmoveaxis(Axis={axis}): {r.get('Value', r)}")

# 5. Check axis rates
print("\n[5] Axis rates...")
for axis in [0, 1]:
    r = get(f"axisrates?Axis={axis}")
    print(f"  axisrates(Axis={axis}): {r.get('Value', r)}")

# 6. Try moveaxis (small nudge on axis 0)
print("\n[6] MoveAxis test — Axis=0 (RA/Az), Rate=1.0 deg/s for 2s...")
print(f"  Before: {pos()}")
print("  >>> WATCH THE SCOPE <<<")
result = put("moveaxis", {"Axis": "0", "Rate": "1.0"})
print(f"  moveaxis result: {result}")
time.sleep(2)
# Stop
result = put("moveaxis", {"Axis": "0", "Rate": "0"})
print(f"  stop result: {result}")
print(f"  After:  {pos()}")

# 7. Try moveaxis on axis 1 (Dec/Alt)
print("\n[7] MoveAxis test — Axis=1 (Dec/Alt), Rate=1.0 deg/s for 2s...")
print(f"  Before: {pos()}")
print("  >>> WATCH THE SCOPE <<<")
result = put("moveaxis", {"Axis": "1", "Rate": "1.0"})
print(f"  moveaxis result: {result}")
time.sleep(2)
result = put("moveaxis", {"Axis": "1", "Rate": "0"})
print(f"  stop result: {result}")
print(f"  After:  {pos()}")

# 8. Try tracking
print("\n[8] Enable tracking...")
result = put("tracking", {"Tracking": "true"})
print(f"  tracking result: {result}")
tracking = get("tracking").get("Value")
print(f"  tracking={tracking}")

# 9. Read final position
time.sleep(2)
print(f"\n[9] Final position: {pos()}")

# 10. Try a small GoTo (slew to current pos + 0.1h RA)
print("\n[10] GoTo test — slew RA+0.1h from current...")
cur_ra = get("rightascension").get("Value", 0)
cur_dec = get("declination").get("Value", 0)
target_ra = (cur_ra + 0.1) % 24
print(f"  Slewing from RA={cur_ra:.4f} to RA={target_ra:.4f}, Dec={cur_dec:.4f}")
print("  >>> WATCH THE SCOPE <<<")
result = put("slewtocoordinatesasync", {
    "RightAscension": str(target_ra),
    "Declination": str(cur_dec)
})
print(f"  slewtocoordinatesasync result: {result}")

# Wait and poll
for i in range(10):
    time.sleep(1)
    slewing = get("slewing").get("Value", False)
    p = pos()
    print(f"  {i+1}s: slewing={slewing} {p}")
    if not slewing and i > 1:
        break

print(f"\n[11] Final position: {pos()}")

# Disconnect
print("\n[12] Disconnecting...")
put("connected", {"Connected": "false"})
print("Done. Did the scope move at any point (steps 6, 7, 10)?")
