#!/usr/bin/env python3
"""
Phase 4 Diagnostic: Nudge Direction Validation
===============================================
Verifies that each nudge direction produces movement in the expected
physical/angular direction.

For each of the 4 directions (up/down/left/right):
  1. Record tilt + compass before
  2. Send a 2-second nudge
  3. Record tilt + compass after
  4. Compare: did the scope move in the expected direction?

Expected results:
  up   → tilt_angle increases (scope arm raises)
  down → tilt_angle decreases (scope arm lowers)
  left → compass_direction increases (scope rotates CW when viewed from top)
         [or decreases depending on mount orientation — we just record direction]
  right→ compass_direction decreases

⚠️  WARNING: Stops solar tracking. Will move scope in 4 directions.
    Use --restore-solar to restart solar mode when done.
    Use --delay-between to give yourself time to observe each nudge.

Usage:
  python tests/diag_phase4_nudge_directions.py --restore-solar --delay-between 3
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("SEESTAR_HOST", "192.168.4.112")
PORT = int(os.getenv("SEESTAR_PORT", "4700"))

# API angle convention (speed_move adds +180 internally)
NUDGE_ANGLES = {"up": 90, "down": 270, "left": 0, "right": 180}
# Which sensor should change for each direction
EXPECTED_CHANGES = {
    "up": {"sensor": "tilt", "direction": "+", "label": "tilt increases (arm raises)"},
    "down": {
        "sensor": "tilt",
        "direction": "-",
        "label": "tilt decreases (arm lowers)",
    },
    "left": {
        "sensor": "compass",
        "direction": "?",
        "label": "compass changes (az rotation)",
    },
    "right": {
        "sensor": "compass",
        "direction": "?",
        "label": "compass changes (az rotation)",
    },
}


def _pass(name, detail=""):
    print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))
    return {"test": name, "status": "PASS", "detail": detail}


def _fail(name, detail=""):
    print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
    return {"test": name, "status": "FAIL", "detail": detail}


def _warn(name, detail=""):
    print(f"  WARN  {name}" + (f" — {detail}" if detail else ""))
    return {"test": name, "status": "WARN", "detail": detail}


def _info(msg):
    print(f"        {msg}")


def get_sensors(client):
    """Get tilt and compass from device_state."""
    r = client._send_command("get_device_state", quiet=True, timeout_override=5)
    if not r:
        return None, None
    bal = (r.get("balance_sensor") or {}).get("data") or {}
    comp = (r.get("compass_sensor") or {}).get("data") or {}
    return bal.get("angle"), comp.get("direction")


def get_equ(client):
    """Get RA/Dec."""
    r = client._send_command("scope_get_equ_coord", quiet=True, timeout_override=5)
    if not r:
        return None, None
    return r.get("ra"), r.get("dec")


def test_nudge(client, direction, nudge_sec, delay_before):
    """Test a single nudge direction. Returns result dict."""
    if delay_before > 0:
        _info(f"Waiting {delay_before}s before {direction} nudge (observe scope) …")
        time.sleep(delay_before)

    # Before snapshot
    tilt_before, compass_before = get_sensors(client)
    ra_before, dec_before = get_equ(client)
    _info(
        f"Before: tilt={tilt_before}° compass={compass_before}° RA={ra_before}h Dec={dec_before}°"
    )

    # Send nudge via raw command (already in scenery mode)
    api_angle = NUDGE_ANGLES[direction]
    fw_angle = (api_angle + 180) % 360
    client._send_command(
        "scope_speed_move",
        params={"speed": 50, "angle": fw_angle, "dur_sec": nudge_sec},
        expect_response=False,
    )
    _info(
        f"Nudge sent: direction={direction} api_angle={api_angle}° fw_angle={fw_angle}° dur={nudge_sec}s"
    )

    time.sleep(nudge_sec + 1.0)

    # After snapshot
    tilt_after, compass_after = get_sensors(client)
    ra_after, dec_after = get_equ(client)
    _info(
        f"After:  tilt={tilt_after}° compass={compass_after}° RA={ra_after}h Dec={dec_after}°"
    )

    delta_tilt = None
    delta_compass = None
    delta_ra_deg = None
    delta_dec = None

    if tilt_before is not None and tilt_after is not None:
        delta_tilt = round(float(tilt_after) - float(tilt_before), 3)
    if compass_before is not None and compass_after is not None:
        delta_compass = round(float(compass_after) - float(compass_before), 3)
        # Wrap-aware
        if abs(delta_compass) > 180:
            delta_compass = delta_compass - 360 * (1 if delta_compass > 0 else -1)
        delta_compass = round(delta_compass, 3)
    if ra_before is not None and ra_after is not None:
        delta_ra_deg = round((float(ra_after) - float(ra_before)) * 15, 4)
    if dec_before is not None and dec_after is not None:
        delta_dec = round(float(dec_after) - float(dec_before), 4)

    result = {
        "direction": direction,
        "api_angle": api_angle,
        "fw_angle": fw_angle,
        "tilt_before": tilt_before,
        "tilt_after": tilt_after,
        "delta_tilt": delta_tilt,
        "compass_before": compass_before,
        "compass_after": compass_after,
        "delta_compass": delta_compass,
        "ra_before": ra_before,
        "ra_after": ra_after,
        "delta_ra_deg": delta_ra_deg,
        "dec_before": dec_before,
        "dec_after": dec_after,
        "delta_dec": delta_dec,
    }

    exp = EXPECTED_CHANGES[direction]
    sensor = exp["sensor"]
    expected_dir = exp["direction"]
    label = exp["label"]

    if sensor == "tilt" and delta_tilt is not None:
        moved = abs(delta_tilt) > 0.1
        correct_dir = (delta_tilt > 0) if expected_dir == "+" else (delta_tilt < 0)
        _info(f"Δtilt={delta_tilt:+.3f}° (expected {label})")
        if moved and correct_dir:
            return _pass(f"nudge_{direction}", f"Δtilt={delta_tilt:+.3f}°"), result
        elif moved and not correct_dir:
            return (
                _fail(
                    f"nudge_{direction}",
                    f"Δtilt={delta_tilt:+.3f}° — WRONG DIRECTION (expected {label})",
                ),
                result,
            )
        else:
            return (
                _warn(
                    f"nudge_{direction}",
                    f"Δtilt={delta_tilt:+.3f}° — no measurable movement",
                ),
                result,
            )

    elif sensor == "compass" and delta_compass is not None:
        moved = abs(delta_compass) > 0.3
        _info(f"Δcompass={delta_compass:+.3f}° (expected {label})")
        if moved:
            return (
                _pass(f"nudge_{direction}", f"Δcompass={delta_compass:+.3f}°"),
                result,
            )
        else:
            return (
                _warn(
                    f"nudge_{direction}",
                    f"Δcompass={delta_compass:+.3f}° — no measurable movement",
                ),
                result,
            )

    else:
        return _warn(f"nudge_{direction}", "sensor data unavailable"), result


def run(host, port, directions, nudge_sec, restore_solar, delay_between, output_path):
    from src.seestar_client import SeestarClient

    print("=" * 70)
    print("PHASE 4: Nudge Direction Validation")
    print("=" * 70)
    print(f"Connecting to {host}:{port} …")
    print(f"Testing directions: {directions}  nudge_sec={nudge_sec}")
    print()

    client = SeestarClient(host=host, port=port, timeout=10, heartbeat_interval=99)
    results = []
    raw = {"nudge_tests": []}

    try:
        ok = client.connect()
        if not ok:
            raise RuntimeError("connect() returned False")
        results.append(_pass("connect"))
    except Exception as e:
        results.append(_fail("connect", str(e)))
        return results, raw

    time.sleep(0.5)

    # Switch to scenery mode once
    print("--- Switching to scenery mode ---")
    try:
        client.stop_view_mode()
        time.sleep(0.5)
        client.start_scenery_mode()
        time.sleep(2)
        results.append(_pass("switch_to_scenery"))
        _info("In scenery mode — solar tracking stopped")
    except Exception as e:
        results.append(_fail("switch_to_scenery", str(e)))
        try:
            client.disconnect()
        except:
            pass
        return results, raw

    print()

    # Test each direction
    for direction in directions:
        print(f"--- Testing nudge: {direction.upper()} ---")
        test_result, raw_data = test_nudge(client, direction, nudge_sec, delay_between)
        results.append(test_result)
        raw["nudge_tests"].append(raw_data)
        print()
        time.sleep(1)

    # Print direction summary
    print("--- Direction Summary ---")
    for r in [x for x in results if x["test"].startswith("nudge_")]:
        icon = "✓" if r["status"] == "PASS" else ("✗" if r["status"] == "FAIL" else "?")
        print(f"  {icon} {r['test']}: {r['detail']}")

    # Restore solar
    if restore_solar:
        print()
        print("--- Restoring solar mode ---")
        try:
            client.start_solar_mode()
            time.sleep(1)
            results.append(_pass("restore_solar"))
            _info("Solar mode restored")
        except Exception as e:
            results.append(_fail("restore_solar", str(e)))

    try:
        client.disconnect()
    except:
        pass

    passed = sum(1 for r in results if r["status"] == "PASS")
    warned = sum(1 for r in results if r["status"] == "WARN")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print()
    print("=" * 70)
    print(f"PASS={passed}  WARN={warned}  FAIL={failed}")
    print("=" * 70)

    output = {
        "phase": "phase4_nudge_directions",
        "host": host,
        "port": port,
        "directions_tested": directions,
        "nudge_sec": nudge_sec,
        "tests": results,
        "raw": raw,
        "summary": {"passed": passed, "warned": warned, "failed": failed},
    }
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults written to {out_path}")
    return results, raw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument(
        "--directions",
        nargs="+",
        default=["up", "down", "left", "right"],
        choices=["up", "down", "left", "right"],
    )
    parser.add_argument("--nudge-sec", type=int, default=2)
    parser.add_argument("--restore-solar", action="store_true")
    parser.add_argument(
        "--delay-between",
        type=float,
        default=2.0,
        help="Seconds to wait between nudges (lets you observe movement)",
    )
    parser.add_argument(
        "--output", default="docs/diag_logs/phase4_nudge_directions.json"
    )
    args = parser.parse_args()
    run(
        args.host,
        args.port,
        args.directions,
        args.nudge_sec,
        args.restore_solar,
        args.delay_between,
        args.output,
    )


if __name__ == "__main__":
    main()
