#!/usr/bin/env python3
"""
Phase 4 Diagnostic: GoTo Servo Loop Validation
===============================================
Tests manual_goto() servo loop by slewing to a safe nearby target.

Strategy: compute Sun's current position, then target 5° higher in altitude
(or a custom alt/az) so the scope moves a small but measurable amount.

Logs every servo iteration: position feedback, computed angle, speed,
delta from target. This lets us see if the loop is converging or diverging.

⚠️  WARNING: Stops solar tracking, enters scenery mode, physically slews.
    Use --restore-solar to restart solar mode after test.

Usage:
  # Slew 5° above current Sun position (safe default):
  python tests/diag_phase4_goto_test.py --restore-solar

  # Explicit target:
  python tests/diag_phase4_goto_test.py --target-alt 50 --target-az 225 --restore-solar
"""
import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("SEESTAR_HOST", "192.168.4.112")
PORT = int(os.getenv("SEESTAR_PORT", "4700"))
OBS_LAT = float(os.getenv("OBSERVER_LATITUDE", "33.111369"))
OBS_LON = float(os.getenv("OBSERVER_LONGITUDE", "-117.310169"))
OBS_ELEV = float(os.getenv("OBSERVER_ELEVATION", "45"))


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


def get_sun_altaz():
    from skyfield.api import wgs84

    from src.constants import ASTRO_EPHEMERIS, EARTH_TIMESCALE

    observer = ASTRO_EPHEMERIS["earth"] + wgs84.latlon(
        OBS_LAT, OBS_LON, elevation_m=OBS_ELEV
    )
    t = EARTH_TIMESCALE.now()
    sun = ASTRO_EPHEMERIS["sun"]
    alt, az, _ = observer.at(t).observe(sun).apparent().altaz()
    return round(alt.degrees, 2), round(az.degrees, 2)


def patched_goto_with_logging(client, target_alt, target_az, max_duration=60):
    """
    Run the GoTo servo loop with verbose per-iteration logging.
    Returns a dict with the iteration trace and final status.
    """
    iterations = []
    start = time.time()
    stall_count = 0
    prev_distance = None
    arrived = False
    final_status = "timeout"

    _TOLERANCE = 0.5
    _SLOW_THRESHOLD = 5.0
    _FAST_SPEED = 80
    _SLOW_SPEED = 20

    while time.time() - start < max_duration:
        t = client.get_telemetry()
        cur_alt = t.get("alt")
        cur_az = t.get("az")
        scope_alt = t.get("scope_alt")
        scope_az = t.get("scope_az")
        tilt = t.get("tilt_angle")
        compass = t.get("compass_direction")

        if cur_alt is None or cur_az is None:
            _info("  No alt/az in telemetry — waiting…")
            time.sleep(1)
            continue

        d_alt = target_alt - cur_alt
        d_az = (target_az - cur_az + 180) % 360 - 180
        distance = math.sqrt(d_alt**2 + d_az**2)
        # Fixed formula (Phase 4): negate d_alt so angle drives scope toward target.
        # atan2(d_az, -d_alt) + 90 → api angle; speed_move adds +180 → fw angle.
        angle = (math.degrees(math.atan2(d_az, -d_alt)) + 90) % 360
        speed = _SLOW_SPEED if distance < _SLOW_THRESHOLD else _FAST_SPEED

        elapsed = round(time.time() - start, 1)
        _info(
            f"  t={elapsed:5.1f}s  pos=({cur_alt:6.2f}°, {cur_az:6.2f}°)  "
            f"delta=({d_alt:+.2f}°, {d_az:+.2f}°)  dist={distance:.2f}°  "
            f"angle={angle:.1f}°  spd={speed}"
        )

        iter_data = {
            "elapsed_s": elapsed,
            "cur_alt": round(cur_alt, 3),
            "cur_az": round(cur_az, 3),
            "scope_alt": scope_alt,
            "scope_az": scope_az,
            "tilt": tilt,
            "compass": compass,
            "d_alt": round(d_alt, 3),
            "d_az": round(d_az, 3),
            "distance": round(distance, 3),
            "angle": round(angle, 1),
            "speed": speed,
        }
        iterations.append(iter_data)

        if distance < _TOLERANCE:
            arrived = True
            final_status = "arrived"
            _info(f"  >>> ARRIVED in {elapsed:.1f}s")
            client.speed_stop()
            break

        # Stall check
        if prev_distance is not None and abs(prev_distance - distance) < 0.05:
            stall_count += 1
            if stall_count >= 5:
                _info("  >>> STALLED — not making progress")
                client.speed_stop()
                final_status = "stalled"
                break
        else:
            stall_count = 0
        prev_distance = distance

        # Send move (uses production speed_move which adds +180 fw offset)
        dur = 2 if distance >= _SLOW_THRESHOLD else 1
        fw_angle = (int(round(angle)) + 180) % 360
        client._send_command(
            "scope_speed_move",
            params={"speed": speed, "angle": fw_angle, "dur_sec": dur},
            expect_response=False,
        )
        time.sleep(dur + 0.2)

    client.speed_stop()
    return {
        "final_status": final_status,
        "arrived": arrived,
        "iterations": iterations,
        "elapsed_s": round(time.time() - start, 1),
    }


def run(host, port, target_alt, target_az, restore_solar, output_path):
    from src.seestar_client import SeestarClient

    print("=" * 70)
    print("PHASE 4: GoTo Servo Loop Validation")
    print("=" * 70)
    print(f"Connecting to {host}:{port} …")

    client = SeestarClient(host=host, port=port, timeout=10, heartbeat_interval=99)
    results = []
    raw = {}

    try:
        ok = client.connect()
        if not ok:
            raise RuntimeError("connect() returned False")
        results.append(_pass("connect"))
    except Exception as e:
        results.append(_fail("connect", str(e)))
        return results, raw

    time.sleep(0.5)

    # If no explicit target, use Sun + 5° altitude
    if target_alt is None or target_az is None:
        try:
            sun_alt, sun_az = get_sun_altaz()
            target_alt = min(sun_alt + 5.0, 85.0)
            target_az = sun_az
            _info(
                f"Sun at alt={sun_alt:.2f}° az={sun_az:.2f}° → target alt={target_alt:.2f}° az={target_az:.2f}°"
            )
        except Exception as e:
            results.append(_fail("get_sun_target", str(e)))
            try:
                client.disconnect()
            except:
                pass
            return results, raw

    raw["target_alt"] = target_alt
    raw["target_az"] = target_az
    _info(f"GoTo target: alt={target_alt:.2f}° az={target_az:.2f}°")
    print()

    # Switch to scenery mode
    print("--- Switching to scenery mode ---")
    try:
        client.stop_view_mode()
        time.sleep(0.5)
        client.start_scenery_mode()
        time.sleep(2)
        results.append(_pass("switch_to_scenery"))
    except Exception as e:
        results.append(_fail("switch_to_scenery", str(e)))
        try:
            client.disconnect()
        except:
            pass
        return results, raw

    # Baseline position
    t = client.get_telemetry()
    raw["start_alt"] = t.get("alt")
    raw["start_az"] = t.get("az")
    _info(f"Starting position: alt={t.get('alt')}° az={t.get('az')}°")
    print()

    # Run patched servo loop
    print("--- GoTo servo loop (verbose) ---")
    try:
        goto_result = patched_goto_with_logging(
            client, target_alt, target_az, max_duration=90
        )
        raw["goto_result"] = goto_result

        status = goto_result["final_status"]
        elapsed = goto_result["elapsed_s"]
        iters = len(goto_result["iterations"])

        if status == "arrived":
            results.append(
                _pass("goto_arrived", f"in {elapsed}s after {iters} iterations")
            )
        elif status == "stalled":
            results.append(
                _fail(
                    "goto_stalled",
                    f"stalled after {iters} iterations — position feedback likely frozen",
                )
            )
        else:
            results.append(_fail("goto_timeout", f"timed out after {elapsed}s"))

        # Analyse: was distance decreasing?
        if goto_result["iterations"]:
            distances = [it["distance"] for it in goto_result["iterations"]]
            first_d = distances[0]
            last_d = distances[-1]
            trend = "converging" if last_d < first_d else "diverging"
            _info(f"Distance trend: {first_d:.2f}° → {last_d:.2f}° ({trend})")
            raw["distance_trend"] = trend
            if trend == "converging":
                results.append(
                    _pass("servo_converging", f"{first_d:.2f}° → {last_d:.2f}°")
                )
            else:
                results.append(
                    _fail(
                        "servo_converging",
                        f"{first_d:.2f}° → {last_d:.2f}° — servo is diverging!",
                    )
                )

    except Exception as e:
        results.append(_fail("goto_loop", str(e)))

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
        "phase": "phase4_goto_test",
        "host": host,
        "port": port,
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
    parser.add_argument("--target-alt", type=float, default=None)
    parser.add_argument("--target-az", type=float, default=None)
    parser.add_argument("--restore-solar", action="store_true")
    parser.add_argument("--output", default="docs/diag_logs/phase4_goto_test.json")
    args = parser.parse_args()
    run(
        args.host,
        args.port,
        args.target_alt,
        args.target_az,
        args.restore_solar,
        args.output,
    )


if __name__ == "__main__":
    main()
