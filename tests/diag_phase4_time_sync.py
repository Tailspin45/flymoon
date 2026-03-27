#!/usr/bin/env python3
"""
Phase 4 Diagnostic: pi_set_time + Clock Sync Validation
========================================================
Tests:
  1. Connect to live Seestar
  2. Send pi_set_time with current UTC — capture full response
  3. Query device_state for any stored clock info
  4. Query scope_get_equ_coord before and after time sync
  5. Compare scope RA/Dec to expected Sun RA/Dec (Skyfield)
  6. Report: is scope time correct? How stale is the position?

Non-destructive — does NOT change viewing mode or send slew commands.

Usage:
  python tests/diag_phase4_time_sync.py
  python tests/diag_phase4_time_sync.py --host 192.168.4.112 --port 4700
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
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


def get_sun_radec():
    """Get current Sun RA/Dec from Skyfield."""
    from skyfield.api import wgs84

    from src.constants import ASTRO_EPHEMERIS, EARTH_TIMESCALE

    observer = ASTRO_EPHEMERIS["earth"] + wgs84.latlon(
        OBS_LAT, OBS_LON, elevation_m=OBS_ELEV
    )
    t = EARTH_TIMESCALE.now()
    sun = ASTRO_EPHEMERIS["sun"]
    ra, dec, _ = observer.at(t).observe(sun).apparent().radec()
    alt, az, _ = observer.at(t).observe(sun).apparent().altaz()
    return {
        "ra_h": ra.hours,
        "dec_deg": dec.degrees,
        "alt": alt.degrees,
        "az": az.degrees,
        "gast": float(t.gast),
    }


def run(host, port, output_path):
    pass


    # Build a minimal direct client to avoid importing the full app
    from src.seestar_client import SeestarClient

    print("=" * 70)
    print("PHASE 4: pi_set_time + Clock Sync Validation")
    print("=" * 70)
    print(f"Connecting to {host}:{port} …")

    client = SeestarClient(host=host, port=port, timeout=10, heartbeat_interval=3)
    results = []
    raw = {}

    # 1. Connect
    try:
        ok = client.connect()
        if not ok:
            raise RuntimeError("connect() returned False")
        _info("Connected successfully")
        results.append(_pass("connect"))
    except Exception as e:
        results.append(_fail("connect", str(e)))
        print("\nCannot continue without connection.")
        return results, raw

    time.sleep(0.5)

    # 2. Query scope_get_equ_coord BEFORE time sync (baseline)
    print()
    print("--- Pre-sync scope_get_equ_coord ---")
    try:
        r = client._send_command("scope_get_equ_coord", quiet=False, timeout_override=5)
        raw["pre_sync_equ"] = r
        _info(f"RA={r.get('ra')}h  Dec={r.get('dec')}°")
        results.append(
            _pass("pre_sync_equ_coord", f"ra={r.get('ra')} dec={r.get('dec')}")
        )
    except Exception as e:
        raw["pre_sync_equ"] = None
        results.append(_fail("pre_sync_equ_coord", str(e)))

    # 3. Send pi_set_time
    print()
    print("--- pi_set_time ---")
    now_utc = datetime.now(timezone.utc)
    try:
        r = client._send_command(
            "pi_set_time",
            params=[
                {
                    "year": now_utc.year,
                    "mon": now_utc.month,
                    "day": now_utc.day,
                    "hour": now_utc.hour,
                    "min": now_utc.minute,
                    "sec": now_utc.second,
                    "time_zone": "UTC",
                }
            ],
            quiet=False,
            timeout_override=8,
        )
        raw["pi_set_time"] = r
        _info(f"Response: {r}")
        if r is not None:
            results.append(_pass("pi_set_time", f"response={r}"))
        else:
            results.append(
                _warn("pi_set_time", "no response (may be normal for this firmware)")
            )
    except Exception as e:
        raw["pi_set_time"] = None
        results.append(_fail("pi_set_time", str(e)))

    time.sleep(1)

    # 4. Query scope_get_equ_coord AFTER time sync
    print()
    print("--- Post-sync scope_get_equ_coord ---")
    try:
        r = client._send_command("scope_get_equ_coord", quiet=False, timeout_override=5)
        raw["post_sync_equ"] = r
        _info(f"RA={r.get('ra')}h  Dec={r.get('dec')}°")
        results.append(
            _pass("post_sync_equ_coord", f"ra={r.get('ra')} dec={r.get('dec')}")
        )
    except Exception as e:
        raw["post_sync_equ"] = None
        results.append(_fail("post_sync_equ_coord", str(e)))

    # 5. Compare scope RA to Sun RA (after sync — should be close if tracking)
    print()
    print("--- RA/Dec vs Skyfield Sun ---")
    try:
        sun = get_sun_radec()
        raw["skyfield_sun"] = sun
        _info(
            f"Skyfield Sun: RA={sun['ra_h']:.4f}h  Dec={sun['dec_deg']:.4f}°  alt={sun['alt']:.2f}° az={sun['az']:.2f}°"
        )

        post = raw.get("post_sync_equ") or raw.get("pre_sync_equ") or {}
        scope_ra = post.get("ra")
        scope_dec = post.get("dec")

        if scope_ra is not None and scope_dec is not None:
            delta_ra_h = abs(float(scope_ra) - sun["ra_h"])
            # Handle wrap
            delta_ra_h = min(delta_ra_h, 24 - delta_ra_h)
            delta_dec = abs(float(scope_dec) - sun["dec_deg"])
            delta_ra_deg = delta_ra_h * 15.0
            _info(f"Scope RA:  {scope_ra}h  (delta from Sun = {delta_ra_deg:.2f}°)")
            _info(f"Scope Dec: {scope_dec}°  (delta from Sun = {delta_dec:.2f}°)")

            raw["delta_ra_deg"] = round(delta_ra_deg, 3)
            raw["delta_dec_deg"] = round(delta_dec, 3)

            # In solar tracking mode, scope should be pointing at the Sun
            if delta_ra_deg < 5 and delta_dec < 5:
                results.append(
                    _pass(
                        "scope_ra_vs_sun",
                        f"Δra={delta_ra_deg:.2f}° Δdec={delta_dec:.2f}° — scope tracking Sun",
                    )
                )
            elif delta_ra_deg < 30:
                results.append(
                    _warn(
                        "scope_ra_vs_sun",
                        f"Δra={delta_ra_deg:.2f}° Δdec={delta_dec:.2f}° — moderate offset (stale?)",
                    )
                )
            else:
                results.append(
                    _fail(
                        "scope_ra_vs_sun",
                        f"Δra={delta_ra_deg:.2f}° Δdec={delta_dec:.2f}° — large offset (scope clock wrong?)",
                    )
                )
        else:
            results.append(_warn("scope_ra_vs_sun", "no scope RA/Dec to compare"))
    except Exception as e:
        results.append(_fail("scope_ra_vs_sun", str(e)))

    # 6. Try scope_get_horiz_coord (may or may not exist)
    print()
    print("--- scope_get_horiz_coord (may not exist) ---")
    try:
        r = client._send_command(
            "scope_get_horiz_coord", quiet=False, timeout_override=5
        )
        raw["horiz_coord"] = r
        if r:
            _info(f"Response: {r}")
            results.append(_pass("scope_get_horiz_coord", f"EXISTS: {r}"))
        else:
            results.append(_warn("scope_get_horiz_coord", "returned empty/None"))
    except Exception as e:
        raw["horiz_coord"] = None
        _info(f"Not supported or error: {e}")
        results.append(_warn("scope_get_horiz_coord", f"not supported: {e}"))

    # 7. Full telemetry snapshot
    print()
    print("--- Full telemetry snapshot ---")
    try:
        t = client.get_telemetry()
        raw["telemetry"] = {k: v for k, v in t.items() if k not in ("view_state",)}
        _info(
            f"alt={t.get('alt')}° az={t.get('az')}°  (scope_alt={t.get('scope_alt')} scope_az={t.get('scope_az')})"
        )
        _info(f"target_alt={t.get('target_alt')}° target_az={t.get('target_az')}°")
        _info(f"view_mode={t.get('view_mode')}  firmware={t.get('firmware_ver')}")
        results.append(
            _pass(
                "telemetry",
                f"view_mode={t.get('view_mode')} firmware={t.get('firmware_ver')}",
            )
        )
    except Exception as e:
        results.append(_fail("telemetry", str(e)))

    # Disconnect gracefully
    try:
        client.disconnect()
    except Exception:
        pass

    # Summary
    passed = sum(1 for r in results if r["status"] == "PASS")
    warned = sum(1 for r in results if r["status"] == "WARN")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print()
    print("=" * 70)
    print(f"PASS={passed}  WARN={warned}  FAIL={failed}")
    print("=" * 70)

    output = {
        "phase": "phase4_time_sync",
        "host": host,
        "port": port,
        "utc_at_run": now_utc.isoformat(),
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
    parser.add_argument("--output", default="docs/diag_logs/phase4_time_sync.json")
    args = parser.parse_args()
    run(args.host, args.port, args.output)


if __name__ == "__main__":
    main()
