#!/usr/bin/env python3
"""
Phase 5 Diagnostic: Synthetic Transit Injection
================================================
Tests the full prediction pipeline without FlightAware:
  1. Generate a synthetic HIGH-probability flight N minutes before transit
  2. Run get_transits() directly (test_mode=True) — verifies prediction logic
  3. Optionally hit a running app.py instance (/api/transits) — verifies HTTP path
  4. Optionally exercise the TransitMonitor.cached_transits path

Usage:
    python tests/diag_phase5_synthetic_transit.py \
        --observer-lat 33.111369 --observer-lon -117.310169 \
        --target sun --transit-in-minutes 5

    # Against a running app (port 5000 by default):
    python tests/diag_phase5_synthetic_transit.py \
        --observer-lat 33.111369 --observer-lon -117.310169 \
        --target sun --transit-in-minutes 5 \
        --app-url http://localhost:5000
"""

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── helpers ──────────────────────────────────────────────────────────────────


def _sun_position(lat, lon, elev):
    from src.astro import CelestialObject
    from src.constants import ASTRO_EPHEMERIS
    from src.position import get_my_pos

    earth = ASTRO_EPHEMERIS["earth"]
    pos = get_my_pos(lat, lon, elev, earth)
    now = datetime.now(timezone.utc)
    obj = CelestialObject(name="sun", observer_position=pos)
    obj.update_position(now)
    coords = obj.get_coordinates()
    return coords["altitude"], coords["azimuthal"]


def _moon_position(lat, lon, elev):
    from src.astro import CelestialObject
    from src.constants import ASTRO_EPHEMERIS
    from src.position import get_my_pos

    earth = ASTRO_EPHEMERIS["earth"]
    pos = get_my_pos(lat, lon, elev, earth)
    now = datetime.now(timezone.utc)
    obj = CelestialObject(name="moon", observer_position=pos)
    obj.update_position(now)
    coords = obj.get_coordinates()
    return coords["altitude"], coords["azimuthal"]


def build_synthetic_flight(
    obs_lat: float,
    obs_lon: float,
    obs_elev: float,
    target_alt_deg: float,
    target_az_deg: float,
    transit_in_minutes: float = 5.0,
    altitude_ft: int = 35000,
    groundspeed_knots: int = 450,
) -> dict:
    """
    Build a synthetic flight dict that will produce a HIGH-probability transit
    (angular separation < 2°) at `transit_in_minutes` from now.

    Strategy: place the aircraft at the target azimuth, slightly above the
    target altitude, heading perpendicular to the sun/moon azimuth.  The
    aircraft will sweep through the disc in ~5 minutes at cruise speed.
    """
    # Aircraft at target azimuth, slightly above target alt so the angular
    # separation prediction hits ≤1° at closest approach.
    offset_alt_deg = 0.4  # slight positive alt offset (aircraft a bit high)

    obs_elev_ft = obs_elev * 3.28084
    alt_angle_rad = math.radians(max(target_alt_deg + offset_alt_deg, 1.0))
    horiz_dist_ft = (altitude_ft - obs_elev_ft) / math.tan(alt_angle_rad)
    dist_km = horiz_dist_ft * 0.0003048
    dist_deg = dist_km / 111.32

    az_rad = math.radians(target_az_deg)
    lat = obs_lat + dist_deg * math.cos(az_rad)
    lon = obs_lon + dist_deg * math.sin(az_rad) / math.cos(math.radians(obs_lat))

    # Heading perpendicular to azimuth so it sweeps through the disc
    heading = int((target_az_deg + 90) % 360)

    # Propagate position backwards by transit_in_minutes at cruise speed so
    # the aircraft is exactly on a collision course when we run the 0..15 min
    # forward prediction.
    speed_kmh = groundspeed_knots * 1.852
    speed_deg_min = speed_kmh / (111.32 * 60)
    back_dist_deg = speed_deg_min * transit_in_minutes

    head_rad = math.radians(heading)
    lat -= back_dist_deg * math.cos(head_rad)
    lon -= back_dist_deg * math.sin(head_rad) / math.cos(math.radians(obs_lat))

    now = datetime.now(timezone.utc)
    return {
        "ident": "SYN_HIGH",
        "ident_icao": "SYN_HIGH",
        "ident_iata": "SH1",
        "fa_flight_id": f"SYN_HIGH-{int(now.timestamp())}-synthetic",
        "actual_off": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actual_on": "None",
        "origin": {
            "code": "KLAX",
            "code_icao": "KLAX",
            "code_iata": "LAX",
            "name": "Los Angeles International",
            "city": "Los Angeles",
        },
        "destination": {
            "code": "KPHX",
            "code_icao": "KPHX",
            "code_iata": "PHX",
            "name": "Phoenix Sky Harbor",
            "city": "Phoenix",
        },
        "waypoints": [],
        "last_position": {
            "fa_flight_id": f"SYN_HIGH-{int(now.timestamp())}-synthetic",
            "altitude": altitude_ft // 100,
            "altitude_change": "-",
            "groundspeed": groundspeed_knots,
            "heading": heading,
            "latitude": lat,
            "longitude": lon,
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "update_type": "A",
        },
        "aircraft_type": "B738",
    }


def write_test_data_file(flight: dict, path: Path):
    payload = {
        "flights": [flight],
        "links": "None",
        "num_pages": 1,
        "_test_metadata": {
            "scenario": "phase5_synthetic_transit",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def run_prediction(lat, lon, elev, target, test_data_path):
    """Call get_transits() in test mode and return result."""
    # Temporarily swap the TEST_DATA_PATH constant so test_mode uses our file
    import src.transit as _transit_mod
    from src.transit import get_transits

    orig = getattr(_transit_mod, "TEST_DATA_PATH", None)
    _transit_mod.TEST_DATA_PATH = str(test_data_path)

    try:
        result = get_transits(
            latitude=lat,
            longitude=lon,
            elevation=elev,
            target_name=target,
            test_mode=True,
            data_source="hybrid",
            enrich=False,
        )
    finally:
        if orig is not None:
            _transit_mod.TEST_DATA_PATH = orig

    return result


def query_app(app_url: str, timeout: int = 10) -> dict:
    import urllib.request

    url = f"{app_url.rstrip('/')}/api/transits"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"error": str(exc)}


# ── main ─────────────────────────────────────────────────────────────────────


def main():
    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(description="Phase 5: Synthetic transit injection")
    parser.add_argument("--observer-lat", type=float, default=33.111369)
    parser.add_argument("--observer-lon", type=float, default=-117.310169)
    parser.add_argument("--observer-elev", type=float, default=45.0)
    parser.add_argument("--target", choices=["sun", "moon"], default="sun")
    parser.add_argument("--transit-in-minutes", type=float, default=5.0)
    parser.add_argument("--app-url", default=None, help="Running app URL (optional)")
    parser.add_argument(
        "--keep-test-data",
        action="store_true",
        help="Don't delete the temp test data file after run",
    )
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print("Phase 5: Synthetic Transit Injection Test")
    print(f"{'='*60}")
    print(
        f"  Observer : {args.observer_lat}, {args.observer_lon}, {args.observer_elev}m"
    )
    print(f"  Target   : {args.target}")
    print(f"  Transit in: {args.transit_in_minutes} min")

    # ── Step 1: get current target position ───────────────────────────────
    print("\n[1] Fetching current celestial position …")
    try:
        if args.target == "sun":
            t_alt, t_az = _sun_position(
                args.observer_lat, args.observer_lon, args.observer_elev
            )
        else:
            t_alt, t_az = _moon_position(
                args.observer_lat, args.observer_lon, args.observer_elev
            )
        print(f"    {args.target.title()} alt={t_alt:.1f}°  az={t_az:.1f}°")
    except Exception as exc:
        print(f"    ERROR: {exc}")
        sys.exit(1)

    if t_alt < 5:
        print(
            f"    WARNING: {args.target} is only {t_alt:.1f}° above horizon — "
            "prediction window may not cover transit correctly."
        )

    # ── Step 2: build synthetic flight ─────────────────────────────────────
    print("\n[2] Building synthetic HIGH-probability flight …")
    flight = build_synthetic_flight(
        obs_lat=args.observer_lat,
        obs_lon=args.observer_lon,
        obs_elev=args.observer_elev,
        target_alt_deg=t_alt,
        target_az_deg=t_az,
        transit_in_minutes=args.transit_in_minutes,
    )
    lp = flight["last_position"]
    print(f"    Flight   : {flight['ident']}")
    print(f"    Pos      : lat={lp['latitude']:.4f}  lon={lp['longitude']:.4f}")
    print(
        f"    Alt      : {lp['altitude']*100} ft  spd={lp['groundspeed']} kts  hdg={lp['heading']}°"
    )

    # ── Step 3: write test data file ───────────────────────────────────────
    test_data_path = Path("data/raw_flight_data_example.json")
    write_test_data_file(flight, test_data_path)
    print(f"\n[3] Test data written to {test_data_path}")

    # ── Step 4: run prediction pipeline ───────────────────────────────────
    print(f"\n[4] Running prediction pipeline (test_mode=True, target={args.target}) …")
    t0 = time.monotonic()
    try:
        result = run_prediction(
            lat=args.observer_lat,
            lon=args.observer_lon,
            elev=args.observer_elev,
            target=args.target,
            test_data_path=test_data_path,
        )
    except Exception as exc:
        import traceback

        print(f"    ERROR: {exc}")
        traceback.print_exc()
        sys.exit(1)
    elapsed = time.monotonic() - t0

    flights = result.get("flights", [])
    print(f"    Elapsed  : {elapsed*1000:.0f} ms")
    print(f"    Flights returned: {len(flights)}")

    passed = False
    for f in flights:
        lvl = f.get("possibility_level")
        ang = f.get("angular_separation")
        tm = f.get("time")
        fid = f.get("id", "?")
        print(f"    → {fid:20s}  sep={ang:.3f}°  level={lvl}  time={tm:.2f} min")
        if fid == "SYN_HIGH" and lvl == 3:  # PossibilityLevel.HIGH = 3
            passed = True

    if passed:
        print("\n  ✅ PASS: SYN_HIGH flight detected as HIGH probability")
    else:
        if not flights:
            print(
                "\n  ❌ FAIL: No flights returned — check bbox, target altitude, or test data path"
            )
        else:
            print("\n  ❌ FAIL: SYN_HIGH not classified as HIGH (level=3)")

    # ── Step 5: (optional) query running app ──────────────────────────────
    if args.app_url:
        print(f"\n[5] Querying running app at {args.app_url}/api/transits …")
        resp = query_app(args.app_url)
        if "error" in resp:
            print(f"    ERROR: {resp['error']}")
        else:
            api_flights = resp.get("flights", [])
            print(f"    API returned {len(api_flights)} flights")
            any_high = [f for f in api_flights if f.get("possibility_level") == 3]
            print(f"    HIGH-probability: {len(any_high)}")
            for f in any_high:
                print(
                    f"    → {f.get('id','?'):20s}  sep={f.get('angular_separation','?')}°  time={f.get('time','?')} min"
                )
    else:
        print("\n[5] Skipping live app query (no --app-url provided)")

    # ── Cleanup ─────────────────────────────────────────────────────────────
    if not args.keep_test_data:
        pass  # Keep the file — app.py reads it on next test-mode cycle

    print(f"\n{'='*60}")
    print("Phase 5 Synthetic Transit Test complete.")
    print(f"{'='*60}\n")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
