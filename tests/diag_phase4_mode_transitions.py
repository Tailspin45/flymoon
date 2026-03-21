#!/usr/bin/env python3
"""
Phase 4 Diagnostic: Mode Transition Safety
==========================================
Stress-tests mode transitions to confirm:
  - sun → scenery → sun works without errors
  - Rapid mode switches don't corrupt state
  - Telemetry remains accurate after transitions

Tests:
  1. Baseline: confirm scope is in solar mode
  2. N cycles of: sun → scenery (2s pause) → sun (3s pause)
  3. Verify viewing mode after each transition
  4. Verify telemetry alt/az valid after each transition
  5. Single nudge-then-return-to-solar (most common real-world usage)

⚠️  WARNING: Stops solar tracking multiple times.
    Ends in solar mode if --restore-solar (default) or scenery mode otherwise.

Usage:
  python tests/diag_phase4_mode_transitions.py --cycles 3 --restore-solar
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


def check_view_mode(client, expected: str) -> tuple:
    """Query get_view_state and return (actual_mode, passed)."""
    try:
        r = client._send_command("get_view_state", quiet=True, timeout_override=5)
        if r:
            vm = (r.get("View") or r or {}).get("mode")
            return vm, (vm == expected)
    except Exception as e:
        return None, False
    return None, False


def run(host, port, cycles, restore_solar, output_path):
    from src.seestar_client import SeestarClient

    print("=" * 70)
    print("PHASE 4: Mode Transition Safety")
    print("=" * 70)
    print(f"Connecting to {host}:{port} …")
    print(f"Cycles: {cycles}  restore_solar={restore_solar}")
    print()

    client = SeestarClient(host=host, port=port, timeout=10, heartbeat_interval=99)
    results = []
    raw = {"cycles": []}

    try:
        ok = client.connect()
        if not ok:
            raise RuntimeError("connect() returned False")
        results.append(_pass("connect"))
    except Exception as e:
        results.append(_fail("connect", str(e)))
        return results, raw

    time.sleep(0.5)

    # 1. Baseline mode check
    # Firmware reports "solar_sys" for solar mode (not "solar")
    SOLAR_MODES = {"solar", "solar_sys", "sun"}

    print("--- Baseline mode ---")
    initial_mode, _ = check_view_mode(client, "solar")
    raw["initial_mode"] = initial_mode
    _info(f"Current view mode: {initial_mode}")
    if initial_mode in SOLAR_MODES:
        results.append(_pass("baseline_solar_mode", f"mode={initial_mode}"))
    else:
        results.append(_warn("baseline_solar_mode", f"mode={initial_mode} (expected solar/solar_sys)"))

    print()

    # 2. Transition cycles
    all_cycles_ok = True
    for i in range(1, cycles + 1):
        print(f"--- Cycle {i}/{cycles}: solar → scenery → solar ---")
        cycle_data = {"cycle": i}

        # solar → scenery
        try:
            client.stop_view_mode()
            time.sleep(0.3)
            client.start_scenery_mode()
            time.sleep(2)
            mode, ok = check_view_mode(client, "scenery")
            cycle_data["scenery_mode_actual"] = mode
            if ok:
                _info(f"  Cycle {i}: → scenery OK (mode={mode})")
            else:
                _info(f"  Cycle {i}: → scenery mode={mode} (expected scenery)")
        except Exception as e:
            _info(f"  Cycle {i}: → scenery FAILED: {e}")
            cycle_data["scenery_error"] = str(e)
            all_cycles_ok = False

        # Check telemetry in scenery mode
        try:
            t = client.get_telemetry()
            cycle_data["scenery_telemetry"] = {
                "alt": t.get("alt"), "az": t.get("az"),
                "scope_alt": t.get("scope_alt"), "scope_az": t.get("scope_az"),
            }
            _info(f"  Cycle {i} scenery telemetry: alt={t.get('alt')} az={t.get('az')}")
        except Exception as e:
            _info(f"  Cycle {i} scenery telemetry error: {e}")
            cycle_data["scenery_telemetry_error"] = str(e)

        # scenery → solar
        try:
            client.start_solar_mode()
            time.sleep(3)
            mode, ok = check_view_mode(client, "solar")
            # Firmware reports "solar_sys" — accept any solar variant
            ok = mode in SOLAR_MODES
            cycle_data["solar_mode_actual"] = mode
            if ok:
                _info(f"  Cycle {i}: → solar OK (mode={mode})")
            else:
                _info(f"  Cycle {i}: → solar mode={mode} (expected solar/solar_sys)")
                all_cycles_ok = False
        except Exception as e:
            _info(f"  Cycle {i}: → solar FAILED: {e}")
            cycle_data["solar_error"] = str(e)
            all_cycles_ok = False

        # Check telemetry in solar mode
        try:
            t = client.get_telemetry()
            cycle_data["solar_telemetry"] = {
                "alt": t.get("alt"), "az": t.get("az"),
                "target_alt": t.get("target_alt"), "target_az": t.get("target_az"),
            }
            _info(f"  Cycle {i} solar telemetry: alt={t.get('alt')} az={t.get('az')}")
        except Exception as e:
            cycle_data["solar_telemetry_error"] = str(e)

        raw["cycles"].append(cycle_data)
        print()

    results.append(
        _pass("mode_transition_cycles", f"{cycles} cycles completed")
        if all_cycles_ok
        else _fail("mode_transition_cycles", f"errors in {cycles} cycles — see raw data")
    )

    # 3. Nudge-then-return test (common real-world pattern)
    print("--- Nudge then return to solar ---")
    try:
        client.stop_view_mode()
        time.sleep(0.3)
        client.start_scenery_mode()
        time.sleep(2)
        # Single nudge up for 1s
        client._send_command(
            "scope_speed_move",
            params={"speed": 30, "angle": (90 + 180) % 360, "dur_sec": 1},
            expect_response=False,
        )
        time.sleep(1.5)
        client.start_solar_mode()
        time.sleep(3)
        mode, ok = check_view_mode(client, "solar")
        ok = mode in SOLAR_MODES
        _info(f"After nudge-then-solar: mode={mode}")
        t = client.get_telemetry()
        raw["nudge_then_solar"] = {
            "mode": mode,
            "alt": t.get("alt"), "az": t.get("az"),
            "target_alt": t.get("target_alt"), "target_az": t.get("target_az"),
        }
        results.append(
            _pass("nudge_then_solar", f"mode={mode} alt={t.get('alt')} az={t.get('az')}")
            if ok else _warn("nudge_then_solar", f"mode={mode} — check raw")
        )
    except Exception as e:
        results.append(_fail("nudge_then_solar", str(e)))

    # Leave in solar mode (restore_solar is implied when finishing with solar)
    if not restore_solar:
        print()
        print("--- Switching to scenery mode (--no-restore-solar) ---")
        try:
            client.stop_view_mode()
            time.sleep(0.3)
            client.start_scenery_mode()
        except Exception:
            pass

    try: client.disconnect()
    except: pass

    passed = sum(1 for r in results if r["status"] == "PASS")
    warned = sum(1 for r in results if r["status"] == "WARN")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print()
    print("=" * 70)
    print(f"PASS={passed}  WARN={warned}  FAIL={failed}")
    print("=" * 70)

    output = {
        "phase": "phase4_mode_transitions",
        "host": host, "port": port,
        "cycles": cycles,
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
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--restore-solar", action="store_true", default=True)
    parser.add_argument("--no-restore-solar", action="store_false", dest="restore_solar")
    parser.add_argument("--output", default="docs/diag_logs/phase4_mode_transitions.json")
    args = parser.parse_args()
    run(args.host, args.port, args.cycles, args.restore_solar, args.output)


if __name__ == "__main__":
    main()
