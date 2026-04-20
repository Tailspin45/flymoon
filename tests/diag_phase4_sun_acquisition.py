#!/usr/bin/env python3
"""Phase 4 / Phase 6 diagnostic: end-to-end Sun / Moon acquisition + centering.

Run once with the iPhone Seestar app OPEN, then a second time with the app
force-quit. Compare the two reports.

Connects to Seestar + ALPACA, runs the init sequence, triggers solar or
lunar mode (with verification), polls until slew settles, grabs an RTSP
frame for disk detection, then runs ``src.disk_center.center_on_disk``
(unless ``--no-center`` is passed) and prints a per-iteration offset
trajectory followed by a compact summary block.

Usage
-----
  python3 tests/diag_phase4_sun_acquisition.py --target sun
  python3 tests/diag_phase4_sun_acquisition.py --target moon --iterations 8
  python3 tests/diag_phase4_sun_acquisition.py --no-center  # diagnose only

Exit code is 0 on a successful centering (or diagnose-only mode) and 1 on
any hard failure (no connect, disk never detected, etc.).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

from src import logger  # noqa: E402
from src.alpaca_client import AlpacaClient  # noqa: E402
from src.disk_center import _grab_frame_once, center_on_disk, detect_disk  # noqa: E402
from src.seestar_client import SeestarClient  # noqa: E402


def _fmt(x):
    if x is None:
        return "None"
    if isinstance(x, float):
        return f"{x:.3f}"
    return str(x)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["sun", "moon"], default="sun")
    ap.add_argument("--iterations", type=int, default=6)
    ap.add_argument("--tolerance-px", type=int, default=8)
    ap.add_argument("--no-center", action="store_true",
                    help="Skip the centering pass (diagnose only)")
    args = ap.parse_args()

    host = os.getenv("SEESTAR_HOST", "")
    if not host:
        print("SEESTAR_HOST is not set (.env)", file=sys.stderr)
        return 1
    alpaca_host = os.getenv("SEESTAR_ALPACA_HOST", host)
    alpaca_port = int(os.getenv("SEESTAR_ALPACA_PORT", "32323"))

    print(f"[diag] connecting to Seestar {host}:4700…")
    client = SeestarClient(host=host)
    if not client.connect():
        print("[diag] Seestar connect failed", file=sys.stderr)
        return 1
    print(f"[diag] connecting to ALPACA {alpaca_host}:{alpaca_port}…")
    alpaca = AlpacaClient(host=alpaca_host, port=alpaca_port)
    if not alpaca.connect():
        print("[diag] ALPACA connect failed", file=sys.stderr)
        return 1

    # Kick the start-tracked-mode path (uses PR#1 master reclaim + verification
    # if present; on a plain main checkout it just falls through to the basic
    # path).
    if args.target == "sun":
        client.start_solar_mode()
    else:
        client.start_lunar_mode()

    # Poll until slew settles.
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if not alpaca.is_slewing(timeout_sec=1.5):
                break
        except Exception:
            break
        time.sleep(0.5)

    # One-shot disk detection before centering
    rtsp_port = int(os.getenv("SEESTAR_RTSP_PORT", "4554"))
    frame = _grab_frame_once(host, rtsp_port)
    if frame is None:
        print("[diag] RTSP frame grab failed — is the stream active?",
              file=sys.stderr)
        pre_offset = None
    else:
        h, w = frame.shape[:2]
        disk = detect_disk(frame)
        if disk is None:
            pre_offset = None
            print(f"[diag] disk not detected in {w}x{h} frame")
        else:
            cx, cy, radius = disk
            pre_offset = (cx - w / 2.0, cy - h / 2.0, radius, w, h)
            print(
                f"[diag] disk detected cx={cx} cy={cy} r={radius} "
                f"offset_px=({pre_offset[0]:.1f},{pre_offset[1]:.1f})"
            )

    center_result = None
    if not args.no_center:
        print(f"[diag] running center_on_disk (max_iters={args.iterations}, "
              f"tol={args.tolerance_px}px)…")
        center_result = center_on_disk(
            client,
            alpaca,
            None,
            target=args.target,
            max_iterations=args.iterations,
            tolerance_px=args.tolerance_px,
        )

    # Final report
    master_state = getattr(client, "master_state", "unknown")
    last_start = getattr(client, "_last_start_result", None) or {}
    print("")
    print("================ REPORT ================")
    print(f"target            : {args.target}")
    print(f"master_state      : {master_state}")
    print(f"mode_confirmed    : {_fmt(last_start.get('mode_confirmed'))}")
    print(f"tracking_confirmed: {_fmt(last_start.get('tracking_confirmed'))}")
    print(f"pre_offset_px     : {_fmt(pre_offset)}")
    if center_result:
        print(f"iterations        : {center_result.get('iterations')}")
        print(f"final_offset_px   : {_fmt(center_result.get('final_offset_px'))}")
        print(
            f"final_offset_arcs : {_fmt(center_result.get('final_offset_arcsec'))}"
        )
        print(f"success           : {center_result.get('success')}")
        print(f"reason            : {center_result.get('reason')}")
    else:
        print("centering         : skipped (--no-center)")
    print("========================================")

    if center_result and not center_result.get("success"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
