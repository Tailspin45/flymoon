#!/usr/bin/env python3
"""
Phase 4 Diagnostic: Position Feedback in Scenery Mode
======================================================
The core question: does scope_get_equ_coord update after a physical nudge
when the scope is in scenery mode?

Procedure:
  1. Connect to scope (currently in solar mode)
  2. Record initial scope_get_equ_coord + tilt/compass
  3. Switch to scenery mode (stops solar tracking!)
  4. Wait 2s for mode transition
  5. Nudge UP for 2 seconds
  6. Wait 1s for mechanical settle
  7. Read scope_get_equ_coord + tilt/compass again
  8. Capture all Event messages received during the nudge
  9. Report: did RA/Dec change? Did tilt/compass change?
  10. Try scope_get_horiz_coord before and after nudge
  11. Return to solar mode (optional, based on --restore-solar flag)

⚠️  WARNING: This script WILL stop solar tracking. Run when you can
    observe the scope physically and tolerate a brief interruption.
    Use --restore-solar to restart solar mode after the test.

Usage:
  python tests/diag_phase4_position_feedback.py --restore-solar
  python tests/diag_phase4_position_feedback.py --nudge-direction up --nudge-sec 2
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

# API angle convention (speed_move adds +180 internally):
#   90=up, 180=right, 270=down, 0=left
NUDGE_ANGLES = {"up": 90, "down": 270, "left": 0, "right": 180}


def _pass(name, detail=""):
    msg = f"  PASS  {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return {"test": name, "status": "PASS", "detail": detail}


def _fail(name, detail=""):
    msg = f"  FAIL  {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return {"test": name, "status": "FAIL", "detail": detail}


def _warn(name, detail=""):
    msg = f"  WARN  {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return {"test": name, "status": "WARN", "detail": detail}


def _info(msg):
    print(f"        {msg}")


def capture_events(client, duration_s: float) -> list:
    """Read raw messages from the socket for duration_s seconds, return events."""
    events = []
    deadline = time.time() + duration_s
    import select

    while time.time() < deadline:
        try:
            with client._socket_lock:
                sock = client.socket
                if sock is None:
                    break
                ready = select.select([sock], [], [], 0.1)[0]
                if not ready:
                    continue
                chunk = sock.recv(4096)
                if chunk:
                    lines = (
                        chunk.decode("utf-8", errors="replace").strip().split("\r\n")
                    )
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            if "Event" in msg or "method" in msg:
                                events.append(msg)
                        except Exception:
                            pass
        except Exception:
            break
    return events


def snapshot(client, label: str) -> dict:
    """Capture current scope state — equ coord, horiz coord, tilt, compass."""
    s = {"label": label, "ts": time.time()}

    # scope_get_equ_coord
    try:
        r = client._send_command("scope_get_equ_coord", quiet=True, timeout_override=5)
        s["equ"] = r
        if r:
            _info(
                f"[{label}] scope_get_equ_coord: RA={r.get('ra')}h  Dec={r.get('dec')}°"
            )
        else:
            _info(f"[{label}] scope_get_equ_coord: no response")
    except Exception as e:
        s["equ"] = None
        _info(f"[{label}] scope_get_equ_coord error: {e}")

    # scope_get_horiz_coord
    try:
        r = client._send_command(
            "scope_get_horiz_coord", quiet=True, timeout_override=5
        )
        s["horiz"] = r
        if r:
            _info(f"[{label}] scope_get_horiz_coord: {r}")
        else:
            _info(f"[{label}] scope_get_horiz_coord: no response")
    except Exception as e:
        s["horiz"] = None
        _info(f"[{label}] scope_get_horiz_coord: not supported ({type(e).__name__})")

    # get_device_state — tilt + compass
    try:
        r = client._send_command("get_device_state", quiet=True, timeout_override=5)
        if r:
            bal = (r.get("balance_sensor") or {}).get("data") or {}
            comp = (r.get("compass_sensor") or {}).get("data") or {}
            s["tilt"] = bal.get("angle")
            s["compass"] = comp.get("direction")
            _info(f"[{label}] tilt={s['tilt']}°  compass={s['compass']}°")
    except Exception as e:
        s["tilt"] = None
        s["compass"] = None
        _info(f"[{label}] device_state error: {e}")

    return s


def run(host, port, nudge_direction, nudge_sec, restore_solar, output_path):
    from src.seestar_client import SeestarClient

    print("=" * 70)
    print("PHASE 4: Position Feedback in Scenery Mode")
    print("=" * 70)
    print(f"Connecting to {host}:{port} …")
    print(f"Nudge: {nudge_direction} for {nudge_sec}s")
    if restore_solar:
        print("Will restore solar mode after test.")
    print()

    client = SeestarClient(host=host, port=port, timeout=10, heartbeat_interval=99)
    results = []
    raw = {}

    # Connect
    try:
        ok = client.connect()
        if not ok:
            raise RuntimeError("connect() returned False")
        results.append(_pass("connect"))
    except Exception as e:
        results.append(_fail("connect", str(e)))
        return results, raw

    time.sleep(0.5)

    # 1. Baseline snapshot (in solar mode)
    print("--- Baseline (solar mode) ---")
    raw["initial_mode"] = client._viewing_mode
    try:
        t = client._send_command("get_view_state", quiet=True, timeout_override=5)
        if t:
            raw["initial_view_state"] = t.get("View") or t
            vm = (t.get("View") or t or {}).get("mode")
            _info(f"Current view mode: {vm}")
            raw["initial_mode"] = vm
    except Exception:
        pass

    raw["snapshot_before"] = snapshot(client, "before")

    # 2. Switch to scenery mode
    print()
    print("--- Switching to scenery mode ---")
    try:
        client.start_scenery_mode()
        time.sleep(2)
        _info("Switched to scenery mode")
        results.append(_pass("switch_to_scenery"))
    except Exception as e:
        results.append(_fail("switch_to_scenery", str(e)))
        try:
            client.disconnect()
        except:
            pass
        return results, raw

    # 3. Snapshot in scenery mode (before nudge)
    print()
    print("--- Scenery mode baseline (before nudge) ---")
    raw["snapshot_scenery_pre"] = snapshot(client, "scenery_pre_nudge")

    # 4. Nudge in chosen direction
    print()
    angle = NUDGE_ANGLES[nudge_direction]
    print(f"--- Nudging {nudge_direction} (angle={angle}°) for {nudge_sec}s ---")
    try:
        # send speed_move directly without the mode-switch logic (already in scenery)
        fw_angle = (angle + 180) % 360
        client._send_command(
            "scope_speed_move",
            params={"speed": 50, "angle": fw_angle, "dur_sec": nudge_sec},
            expect_response=False,
        )
        _info(f"scope_speed_move sent: speed=50 fw_angle={fw_angle}° dur={nudge_sec}s")
        results.append(
            _pass("nudge_sent", f"direction={nudge_direction} fw_angle={fw_angle}°")
        )
    except Exception as e:
        results.append(_fail("nudge_sent", str(e)))

    # 5. Wait for move to complete, then snapshot
    time.sleep(nudge_sec + 1.0)

    print()
    print("--- Post-nudge snapshot ---")
    raw["snapshot_after"] = snapshot(client, "after_nudge")

    # 6. Analysis: did RA/Dec change?
    print()
    print("--- Analysis ---")
    before = raw.get("snapshot_scenery_pre", {})
    after = raw.get("snapshot_after", {})

    # RA/Dec change
    before_equ = before.get("equ") or {}
    after_equ = after.get("equ") or {}

    if before_equ.get("ra") is not None and after_equ.get("ra") is not None:
        delta_ra = abs(float(after_equ["ra"]) - float(before_equ["ra"]))
        delta_dec = abs(
            float(after_equ.get("dec", 0)) - float(before_equ.get("dec", 0))
        )
        raw["delta_ra_h"] = round(delta_ra, 6)
        raw["delta_dec_deg"] = round(delta_dec, 6)
        _info(f"RA change:  {delta_ra:.6f} h ({delta_ra * 15:.4f}°)")
        _info(f"Dec change: {delta_dec:.6f}°")

        if delta_ra * 15 > 0.05 or delta_dec > 0.05:
            results.append(
                _pass(
                    "equ_coord_updates_after_nudge",
                    f"ΔRA={delta_ra*15:.3f}° ΔDec={delta_dec:.3f}° — position feedback WORKS in scenery",
                )
            )
        else:
            results.append(
                _fail(
                    "equ_coord_updates_after_nudge",
                    f"ΔRA={delta_ra*15:.4f}° ΔDec={delta_dec:.4f}° — RA/Dec FROZEN, servo loop will be blind",
                )
            )
    else:
        results.append(
            _warn(
                "equ_coord_updates_after_nudge",
                "could not compare — missing before or after data",
            )
        )

    # Tilt change
    before_tilt = before.get("tilt")
    after_tilt = after.get("tilt")
    if before_tilt is not None and after_tilt is not None:
        delta_tilt = abs(float(after_tilt) - float(before_tilt))
        _info(f"Tilt change: {delta_tilt:.2f}°")
        raw["delta_tilt_deg"] = round(delta_tilt, 3)
        if delta_tilt > 0.2:
            results.append(
                _pass(
                    "tilt_updates_after_nudge",
                    f"Δtilt={delta_tilt:.2f}° — balance sensor responds",
                )
            )
        else:
            results.append(
                _warn(
                    "tilt_updates_after_nudge",
                    f"Δtilt={delta_tilt:.2f}° — may not have moved far enough",
                )
            )
    else:
        results.append(_warn("tilt_updates_after_nudge", "tilt data unavailable"))

    # Compass change
    before_comp = before.get("compass")
    after_comp = after.get("compass")
    if before_comp is not None and after_comp is not None:
        delta_comp = abs(float(after_comp) - float(before_comp))
        delta_comp = min(delta_comp, 360 - delta_comp)
        _info(f"Compass change: {delta_comp:.2f}°")
        raw["delta_compass_deg"] = round(delta_comp, 3)

    # horiz_coord change
    before_horiz = before.get("horiz")
    after_horiz = after.get("horiz")
    if before_horiz and after_horiz:
        _info(f"horiz_coord before: {before_horiz}  after: {after_horiz}")
        results.append(
            _pass("horiz_coord_exists", f"before={before_horiz} after={after_horiz}")
        )
    elif before_horiz is None and after_horiz is None:
        results.append(
            _warn("horiz_coord_exists", "scope_get_horiz_coord not supported")
        )

    # 7. Restore solar mode (optional)
    if restore_solar:
        print()
        print("--- Restoring solar mode ---")
        try:
            client.start_solar_mode()
            time.sleep(1)
            _info("Solar mode restored")
            results.append(_pass("restore_solar"))
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

    # Key finding
    equ_result = next(
        (r for r in results if r["test"] == "equ_coord_updates_after_nudge"), None
    )
    if equ_result:
        print()
        print("KEY FINDING (servo loop viability):")
        print(f"  {equ_result['status']}: {equ_result['detail']}")

    output = {
        "phase": "phase4_position_feedback",
        "host": host,
        "port": port,
        "nudge_direction": nudge_direction,
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
        "--nudge-direction", default="up", choices=["up", "down", "left", "right"]
    )
    parser.add_argument("--nudge-sec", type=int, default=2)
    parser.add_argument(
        "--restore-solar", action="store_true", help="Restart solar mode after test"
    )
    parser.add_argument(
        "--output", default="docs/diag_logs/phase4_position_feedback.json"
    )
    args = parser.parse_args()
    run(
        args.host,
        args.port,
        args.nudge_direction,
        args.nudge_sec,
        args.restore_solar,
        args.output,
    )


if __name__ == "__main__":
    main()
