#!/usr/bin/env python3
"""
RTSP Concurrent Stream Diagnostic
Tests 3 simultaneous FFmpeg readers against the Seestar RTSP stream,
matching production pipeline configuration.
"""

import subprocess
import threading
import time

RTSP_URL = "rtsp://192.168.4.112:4554/stream"
DURATION = 90  # seconds


def run_reader(name, cmd, results):
    """Run an FFmpeg reader, count frames per second, log events."""
    info = {
        "name": name,
        "cmd": " ".join(cmd),
        "first_frame_time": None,
        "death_time": None,
        "exit_code": None,
        "stderr_tail": "",
        "frame_counts": [],  # list of (timestamp, cumulative_frames)
        "total_frames": 0,
        "survived": False,
    }
    results[name] = info

    start = time.time()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        info["stderr_tail"] = str(e)
        info["death_time"] = time.time() - start
        return

    # Read stdout in chunks to count frames
    # For raw pipe readers, we know the frame size; for MJPEG we count JPEG SOI markers
    frame_count = 0
    last_log = start

    if "rawvideo" in cmd:
        # Low-res: 160x90 RGB24 = 160*90*3 = 43200 bytes per frame
        frame_size = 160 * 90 * 3
        buf = b""
        while time.time() - start < DURATION:
            try:
                chunk = proc.stdout.read(frame_size - len(buf))
                if not chunk:
                    break
                buf += chunk
                if len(buf) >= frame_size:
                    frame_count += 1
                    buf = b""
                    if info["first_frame_time"] is None:
                        info["first_frame_time"] = time.time() - start
                    now = time.time()
                    if now - last_log >= 1.0:
                        info["frame_counts"].append(
                            (round(now - start, 1), frame_count)
                        )
                        last_log = now
            except Exception:
                break
    else:
        # MJPEG pipe: count JPEG SOI markers (FF D8)
        while time.time() - start < DURATION:
            try:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                soi_count = chunk.count(b"\xff\xd8")
                if soi_count > 0:
                    frame_count += soi_count
                    if info["first_frame_time"] is None:
                        info["first_frame_time"] = time.time() - start
                now = time.time()
                if now - last_log >= 1.0:
                    info["frame_counts"].append((round(now - start, 1), frame_count))
                    last_log = now
            except Exception:
                break

    info["total_frames"] = frame_count

    # Check if process is still alive
    poll = proc.poll()
    if poll is not None:
        info["exit_code"] = poll
        info["death_time"] = time.time() - start
        info["survived"] = False
    else:
        info["survived"] = True
        proc.kill()

    # Grab stderr tail
    try:
        proc.stdout.close()
        stderr_data = proc.stderr.read()
        if stderr_data:
            lines = stderr_data.decode("utf-8", errors="replace").strip().split("\n")
            info["stderr_tail"] = "\n".join(lines[-10:])
        proc.stderr.close()
    except Exception:
        pass

    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def main():
    print(f"RTSP Concurrent Stream Diagnostic")
    print(f"URL: {RTSP_URL}")
    print(f"Duration: {DURATION}s")
    print(f"Starting 3 concurrent readers...\n")

    # Define the 3 readers matching production config
    readers = {
        "low-res (160x90 raw @15fps)": [
            "ffmpeg",
            "-rtsp_transport",
            "tcp",
            "-i",
            RTSP_URL,
            "-vf",
            "scale=160:90",
            "-r",
            "15",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-loglevel",
            "warning",
            "pipe:1",
        ],
        "high-res (mjpeg q3 @30fps)": [
            "ffmpeg",
            "-rtsp_transport",
            "tcp",
            "-i",
            RTSP_URL,
            "-c:v",
            "mjpeg",
            "-q:v",
            "3",
            "-r",
            "30",
            "-f",
            "mjpeg",
            "-loglevel",
            "warning",
            "pipe:1",
        ],
        "preview (mjpeg q5 @10fps)": [
            "ffmpeg",
            "-rtsp_transport",
            "tcp",
            "-i",
            RTSP_URL,
            "-c:v",
            "mjpeg",
            "-q:v",
            "5",
            "-r",
            "10",
            "-f",
            "mjpeg",
            "-loglevel",
            "warning",
            "pipe:1",
        ],
    }

    results = {}
    threads = []
    t_start = time.time()

    for name, cmd in readers.items():
        t = threading.Thread(target=run_reader, args=(name, cmd, results), daemon=True)
        threads.append(t)
        t.start()
        print(f"  Started: {name}")

    print(f"\nRunning for {DURATION}s...")

    # Wait for threads to finish (they self-limit to DURATION)
    for t in threads:
        t.join(timeout=DURATION + 30)

    elapsed = time.time() - t_start
    print(f"\nTest completed in {elapsed:.1f}s\n")

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for name, info in results.items():
        print(f"\n--- {name} ---")
        print(f"  Survived full {DURATION}s: {info['survived']}")
        if info["first_frame_time"] is not None:
            print(f"  First frame at: {info['first_frame_time']:.2f}s")
        else:
            print(f"  First frame at: NEVER (no frames received)")
        print(f"  Total frames: {info['total_frames']}")
        if info["total_frames"] > 0 and elapsed > 0:
            print(f"  Average FPS: {info['total_frames'] / elapsed:.1f}")
        if info["death_time"] is not None:
            print(
                f"  Died at: {info['death_time']:.1f}s (exit code: {info['exit_code']})"
            )
        if info["stderr_tail"]:
            print(f"  Stderr (last lines):")
            for line in info["stderr_tail"].split("\n"):
                print(f"    {line}")

        # Show per-second frame counts (sample every 10s)
        if info["frame_counts"]:
            print(f"  Frame count samples (time, cumulative):")
            samples = info["frame_counts"]
            # Show every ~10s
            step = max(1, len(samples) // 9)
            for i in range(0, len(samples), step):
                ts, fc = samples[i]
                print(f"    t={ts:>6.1f}s  frames={fc}")
            # Always show last
            if (
                samples[-1]
                != samples[min(len(samples) - 1, (len(samples) // step) * step)]
            ):
                ts, fc = samples[-1]
                print(f"    t={ts:>6.1f}s  frames={fc}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
