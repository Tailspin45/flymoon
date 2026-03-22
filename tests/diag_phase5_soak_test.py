#!/usr/bin/env python3
"""
Phase 5 Diagnostic: RTSP Sustained-Operation (Soak) Test
=========================================================
Spawns one or more ffmpeg processes reading the Seestar RTSP stream and
logs frame counts per minute for each consumer.  Detects silent stream
deaths and measures time-to-recovery.

Three consumers mirror what the full system runs concurrently:
  • detector  — 160×90 @ 15 fps rawvideo (TransitDetector low-res)
  • hires     — 1920×1080 MJPEG (TransitDetector hi-res buffer)
  • preview   — 640×360 @ 10 fps (UI preview JPEG stream)

Usage:
    python tests/diag_phase5_soak_test.py --duration 7200

    # Quick 5-minute smoke test:
    python tests/diag_phase5_soak_test.py --duration 300

    # Only detector consumer:
    python tests/diag_phase5_soak_test.py --duration 300 --consumers detector

Options:
    --rtsp-host    Seestar IP (default: $SEESTAR_HOST or 192.168.4.112)
    --rtsp-port    RTSP port (default: $SEESTAR_RTSP_PORT or 4554)
    --duration     Test duration in seconds (default: 7200)
    --consumers    Comma-separated list: detector,hires,preview (default: all three)
    --log-dir      Directory for per-consumer logs (default: docs/diag_logs/)
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


FFMPEG = "ffmpeg"


# ── Consumer definitions ──────────────────────────────────────────────────

def _ffmpeg_cmd_detector(rtsp_url: str) -> list:
    """Low-res 160×90 rawvideo — mirrors TransitDetector._reader_loop."""
    return [
        FFMPEG, "-rtsp_transport", "tcp",
        "-timeout", "10000000",
        "-i", rtsp_url,
        "-vf", "scale=160:90",
        "-r", "15",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-an",
        "pipe:1",
    ]


def _ffmpeg_cmd_hires(rtsp_url: str) -> list:
    """Full-res MJPEG — mirrors TransitDetector._hires_reader_loop."""
    return [
        FFMPEG, "-rtsp_transport", "tcp",
        "-timeout", "10000000",
        "-i", rtsp_url,
        "-f", "mjpeg",
        "-q:v", "3",
        "pipe:1",
    ]


def _ffmpeg_cmd_preview(rtsp_url: str) -> list:
    """640×360 MJPEG preview — mirrors the UI preview feed."""
    return [
        FFMPEG, "-rtsp_transport", "tcp",
        "-timeout", "10000000",
        "-i", rtsp_url,
        "-vf", "scale=640:360",
        "-r", "10",
        "-f", "mjpeg",
        "-q:v", "5",
        "pipe:1",
    ]


CONSUMERS = {
    "detector": (_ffmpeg_cmd_detector, 160 * 90 * 3),   # raw frame bytes
    "hires":    (_ffmpeg_cmd_hires,    None),             # MJPEG — count by SOI marker
    "preview":  (_ffmpeg_cmd_preview,  None),             # MJPEG
}


# ── Consumer thread ───────────────────────────────────────────────────────

class ConsumerThread:
    """Runs one ffmpeg consumer, counting frames and detecting stream loss."""

    RECONNECT_DELAY_INIT = 2
    RECONNECT_DELAY_MAX  = 30

    def __init__(self, name: str, rtsp_url: str, duration: int, log_file: Path):
        self.name = name
        self.rtsp_url = rtsp_url
        self.duration = duration
        self.log_file = log_file

        self._cmd_fn, self._frame_bytes = CONSUMERS[name]
        self._total_frames = 0
        self._minute_frames = 0
        self._deaths = 0           # stream death count
        self._last_frame_ts = None # wall time of last frame received
        self._max_gap_s = 0.0      # longest gap without a frame
        self._recovery_times = []  # list of (death_ts, recovery_ts) tuples

        self._running = False
        self._thread = None
        self._minute_log: list = []  # (minute_idx, frame_count) per minute

    def start(self):
        self._running = True
        self._start_ts = time.monotonic()
        self._thread = threading.Thread(target=self._run, name=f"soak-{self.name}", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout=timeout)

    def stats(self) -> dict:
        return {
            "name": self.name,
            "total_frames": self._total_frames,
            "stream_deaths": self._deaths,
            "max_gap_s": round(self._max_gap_s, 1),
            "avg_recovery_s": (
                round(sum(r - d for d, r in self._recovery_times) / len(self._recovery_times), 1)
                if self._recovery_times else None
            ),
            "max_recovery_s": (
                round(max(r - d for d, r in self._recovery_times), 1)
                if self._recovery_times else None
            ),
        }

    # ── internal ─────────────────────────────────────────────────────────

    def _log(self, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{ts}][{self.name}] {msg}"
        print(line)
        with open(self.log_file, "a") as f:
            f.write(line + "\n")

    def _count_frames_rawvideo(self, buf: bytes) -> int:
        return len(buf) // self._frame_bytes

    def _count_frames_mjpeg(self, buf: bytes) -> int:
        """Count JPEG SOI markers (0xFF 0xD8) as a proxy for frame count."""
        count = 0
        pos = 0
        while True:
            idx = buf.find(b"\xff\xd8", pos)
            if idx == -1:
                break
            count += 1
            pos = idx + 2
        return count

    def _run(self):
        deadline = time.monotonic() + self.duration
        reconnect_delay = self.RECONNECT_DELAY_INIT
        death_ts = None

        self._log(f"Starting (duration={self.duration}s, cmd={self._cmd_fn.__name__})")

        while self._running and time.monotonic() < deadline:
            cmd = self._cmd_fn(self.rtsp_url)
            proc = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=65536,
                )
                reconnect_delay = self.RECONNECT_DELAY_INIT
                if death_ts is not None:
                    recovery_ts = time.monotonic()
                    self._recovery_times.append((death_ts, recovery_ts))
                    self._log(f"Stream recovered (gap={recovery_ts - death_ts:.1f}s)")
                    death_ts = None

                self._last_frame_ts = time.monotonic()
                buf = b""

                while self._running and time.monotonic() < deadline:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break  # stream ended

                    buf += chunk
                    now = time.monotonic()

                    # Count frames in buffer
                    if self._frame_bytes:
                        n = self._count_frames_rawvideo(buf)
                        buf = buf[n * self._frame_bytes:]
                    else:
                        n = self._count_frames_mjpeg(buf)
                        # Keep last ~4 KB in case a frame straddles a chunk boundary
                        buf = buf[-4096:] if len(buf) > 4096 else buf

                    if n > 0:
                        self._total_frames += n
                        self._minute_frames += n
                        gap = now - self._last_frame_ts
                        if gap > self._max_gap_s:
                            self._max_gap_s = gap
                        self._last_frame_ts = now

            except Exception as exc:
                self._log(f"ffmpeg error: {exc}")
            finally:
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                        proc.wait(timeout=3)
                    except Exception:
                        pass

            if not self._running or time.monotonic() >= deadline:
                break

            self._deaths += 1
            if death_ts is None:
                death_ts = time.monotonic()

            gap_so_far = time.monotonic() - (self._last_frame_ts or time.monotonic())
            self._log(f"Stream lost (death #{self._deaths}, gap so far={gap_so_far:.1f}s) — "
                      f"reconnecting in {reconnect_delay}s")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, self.RECONNECT_DELAY_MAX)

        self._log(f"Done — {self._total_frames} total frames, {self._deaths} deaths")


# ── Minute-ticker ─────────────────────────────────────────────────────────

class MinuteTicker:
    """Logs per-minute frame counts for all consumers."""

    def __init__(self, consumers: list, log_file: Path, duration: int):
        self.consumers = consumers
        self.log_file = log_file
        self.duration = duration
        self._thread = None
        self._running = False

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, name="minute-ticker", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        minute = 0
        deadline = time.monotonic() + self.duration
        while self._running and time.monotonic() < deadline:
            time.sleep(60)
            if not self._running:
                break
            minute += 1
            parts = [f"Minute {minute:4d}"]
            for c in self.consumers:
                fps = c._minute_frames / 60.0
                parts.append(f"{c.name}={c._minute_frames:5d}fr ({fps:.1f}fps)")
                c._minute_log.append((minute, c._minute_frames))
                c._minute_frames = 0
            line = "  ".join(parts)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            full = f"[{ts}][ticker] {line}"
            print(full)
            with open(self.log_file, "a") as f:
                f.write(full + "\n")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Phase 5: RTSP soak test")
    parser.add_argument("--rtsp-host", default=os.getenv("SEESTAR_HOST", "192.168.4.112"))
    parser.add_argument("--rtsp-port", type=int, default=int(os.getenv("SEESTAR_RTSP_PORT", "4554")))
    parser.add_argument("--duration", type=int, default=7200, help="Test duration (seconds)")
    parser.add_argument("--consumers", default="detector,hires,preview",
                        help="Comma-separated consumers to run")
    parser.add_argument("--log-dir", default="docs/diag_logs")
    args = parser.parse_args()

    rtsp_url = f"rtsp://{args.rtsp_host}:{args.rtsp_port}/stream"
    consumer_names = [c.strip() for c in args.consumers.split(",") if c.strip()]
    invalid = [n for n in consumer_names if n not in CONSUMERS]
    if invalid:
        print(f"Unknown consumers: {invalid}. Valid: {list(CONSUMERS)}")
        sys.exit(1)

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    combined_log = log_dir / f"phase5_soak_{stamp}.log"
    summary_log  = log_dir / f"phase5_soak_{stamp}_summary.txt"

    print(f"\n{'='*60}")
    print("Phase 5: RTSP Sustained-Operation Soak Test")
    print(f"{'='*60}")
    print(f"  RTSP URL   : {rtsp_url}")
    print(f"  Duration   : {args.duration}s ({args.duration/3600:.1f}h)")
    print(f"  Consumers  : {consumer_names}")
    print(f"  Log        : {combined_log}")
    print(f"{'='*60}\n")

    # Verify ffmpeg is available
    global FFMPEG
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        from src.constants import get_ffmpeg_path
        FFMPEG = get_ffmpeg_path() or "ffmpeg"

    # Create consumers
    consumers = [
        ConsumerThread(name, rtsp_url, args.duration, combined_log)
        for name in consumer_names
    ]

    # Start minute ticker
    ticker = MinuteTicker(consumers, combined_log, args.duration)

    print(f"Starting {len(consumers)} consumer(s) … press Ctrl+C to abort early.\n")
    start_wall = time.time()

    try:
        for c in consumers:
            c.start()
        ticker.start()

        # Progress bar
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            elapsed = time.time() - start_wall
            pct = min(100, int(elapsed / args.duration * 100))
            deaths = sum(c._deaths for c in consumers)
            sys.stdout.write(
                f"\r  Elapsed: {int(elapsed):5d}s / {args.duration}s  "
                f"({pct:3d}%)  stream deaths: {deaths}"
            )
            sys.stdout.flush()
            time.sleep(5)

    except KeyboardInterrupt:
        print("\n\nAborted by user.")

    finally:
        ticker.stop()
        for c in consumers:
            c.stop()
        for c in consumers:
            c.join(timeout=10)

    elapsed = time.time() - start_wall
    print(f"\n\n{'='*60}")
    print("Soak Test Summary")
    print(f"{'='*60}")
    print(f"  Actual duration : {elapsed:.0f}s")

    all_pass = True
    rows = []
    for c in consumers:
        st = c.stats()
        avg_fps = st["total_frames"] / max(elapsed, 1)
        max_rec = st["max_recovery_s"] or 0
        deaths   = st["stream_deaths"]
        gap      = st["max_gap_s"]

        # Pass criteria: max recovery < 30s, total deaths reasonable
        ok_recovery = (max_rec == 0 or max_rec < 30)
        row_pass = ok_recovery
        if not row_pass:
            all_pass = False

        status = "✅ PASS" if row_pass else "❌ FAIL"
        rows.append({**st, "avg_fps": round(avg_fps, 1), "status": status})

        print(f"\n  [{status}] {c.name}")
        print(f"    total frames    : {st['total_frames']}")
        print(f"    avg fps         : {avg_fps:.1f}")
        print(f"    stream deaths   : {deaths}")
        print(f"    max gap (s)     : {gap}")
        print(f"    avg recovery (s): {st['avg_recovery_s']}")
        print(f"    max recovery (s): {max_rec}")

        # Per-minute breakdown
        if c._minute_log:
            low = min(fr for _, fr in c._minute_log)
            high = max(fr for _, fr in c._minute_log)
            print(f"    per-minute range: {low}–{high} frames/min")
            zero_mins = [m for m, fr in c._minute_log if fr == 0]
            if zero_mins:
                print(f"    ⚠  Zero-frame minutes: {zero_mins}")

    print(f"\n  Overall: {'✅ PASS' if all_pass else '❌ FAIL'}")

    # Write summary
    with open(summary_log, "w") as f:
        import json
        f.write(json.dumps({"duration_s": elapsed, "consumers": rows}, indent=2))
    print(f"\n  Summary written: {summary_log}")
    print(f"{'='*60}\n")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
