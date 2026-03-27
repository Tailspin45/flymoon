#!/usr/bin/env python3
"""
Phase 3 Diagnostic: OpenSky Data Freshness & Rate Budget
=========================================================
Measures:
  1. Position age (staleness) of OpenSky reports in the observer's bbox
  2. Aircraft count per query (for density analysis)
  3. Estimated rate limit usage given MONITOR_INTERVAL
  4. Impact of position staleness on angular prediction error

Usage:
  # Single snapshot (quick check):
  python tests/diag_phase3_opensky_freshness.py

  # Multi-sample over N minutes:
  python tests/diag_phase3_opensky_freshness.py --duration 600

  # With explicit bbox (uses dynamic bbox by default):
  python tests/diag_phase3_opensky_freshness.py --bbox 32 -118 33.5 -117
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from src.constants import ASTRO_EPHEMERIS
from src.opensky import fetch_opensky_positions
from src.position import get_my_pos, transit_corridor_bbox

EARTH = ASTRO_EPHEMERIS["earth"]


def _info(msg: str):
    print(f"  {msg}")


def get_dynamic_bbox(obs_lat, obs_lon, obs_elev, target_name):
    """Compute the current dynamic transit corridor bbox."""

    from src.astro import CelestialObject

    my_pos = get_my_pos(obs_lat, obs_lon, obs_elev, EARTH)
    celestial = CelestialObject(name=target_name, observer_position=my_pos)
    celestial.update_position(datetime.now(tz=timezone.utc))
    t_alt = celestial.altitude.degrees
    t_az = celestial.azimuthal.degrees

    if t_alt <= 0:
        _info(f"{target_name} is below horizon — using fallback bbox from .env")
        return (
            float(os.getenv("LAT_LOWER_LEFT", "32.0")),
            float(os.getenv("LONG_LOWER_LEFT", "-118.0")),
            float(os.getenv("LAT_UPPER_RIGHT", "33.5")),
            float(os.getenv("LONG_UPPER_RIGHT", "-117.0")),
            t_alt,
        )

    bbox = transit_corridor_bbox(obs_lat, obs_lon, t_alt, t_az)
    _info(f"{target_name}: alt={t_alt:.2f}° az={t_az:.2f}°")
    return (
        bbox.lat_lower_left,
        bbox.long_lower_left,
        bbox.lat_upper_right,
        bbox.long_upper_right,
        t_alt,
    )


def snapshot(lat_ll, lon_ll, lat_ur, lon_ur):
    """Fetch one snapshot of OpenSky data and return staleness stats."""
    now = time.time()
    data = fetch_opensky_positions(lat_ll, lon_ll, lat_ur, lon_ur)

    if not data:
        return {
            "ts": now,
            "aircraft_count": 0,
            "ages_s": [],
            "mean_age_s": None,
            "max_age_s": None,
            "min_age_s": None,
            "airborne_count": 0,
        }

    ages = []
    airborne = 0
    for callsign, pos in data.items():
        lc = pos.get("last_contact")
        if lc is not None:
            age = now - lc
            ages.append(age)
        if not pos.get("on_ground", False):
            airborne += 1

    return {
        "ts": now,
        "aircraft_count": len(data),
        "airborne_count": airborne,
        "ages_s": [round(a, 1) for a in ages],
        "mean_age_s": round(sum(ages) / len(ages), 1) if ages else None,
        "max_age_s": round(max(ages), 1) if ages else None,
        "min_age_s": round(min(ages), 1) if ages else None,
        "p95_age_s": (
            round(sorted(ages)[int(len(ages) * 0.95)], 1) if len(ages) >= 2 else None
        ),
    }


def angular_error_from_staleness(
    stale_s: float, speed_kmh: float, dist_km: float, alt_km: float
) -> float:
    """
    Compute angular prediction error (degrees) for an aircraft with position
    `stale_s` seconds old, flying at `speed_kmh` km/h, located `dist_km`
    away horizontally at `alt_km` altitude.
    """
    lateral_err_km = speed_kmh * (stale_s / 3600.0)
    slant_km = math.sqrt(dist_km**2 + alt_km**2)
    return math.degrees(math.atan2(lateral_err_km, slant_km))


def rate_limit_analysis(monitor_interval_min: int):
    """Compute daily/hourly API call budget."""
    calls_per_hour = 60.0 / monitor_interval_min
    calls_per_day = calls_per_hour * 24
    anon_ok = calls_per_day <= 100
    registered_ok = calls_per_day <= 400

    print()
    print("  Rate limit analysis:")
    print(f"  MONITOR_INTERVAL = {monitor_interval_min} min")
    print(f"  Calls/hour: {calls_per_hour:.1f}")
    print(f"  Calls/day:  {calls_per_day:.0f}")
    print(f"  Anonymous (100/day):  {'OK' if anon_ok else 'EXCEEDS LIMIT'}")
    print(f"  Registered (400/day): {'OK' if registered_ok else 'EXCEEDS LIMIT'}")

    has_creds = bool(os.getenv("OPENSKY_CLIENT_ID") or os.getenv("OPENSKY_USERNAME"))
    print(f"  Credentials configured: {'YES' if has_creds else 'NO (anonymous)'}")

    if not anon_ok and not has_creds:
        print("  WARNING: At current interval, anonymous quota will be exceeded.")
        print("  Recommendation: Add OPENSKY_CLIENT_ID + OPENSKY_CLIENT_SECRET to .env")

    return {
        "monitor_interval_min": monitor_interval_min,
        "calls_per_day": calls_per_day,
        "anon_ok": anon_ok,
        "registered_ok": registered_ok,
        "has_credentials": has_creds,
    }


def print_staleness_impact():
    """Print a table of angular error for typical staleness values."""
    print()
    print("  Staleness → angular error (at 200 km distance, 10 km altitude):")
    print(
        f"  {'Staleness':>12}  {'Speed':>10}  {'Lateral err':>12}  {'Angular err':>12}"
    )
    rows = []
    for stale_s in [5, 10, 15, 30, 60]:
        for speed_kmh in [600, 900]:
            ang = angular_error_from_staleness(stale_s, speed_kmh, 200, 10)
            lat_km = speed_kmh * (stale_s / 3600.0)
            print(
                f"  {stale_s:12.0f}s  {speed_kmh:10.0f}  {lat_km:12.3f} km  {ang:12.4f}°"
            )
            rows.append(
                {
                    "stale_s": stale_s,
                    "speed_kmh": speed_kmh,
                    "lateral_err_km": round(lat_km, 3),
                    "angular_err_deg": round(ang, 4),
                }
            )
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--observer-lat",
        type=float,
        default=float(os.getenv("OBSERVER_LATITUDE", "33.111369")),
    )
    parser.add_argument(
        "--observer-lon",
        type=float,
        default=float(os.getenv("OBSERVER_LONGITUDE", "-117.310169")),
    )
    parser.add_argument(
        "--observer-elev",
        type=float,
        default=float(os.getenv("OBSERVER_ELEVATION", "45")),
    )
    parser.add_argument("--target", default="sun", choices=["sun", "moon"])
    parser.add_argument(
        "--bbox", nargs=4, type=float, metavar=("LAT_LL", "LON_LL", "LAT_UR", "LON_UR")
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Sampling duration in seconds (0=single snapshot)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Sampling interval in seconds (for --duration)",
    )
    parser.add_argument(
        "--output", default="docs/diag_logs/phase3_opensky_freshness.json"
    )
    args = parser.parse_args()

    obs_lat = args.observer_lat
    obs_lon = args.observer_lon
    obs_elev = args.observer_elev

    print("=" * 70)
    print("PHASE 3 OPENSKY FRESHNESS & RATE BUDGET ANALYSIS")
    print("=" * 70)
    print(f"Observer: lat={obs_lat}, lon={obs_lon}, elev={obs_elev}m")
    print()

    # Determine bbox
    if args.bbox:
        lat_ll, lon_ll, lat_ur, lon_ur = args.bbox
        target_alt = None
        _info(f"Using explicit bbox: ({lat_ll},{lon_ll}) → ({lat_ur},{lon_ur})")
    else:
        lat_ll, lon_ll, lat_ur, lon_ur, target_alt = get_dynamic_bbox(
            obs_lat, obs_lon, obs_elev, args.target
        )
        _info(
            f"Dynamic bbox: ({lat_ll:.2f},{lon_ll:.2f}) → ({lat_ur:.2f},{lon_ur:.2f})  "
            f"target_alt={target_alt:.1f}°"
        )

    bbox_area_approx = (
        (lat_ur - lat_ll)
        * (lon_ur - lon_ll)
        * 111.32**2
        * math.cos(math.radians(obs_lat))
    )
    _info(f"Bbox area: ~{bbox_area_approx:,.0f} km²")
    print()

    # Rate limit analysis
    monitor_interval = int(os.getenv("MONITOR_INTERVAL", "15"))
    rate_data = rate_limit_analysis(monitor_interval)
    print()

    # Staleness impact table
    print("  --- Staleness impact analysis ---")
    staleness_rows = print_staleness_impact()
    print()

    # Snapshot(s)
    snapshots = []
    if args.duration == 0:
        print("  --- Single snapshot ---")
        # Clear the opensky cache to force a live fetch
        import src.opensky as osky

        osky._cache.clear()
        snap = snapshot(lat_ll, lon_ll, lat_ur, lon_ur)
        snapshots.append(snap)
        dt_str = datetime.fromtimestamp(snap["ts"], tz=timezone.utc).strftime(
            "%H:%M:%S UTC"
        )
        _info(f"Time: {dt_str}")
        _info(
            f"Aircraft in bbox: {snap['aircraft_count']} total, {snap['airborne_count']} airborne"
        )
        if snap["ages_s"]:
            _info(
                f"Position age — min={snap['min_age_s']}s  mean={snap['mean_age_s']}s  max={snap['max_age_s']}s  p95={snap['p95_age_s']}s"
            )
            # Assess: is typical staleness acceptable?
            typical_stale = snap["mean_age_s"] or 0
            ang_err = angular_error_from_staleness(typical_stale, 900, 200, 10)
            _info(
                f"Typical staleness angular error (200km, 10km alt, 900km/h): {ang_err:.4f}°"
            )
            if ang_err < 1.0:
                print(
                    f"  PASS  OpenSky staleness acceptable — {ang_err:.4f}° < 1.0° HIGH threshold margin"
                )
            else:
                print(
                    f"  WARN  OpenSky staleness may cause misses — {ang_err:.4f}° is significant vs 2.0° HIGH threshold"
                )
        else:
            _info("No position age data available (no aircraft or OpenSky unavailable)")
    else:
        # Multi-sample
        print(f"  --- Multi-sample ({args.duration}s, every {args.interval}s) ---")
        end_time = time.time() + args.duration
        sample_count = 0
        import src.opensky as osky

        while time.time() < end_time:
            osky._cache.clear()
            snap = snapshot(lat_ll, lon_ll, lat_ur, lon_ur)
            snapshots.append(snap)
            sample_count += 1
            dt_str = datetime.fromtimestamp(snap["ts"], tz=timezone.utc).strftime(
                "%H:%M:%S UTC"
            )
            ages_str = (
                f"mean={snap['mean_age_s']}s max={snap['max_age_s']}s"
                if snap["mean_age_s"] is not None
                else "no data"
            )
            _info(
                f"[{sample_count}] {dt_str}  n={snap['airborne_count']} airborne  ages: {ages_str}"
            )

            remaining = end_time - time.time()
            if remaining > 0:
                time.sleep(min(args.interval, remaining))

        # Aggregate
        all_ages = []
        for s in snapshots:
            all_ages.extend(s["ages_s"])
        if all_ages:
            all_ages.sort()
            print()
            _info(f"Aggregate over {sample_count} samples:")
            _info(f"  Total positions: {len(all_ages)}")
            _info(f"  Min age: {min(all_ages):.1f}s")
            _info(f"  Mean age: {sum(all_ages)/len(all_ages):.1f}s")
            _info(f"  Max age: {max(all_ages):.1f}s")
            _info(f"  P95 age: {all_ages[int(len(all_ages)*0.95)]:.1f}s")

    # Write results
    output = {
        "phase": "phase3_opensky_freshness",
        "observer": {"lat": obs_lat, "lon": obs_lon, "elev": obs_elev},
        "bbox": {
            "lat_ll": lat_ll,
            "lon_ll": lon_ll,
            "lat_ur": lat_ur,
            "lon_ur": lon_ur,
        },
        "rate_analysis": rate_data,
        "staleness_impact": staleness_rows,
        "snapshots": snapshots,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults written to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
