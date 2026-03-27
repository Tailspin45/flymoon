#!/usr/bin/env python3
"""Phase 1 RTSP Stability Diagnostic — isolate silent stream death.

Usage:
    python tests/diag_phase1_rtsp_stability.py [--host HOST] [--port PORT] [--duration SECS]

Runs escalating concurrency tests to pinpoint when/why RTSP streams die:
  Test 1: Single low-res stream (baseline)
  Test 2: Two streams (low-res + hi-res)
  Test 3: Three streams (low-res + hi-res + preview)
  Test 4: Three streams with Flask app running (full production)

Each test logs per-second frame rate and bytes/sec.  On stream death, captures
FFmpeg exit code, last stderr lines, and elapsed time.

Results are saved to docs/diag_logs/phase1_rtsp_stability.log
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FFMPEG = "ffmpeg"
LOG_DIR = Path(__file__).resolve().parent.parent / "docs" / "diag_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "phase1_rtsp_stability.log"


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


class StreamMonitor:
    """Monitor a single FFmpeg RTSP stream, tracking fps and health."""

    def __init__(self, name: str, rtsp_url: str, ffmpeg_args: list[str]):
        self.name = name
        self.rtsp_url = rtsp_url
        self.ffmpeg_args = ffmpeg_args
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self._running = False

        # Metrics
        self.total_frames = 0
        self.total_bytes = 0
        self.start_time = 0.0
        self.death_time: float | None = None
        self.death_reason = ""
        self.exit_code: int | None = None
        self.stderr_tail = ""
        self._fps_history: deque[tuple[float, int]] = deque()  # (time, frame_count)
        self._last_frame_time = 0.0

    def start(self):
        self._running = True
        self.start_time = time.time()
        self._last_frame_time = self.start_time
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self._running = False
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass

    def _run(self):
        cmd = [
            FFMPEG,
            "-rtsp_transport",
            "tcp",
            "-timeout",
            "10000000",
            "-i",
            self.rtsp_url,
        ] + self.ffmpeg_args
        log(f"[{self.name}] Launching: {' '.join(cmd)}")

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1024 * 1024,
            )
        except FileNotFoundError:
            log(f"[{self.name}] ERROR: ffmpeg not found")
            self.death_reason = "ffmpeg_not_found"
            return

        # Stderr reader thread (non-blocking capture)
        stderr_lines: list[str] = []

        def _read_stderr():
            try:
                for line in self.proc.stderr:
                    stderr_lines.append(line.decode("utf-8", errors="replace").rstrip())
                    if len(stderr_lines) > 100:
                        stderr_lines.pop(0)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        # Determine read mode based on output format
        is_rawvideo = "-f" in self.ffmpeg_args and "rawvideo" in self.ffmpeg_args
        if is_rawvideo:
            # Fixed-size frame reads (like detection reader)
            self.ffmpeg_args.index("rawvideo")
            # Parse dimensions from -vf scale=WxH
            w, h = 160, 90  # defaults
            for i, a in enumerate(self.ffmpeg_args):
                if a.startswith("scale="):
                    parts = a.split("=")[1].split(":")
                    w, h = int(parts[0]), int(parts[1])
            frame_bytes = w * h * 3
            self._read_rawvideo(frame_bytes)
        else:
            # MJPEG chunk reads (like hi-res / preview reader)
            self._read_mjpeg()

        # Stream ended — capture diagnostics
        self.exit_code = self.proc.poll()
        self.stderr_tail = "\n".join(stderr_lines[-20:])
        elapsed = time.time() - self.start_time
        if not self.death_reason:
            self.death_reason = "stream_ended"
        self.death_time = time.time()
        log(
            f"[{self.name}] DIED after {elapsed:.1f}s — reason={self.death_reason} "
            f"exit_code={self.exit_code} frames={self.total_frames}"
        )
        if self.stderr_tail:
            log(f"[{self.name}] Last stderr:\n{self.stderr_tail}")

    def _read_rawvideo(self, frame_bytes: int):
        while self._running:
            try:
                raw = self.proc.stdout.read(frame_bytes)
            except Exception as e:
                self.death_reason = f"read_exception: {e}"
                break
            if len(raw) < frame_bytes:
                self.death_reason = f"short_read: got {len(raw)}/{frame_bytes}"
                break
            self.total_frames += 1
            self.total_bytes += len(raw)
            self._last_frame_time = time.time()
            self._fps_history.append((self._last_frame_time, self.total_frames))

    def _read_mjpeg(self):
        SOI = b"\xff\xd8"
        EOI = b"\xff\xd9"
        buf = b""
        while self._running:
            try:
                chunk = self.proc.stdout.read(65536)
            except Exception as e:
                self.death_reason = f"read_exception: {e}"
                break
            if not chunk:
                self.death_reason = "empty_read"
                break
            buf += chunk
            self.total_bytes += len(chunk)

            while True:
                soi = buf.find(SOI)
                if soi < 0:
                    buf = b""
                    break
                eoi = buf.find(EOI, soi + 2)
                if eoi < 0:
                    buf = buf[soi:]
                    break
                buf = buf[eoi + 2 :]
                self.total_frames += 1
                self._last_frame_time = time.time()
                self._fps_history.append((self._last_frame_time, self.total_frames))

    def current_fps(self) -> float:
        """Compute fps over the last 5 seconds."""
        now = time.time()
        cutoff = now - 5.0
        while self._fps_history and self._fps_history[0][0] < cutoff:
            self._fps_history.popleft()
        if len(self._fps_history) < 2:
            return 0.0
        dt = self._fps_history[-1][0] - self._fps_history[0][0]
        if dt <= 0:
            return 0.0
        return (len(self._fps_history) - 1) / dt

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self._running

    def stall_seconds(self) -> float:
        """Seconds since last frame."""
        if self._last_frame_time == 0:
            return 0.0
        return time.time() - self._last_frame_time

    def summary(self) -> dict:
        elapsed = time.time() - self.start_time if self.start_time else 0
        return {
            "name": self.name,
            "alive": self.is_alive(),
            "elapsed_s": round(elapsed, 1),
            "total_frames": self.total_frames,
            "avg_fps": round(self.total_frames / elapsed, 1) if elapsed > 0 else 0,
            "current_fps": round(self.current_fps(), 1),
            "stall_s": round(self.stall_seconds(), 1),
            "total_MB": round(self.total_bytes / 1e6, 1),
            "death_reason": self.death_reason,
            "exit_code": self.exit_code,
        }


class Heartbeat:
    """JSON-RPC heartbeat to keep Seestar alive (scope_get_equ_coord every 3s)."""

    def __init__(self, host: str, port: int = 4700):
        self.host = host
        self.port = port
        self._running = False
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._seq = 0
        self.pings_sent = 0
        self.pings_failed = 0

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def _connect(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5)
            self._sock.connect((self.host, self.port))
            log(f"[Heartbeat] Connected to {self.host}:{self.port}")
            return True
        except Exception as e:
            log(f"[Heartbeat] Connect failed: {e}")
            return False

    def _send_ping(self) -> bool:
        self._seq += 1
        msg = (
            json.dumps(
                {"jsonrpc": "2.0", "method": "scope_get_equ_coord", "id": self._seq}
            )
            + "\r\n"
        )
        try:
            self._sock.sendall(msg.encode())
            # Read response (non-critical, just drain)
            self._sock.settimeout(3)
            try:
                self._sock.recv(4096)
            except socket.timeout:
                pass
            self.pings_sent += 1
            return True
        except Exception as e:
            log(f"[Heartbeat] Ping failed: {e}")
            self.pings_failed += 1
            return False

    def _loop(self):
        if not self._connect():
            log("[Heartbeat] WARNING: could not connect — scope may drop RTSP")
            return
        while self._running:
            if not self._send_ping():
                # Try reconnect
                try:
                    self._sock.close()
                except Exception:
                    pass
                if not self._connect():
                    time.sleep(3)
                    continue
            time.sleep(3)


def run_test(
    test_name: str,
    streams: list[StreamMonitor],
    duration: int,
    heartbeat: Heartbeat | None = None,
) -> list[dict]:
    """Run a set of streams for `duration` seconds, logging stats every 10s."""
    log(f"\n{'='*60}")
    log(f"TEST: {test_name}")
    log(f"Streams: {[s.name for s in streams]}")
    log(f"Duration: {duration}s")
    log(f"Heartbeat: {'active' if heartbeat else 'DISABLED'}")
    log(f"{'='*60}")

    for s in streams:
        s.start()

    # Wait for first frames (up to 15s)
    deadline = time.time() + 15
    while time.time() < deadline:
        if all(s.total_frames > 0 for s in streams):
            break
        time.sleep(0.5)

    for s in streams:
        if s.total_frames == 0:
            log(f"[{s.name}] WARNING: no frames received after 15s")

    start = time.time()
    next_report = start + 10

    while time.time() - start < duration:
        time.sleep(1)
        now = time.time()

        # Check for deaths
        for s in streams:
            if s.death_time and not hasattr(s, "_death_logged"):
                s._death_logged = True
                log(f"[{s.name}] STREAM DEATH at {now - start:.1f}s — {s.death_reason}")

        # Periodic report
        if now >= next_report:
            parts = []
            for s in streams:
                status = "OK" if s.is_alive() else "DEAD"
                parts.append(f"{s.name}={s.current_fps():.1f}fps/{status}")
            log(f"  [{now - start:.0f}s] {' | '.join(parts)}")
            next_report = now + 10

    # Stop all
    for s in streams:
        s.stop()

    # Final summary
    results = []
    for s in streams:
        summary = s.summary()
        results.append(summary)
        log(f"  Result: {json.dumps(summary)}")

    return results


def make_streams(rtsp_url: str, which: list[str]) -> list[StreamMonitor]:
    """Create stream monitors by name."""
    streams = []
    if "lowres" in which:
        streams.append(
            StreamMonitor(
                "lowres-detect",
                rtsp_url,
                [
                    "-vf",
                    "scale=160:90",
                    "-r",
                    "15",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-an",
                    "pipe:1",
                ],
            )
        )
    if "hires" in which:
        streams.append(
            StreamMonitor(
                "hires-buffer",
                rtsp_url,
                ["-f", "mjpeg", "-q:v", "3", "-r", "30", "-an", "pipe:1"],
            )
        )
    if "preview" in which:
        streams.append(
            StreamMonitor(
                "preview",
                rtsp_url,
                [
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "mjpeg",
                    "-q:v",
                    "5",
                    "-r",
                    "10",
                    "-an",
                    "pipe:1",
                ],
            )
        )
    return streams


def main():
    parser = argparse.ArgumentParser(description="Phase 1 RTSP Stability Diagnostic")
    parser.add_argument("--host", default=os.getenv("SEESTAR_HOST", "192.168.110.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("SEESTAR_RTSP_PORT", "4554"))
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=600,
        help="Duration per test in seconds (default: 600 = 10 min)",
    )
    parser.add_argument(
        "--test", type=int, default=0, help="Run only test N (1-4), 0 = all"
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Quick mode: 60s per test instead of full duration",
    )
    args = parser.parse_args()

    rtsp_url = f"rtsp://{args.host}:{args.port}/stream"
    duration = 60 if args.quick else args.duration

    log(f"\nPhase 1 RTSP Stability Diagnostic")
    log(f"Date: {datetime.now().isoformat()}")
    log(f"RTSP URL: {rtsp_url}")
    log(f"Duration per test: {duration}s")
    log(f"Log file: {LOG_FILE}")

    # Start heartbeat to keep Seestar alive
    hb = Heartbeat(args.host, port=4700)
    hb.start()
    time.sleep(1)  # let heartbeat connect before starting streams

    all_results = {}

    tests = {
        1: ("Single stream (low-res only)", ["lowres"]),
        2: ("Dual stream (low-res + hi-res)", ["lowres", "hires"]),
        3: (
            "Triple stream (low-res + hi-res + preview)",
            ["lowres", "hires", "preview"],
        ),
    }

    for test_num, (name, stream_names) in tests.items():
        if args.test and args.test != test_num:
            continue
        streams = make_streams(rtsp_url, stream_names)
        results = run_test(f"Test {test_num}: {name}", streams, duration, heartbeat=hb)
        all_results[f"test_{test_num}"] = results
        # Brief pause between tests to let Seestar recover
        if test_num < 3:
            log(f"\nCooldown 10s before next test...")
            time.sleep(10)

    hb.stop()
    log(f"[Heartbeat] Total pings: {hb.pings_sent}, failed: {hb.pings_failed}")

    # Summary
    log(f"\n{'='*60}")
    log("SUMMARY")
    log(f"{'='*60}")
    any_died = False
    for test_key, results in all_results.items():
        for r in results:
            died = r["death_reason"] not in ("", "stream_ended")
            status = (
                "DIED"
                if died
                else (
                    "SURVIVED"
                    if r["alive"] or r["elapsed_s"] >= duration - 5
                    else "DIED"
                )
            )
            if died:
                any_died = True
            log(
                f"  {test_key}/{r['name']}: {status} — "
                f"avg {r['avg_fps']}fps, {r['total_frames']} frames, "
                f"{r['total_MB']}MB"
            )
            if r["death_reason"] and r["death_reason"] != "stream_ended":
                log(f"    Death: {r['death_reason']} (exit={r['exit_code']})")

    if not any_died:
        log("\nAll streams survived. To reproduce the production failure, try:")
        log("  1. Run with --duration 1800 (30 min)")
        log("  2. Run with Flask app active (python app.py in another terminal)")
        log("  3. Check if failure only occurs during mode transitions")
    else:
        log("\nStream death detected! Check stderr output above for root cause.")
        log("Common causes:")
        log("  - Max concurrent RTSP clients exceeded")
        log("  - FFmpeg socket timeout (10s inactivity)")
        log("  - Seestar firmware dropping connections under load")

    log(f"\nFull log: {LOG_FILE}")


if __name__ == "__main__":
    # Handle Ctrl+C gracefully
    signal.signal(signal.SIGINT, lambda *_: (log("\nInterrupted by user"), sys.exit(0)))
    main()
