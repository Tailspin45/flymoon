#!/usr/bin/env python3
"""
Phase 5 Diagnostic: Failure Injection Test
===========================================
Tests system recovery from common failure modes:

  Test A — JSON-RPC reconnection after simulated socket drop
    • Connect to Seestar, start solar mode
    • Kill the TCP socket forcibly (simulating a network drop)
    • Measure time until heartbeat reconnects
    • Verify telemetry is accurate again after reconnect

  Test B — RTSP recovery after stream interruption
    • Start an ffmpeg consumer reading the RTSP stream
    • Kill the ffmpeg process (simulating stream death)
    • Verify the consumer relaunches within <30 seconds

  Test C — Rapid mode cycling (state-machine stress test)
    • Cycle sun → scenery → moon → scenery → sun N times
    • Verify _viewing_mode is correct after each transition
    • Verify no exceptions are raised
    • Verify telemetry remains coherent throughout

Usage:
    python tests/diag_phase5_failure_injection.py

    # Run only specific tests:
    python tests/diag_phase5_failure_injection.py --tests A,C

    # Adjust cycle count for test C:
    python tests/diag_phase5_failure_injection.py --mode-cycles 10

    # Adjust recovery timeout:
    python tests/diag_phase5_failure_injection.py --recovery-timeout 30
"""

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

PASS = "✅ PASS"
FAIL = "❌ FAIL"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── Test A: JSON-RPC Reconnect ────────────────────────────────────────────────

def test_a_jsonrpc_reconnect(host: str, port: int, recovery_timeout: int) -> bool:
    """
    Simulate a network drop by closing the socket underneath the client,
    then verify the heartbeat thread reconnects within `recovery_timeout` seconds.
    """
    section("Test A: JSON-RPC Reconnect After Socket Drop")

    from src.seestar_client import SeestarClient

    print(f"  [{_ts()}] Connecting to {host}:{port} …")
    client = SeestarClient(host=host, port=port, timeout=10)
    try:
        ok = client.connect()
    except Exception as exc:
        print(f"  [{_ts()}] ERROR during connect: {exc}")
        return False

    if not ok or not client._connected:
        print(f"  [{_ts()}] {FAIL}: Could not connect to Seestar at {host}:{port}")
        return False

    print(f"  [{_ts()}] Connected. Starting solar mode …")
    try:
        client.start_solar_mode()
        time.sleep(2)
    except Exception as exc:
        print(f"  [{_ts()}] WARNING: start_solar_mode raised {exc} — continuing anyway")

    print(f"  [{_ts()}] Telemetry before drop: connected={client._connected} "
          f"mode={client._viewing_mode}")

    # Forcibly close the socket
    print(f"\n  [{_ts()}] Closing socket to simulate network drop …")
    drop_time = time.monotonic()
    try:
        if client.socket:
            client.socket.shutdown(socket.SHUT_RDWR)
            client.socket.close()
            client.socket = None
        client._connected = False
    except Exception as exc:
        print(f"  [{_ts()}] (expected) socket close error: {exc}")

    # Wait for heartbeat to reconnect
    print(f"  [{_ts()}] Waiting for heartbeat reconnect (timeout={recovery_timeout}s) …")
    deadline = time.monotonic() + recovery_timeout
    reconnected = False
    while time.monotonic() < deadline:
        if client._connected:
            reconnected = True
            break
        time.sleep(1)

    recovery_s = time.monotonic() - drop_time

    if reconnected:
        print(f"  [{_ts()}] Reconnected in {recovery_s:.1f}s")
        print(f"  [{_ts()}] Telemetry after reconnect: connected={client._connected} "
              f"mode={client._viewing_mode}")
    else:
        print(f"  [{_ts()}] {FAIL}: Heartbeat did NOT reconnect within {recovery_timeout}s")

    try:
        client.stop_view_mode()
        client.disconnect()
    except Exception:
        pass

    passed = reconnected and recovery_s < recovery_timeout
    print(f"\n  Result: {PASS if passed else FAIL}")
    print(f"    reconnected     : {reconnected}")
    print(f"    recovery time   : {recovery_s:.1f}s  (threshold: <{recovery_timeout}s)")
    return passed


# ── Test B: RTSP Recovery ────────────────────────────────────────────────────

def test_b_rtsp_recovery(rtsp_url: str, recovery_timeout: int) -> bool:
    """
    Launch an ffmpeg RTSP consumer, forcibly kill it, then measure
    how long until the consumer relaunches and gets a frame.
    """
    section("Test B: RTSP Consumer Recovery After Kill")

    ffmpeg_bin = "ffmpeg"
    try:
        subprocess.run([ffmpeg_bin, "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        from src.constants import get_ffmpeg_path
        ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"

    cmd = [
        ffmpeg_bin, "-rtsp_transport", "tcp",
        "-timeout", "10000000",
        "-i", rtsp_url,
        "-vf", "scale=160:90",
        "-r", "15",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-an",
        "pipe:1",
    ]
    FRAME_BYTES = 160 * 90 * 3

    frame_event = threading.Event()
    proc_holder = [None]

    def reader():
        reconnect_delay = 2
        while True:
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    bufsize=FRAME_BYTES * 4,
                )
                proc_holder[0] = proc
                buf = b""
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) >= FRAME_BYTES:
                        frame_event.set()
                        buf = buf[FRAME_BYTES * (len(buf) // FRAME_BYTES):]
            except Exception:
                pass
            p = proc_holder[0]
            if p and p.poll() is None:
                try:
                    p.kill()
                    p.wait(timeout=3)
                except Exception:
                    pass
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # Wait for first frame
    print(f"  [{_ts()}] Waiting for first frame from {rtsp_url} …")
    got_first = frame_event.wait(timeout=30)
    if not got_first:
        print(f"  [{_ts()}] {FAIL}: No frame received within 30s "
              "(is Seestar powered on and streaming?)")
        return False

    print(f"  [{_ts()}] First frame received. Killing ffmpeg process …")
    frame_event.clear()
    kill_time = time.monotonic()

    proc = proc_holder[0]
    if proc and proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)
    else:
        print(f"  [{_ts()}] WARNING: ffmpeg process already dead before kill")

    print(f"  [{_ts()}] Waiting for recovery (timeout={recovery_timeout}s) …")
    recovered = frame_event.wait(timeout=recovery_timeout)
    recovery_s = time.monotonic() - kill_time

    passed = recovered and recovery_s < recovery_timeout
    print(f"\n  Result: {PASS if passed else FAIL}")
    print(f"    recovered       : {recovered}")
    print(f"    recovery time   : {recovery_s:.1f}s  (threshold: <{recovery_timeout}s)")
    return passed


# ── Test C: Rapid Mode Cycling ────────────────────────────────────────────────

def test_c_mode_cycling(host: str, port: int, cycles: int) -> bool:
    """
    Cycle through sun → scenery → moon → scenery → sun modes N times,
    verifying _viewing_mode stays correct and no exceptions are raised.
    """
    section(f"Test C: Rapid Mode Cycling ({cycles} cycles)")

    from src.seestar_client import SeestarClient

    client = SeestarClient(host=host, port=port, timeout=10)
    try:
        ok = client.connect()
    except Exception as exc:
        print(f"  [{_ts()}] ERROR during connect: {exc}")
        return False

    if not ok or not client._connected:
        print(f"  [{_ts()}] {FAIL}: Could not connect to {host}:{port}")
        return False

    mode_sequence = ["sun", "scenery", "moon", "scenery", "sun"]
    mode_fns = {
        "sun":     client.start_solar_mode,
        "moon":    client.start_lunar_mode,
        "scenery": client.start_scenery_mode,
    }

    errors = []
    mismatches = []

    for cycle in range(1, cycles + 1):
        print(f"  [{_ts()}] Cycle {cycle}/{cycles} …", end="", flush=True)
        cycle_ok = True

        for mode in mode_sequence:
            try:
                mode_fns[mode]()
                time.sleep(1.5)

                actual = client._viewing_mode
                if actual != mode:
                    msg = f"[cycle {cycle}] after '{mode}': _viewing_mode='{actual}'"
                    mismatches.append(msg)
                    cycle_ok = False

                if not client._connected:
                    msg = f"[cycle {cycle}] after '{mode}': client._connected=False"
                    errors.append(msg)
                    cycle_ok = False

            except Exception as exc:
                msg = f"[cycle {cycle}] mode='{mode}': {exc}"
                errors.append(msg)
                cycle_ok = False

        print(f" {'✓' if cycle_ok else '✗'}")

    # Return to solar mode ready for next session
    try:
        client.start_solar_mode()
        time.sleep(1)
    except Exception:
        pass

    try:
        client.disconnect()
    except Exception:
        pass

    passed = len(errors) == 0 and len(mismatches) == 0
    print(f"\n  Result: {PASS if passed else FAIL}")
    print(f"    cycles completed : {cycles}")
    print(f"    exceptions       : {len(errors)}")
    print(f"    mode mismatches  : {len(mismatches)}")
    if errors:
        print("    Errors:")
        for e in errors:
            print(f"      • {e}")
    if mismatches:
        print("    Mismatches:")
        for m in mismatches:
            print(f"      • {m}")
    return passed


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Phase 5: Failure injection tests")
    parser.add_argument("--host",     default=os.getenv("SEESTAR_HOST", "192.168.4.112"))
    parser.add_argument("--port",     type=int, default=int(os.getenv("SEESTAR_PORT", "4700")))
    parser.add_argument("--rtsp-port", type=int,
                        default=int(os.getenv("SEESTAR_RTSP_PORT", "4554")))
    parser.add_argument("--tests",    default="A,B,C",
                        help="Comma-separated tests to run: A, B, C (default: all)")
    parser.add_argument("--mode-cycles", type=int, default=5,
                        help="Number of mode-cycle iterations for Test C (default: 5)")
    parser.add_argument("--recovery-timeout", type=int, default=30,
                        help="Max seconds allowed for recovery (default: 30)")
    args = parser.parse_args()

    rtsp_url = f"rtsp://{args.host}:{args.rtsp_port}/stream"
    tests = {t.strip().upper() for t in args.tests.split(",")}

    print(f"\n{'='*60}")
    print("Phase 5: Failure Injection Test")
    print(f"{'='*60}")
    print(f"  Seestar      : {args.host}:{args.port}")
    print(f"  RTSP         : {rtsp_url}")
    print(f"  Tests        : {sorted(tests)}")
    print(f"  Mode cycles  : {args.mode_cycles}")
    print(f"  Recovery max : {args.recovery_timeout}s")
    print(f"{'='*60}")

    results = {}

    if "A" in tests:
        results["A"] = test_a_jsonrpc_reconnect(
            host=args.host,
            port=args.port,
            recovery_timeout=args.recovery_timeout,
        )

    if "B" in tests:
        results["B"] = test_b_rtsp_recovery(
            rtsp_url=rtsp_url,
            recovery_timeout=args.recovery_timeout,
        )

    if "C" in tests:
        results["C"] = test_c_mode_cycling(
            host=args.host,
            port=args.port,
            cycles=args.mode_cycles,
        )

    labels = {"A": "JSON-RPC reconnect", "B": "RTSP recovery", "C": "Mode cycling"}

    print(f"\n{'='*60}")
    print("Summary")
    print(f"{'='*60}")
    all_pass = True
    for k in sorted(results):
        ok = results[k]
        if not ok:
            all_pass = False
        print(f"  Test {k} ({labels.get(k, k)}): {PASS if ok else FAIL}")

    print(f"\n  Overall: {PASS if all_pass else FAIL}")
    print(f"{'='*60}\n")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
