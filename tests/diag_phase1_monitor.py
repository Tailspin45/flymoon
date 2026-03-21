#!/usr/bin/env python3
"""Poll detection status every 30s, flag stalls or stops."""
import time, json, urllib.request

URL = "http://localhost:8000/telescope/detect/status"
DURATION = 1800
INTERVAL = 30

start = time.time()
prev_frames = 0
stalls = 0

print("Monitoring detection stream for 30 min...")
print(f"{'Time':>8} {'Frames':>8} {'FPS':>6} {'Disk':>5} {'Dets':>5} Status")
print("-" * 55)

while time.time() - start < DURATION:
    try:
        resp = urllib.request.urlopen(URL, timeout=5)
        data = json.loads(resp.read())
        frames = data["total_frames"]
        fps = data["fps"]
        disk = "Y" if data["disk_detected"] else "N"
        dets = data["detections"]
        running = data["running"]

        delta = frames - prev_frames
        elapsed = time.time() - start
        status = "OK"
        if not running:
            status = "STOPPED"
        elif delta == 0 and prev_frames > 0:
            stalls += 1
            status = "STALL #%d" % stalls

        print("%7.0fs %8d %5.1f %5s %5d %s" % (elapsed, frames, fps, disk, dets, status))
        prev_frames = frames

        if not running:
            print("Detection stopped! Stream may have died.")
            break
    except Exception as e:
        elapsed = time.time() - start
        print("%7.0fs  ERROR: %s" % (elapsed, e))

    time.sleep(INTERVAL)

print("\nDone. Total stalls: %d" % stalls)
