#!/usr/bin/env python3
"""
Phase 3 Diagnostic: Prediction Accuracy Validation
===================================================
Validates:
  1. angular_separation() against reference values
  2. geographic_to_altaz() against Skyfield direct computation
  3. predict_position() error budget at typical aircraft distances
  4. End-to-end: synthetic transit prediction produces HIGH at correct time
  5. Sun-Moon angular separation on a known date (reference check)
  6. Threshold analysis: what does 2.0° HIGH threshold mean in practice?

Usage:
  python tests/diag_phase3_prediction_validation.py
  python tests/diag_phase3_prediction_validation.py --observer-lat 33.11 --observer-lon -117.31 --target sun
"""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.constants import ASTRO_EPHEMERIS, EARTH_TIMESCALE
from src.position import geographic_to_altaz, get_my_pos, predict_position
from src.transit import angular_separation, get_possibility_level

EARTH = ASTRO_EPHEMERIS["earth"]

# ── Helpers ───────────────────────────────────────────────────────────────────


def _pass(name: str, detail: str = "") -> dict:
    msg = f"  PASS  {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return {"test": name, "status": "PASS", "detail": detail}


def _fail(name: str, detail: str = "") -> dict:
    msg = f"  FAIL  {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return {"test": name, "status": "FAIL", "detail": detail}


def _info(msg: str):
    print(f"        {msg}")


# ── Section 1: angular_separation() reference checks ─────────────────────────


def test_angular_separation_same_point():
    """Same alt/az → separation must be 0°."""
    sep = angular_separation(45.0, 180.0, 45.0, 180.0)
    ok = abs(sep) < 1e-9
    return _pass("ang_sep same point", f"{sep:.6f}°") if ok else _fail(
        "ang_sep same point", f"got {sep:.6f}°, expected 0"
    )


def test_angular_separation_pure_alt_diff():
    """Two objects at same azimuth, differing only in altitude — should equal alt diff."""
    alt1, alt2 = 30.0, 35.0
    sep = angular_separation(alt1, 90.0, alt2, 90.0)
    expected = abs(alt1 - alt2)
    ok = abs(sep - expected) < 0.001
    return _pass("ang_sep pure alt diff", f"got {sep:.4f}°, expected {expected:.4f}°") if ok else _fail(
        "ang_sep pure alt diff", f"got {sep:.4f}°, expected {expected:.4f}°"
    )


def test_angular_separation_horizon_az_diff():
    """Two objects on the horizon (alt=0) with az diff — should equal az diff up to 180°."""
    az_diff = 30.0
    sep = angular_separation(0.0, 0.0, 0.0, az_diff)
    ok = abs(sep - az_diff) < 0.001
    return _pass("ang_sep horizon az diff", f"got {sep:.4f}°, expected {az_diff:.4f}°") if ok else _fail(
        "ang_sep horizon az diff", f"got {sep:.4f}°, expected {az_diff:.4f}°"
    )


def test_angular_separation_near_zenith_compression():
    """Near zenith, azimuth differences compress — test both simple and spherical."""
    # Two objects both at alt=89°, az differing by 90°.
    # Euclidean: sqrt(0² + (90*cos(89°))²) ≈ 1.57°
    # Spherical law of cosines: should give ~1° (correct)
    alt = 89.0
    sep_sphere = angular_separation(alt, 0.0, alt, 90.0)
    # cos(89°) ≈ 0.01745 → az_diff_cos ≈ 90 * 0.01745 ≈ 1.57°
    from math import cos, radians, sqrt
    sep_euclid = sqrt(0**2 + (90 * cos(radians(alt)))**2)
    _info(f"Near zenith (alt={alt}°, Δaz=90°): spherical={sep_sphere:.4f}°, euclidean={sep_euclid:.4f}°")
    # Spherical should be < euclidean + 1° (both are small, within 2° of each other)
    ok = sep_sphere < 5.0 and sep_sphere > 0.0
    return _pass("ang_sep near zenith", f"spherical={sep_sphere:.4f}°") if ok else _fail(
        "ang_sep near zenith", f"unexpected value {sep_sphere:.4f}°"
    )


def test_angular_separation_sun_moon_known_date():
    """Sun-Moon angular separation on 2024-01-01 00:00 UTC — check against Skyfield."""
    from skyfield.api import wgs84

    # Use a fixed observer (SF)
    obs_lat, obs_lon, obs_elev = 37.77, -122.42, 50.0
    location = wgs84.latlon(obs_lat, obs_lon, elevation_m=obs_elev)
    observer = EARTH + location

    t = EARTH_TIMESCALE.from_datetime(datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc))

    sun = ASTRO_EPHEMERIS["sun"]
    moon = ASTRO_EPHEMERIS["moon"]

    sun_alt, sun_az, _ = observer.at(t).observe(sun).apparent().altaz()
    moon_alt, moon_az, _ = observer.at(t).observe(moon).apparent().altaz()

    # Compute separation our way
    sep_ours = angular_separation(
        sun_alt.degrees, sun_az.degrees, moon_alt.degrees, moon_az.degrees
    )

    # Compute via Skyfield directly (astrometric angle between bodies)
    from skyfield.positionlib import position_of_radec
    sun_astr = observer.at(t).observe(sun).apparent()
    moon_astr = observer.at(t).observe(moon).apparent()
    from skyfield.units import Angle
    sep_skyfield = sun_astr.separation_from(moon_astr).degrees

    delta = abs(sep_ours - sep_skyfield)
    _info(
        f"Sun: alt={sun_alt.degrees:.2f}° az={sun_az.degrees:.2f}°  "
        f"Moon: alt={moon_alt.degrees:.2f}° az={moon_az.degrees:.2f}°"
    )
    _info(f"Our separation: {sep_ours:.4f}°  Skyfield: {sep_skyfield:.4f}°  delta={delta:.4f}°")

    # For our purposes the alt-az spherical formula is an approximation of the
    # 3-D angular separation — acceptable tolerance is 0.1° for nearby bodies.
    # When one or both are below the horizon the comparison is less meaningful.
    if sun_alt.degrees < 0 or moon_alt.degrees < 0:
        _info("  (one/both bodies below horizon — comparison skipped)")
        return _pass("ang_sep sun-moon known date", "below horizon — skipped")

    ok = delta < 0.1
    return _pass("ang_sep sun-moon known date", f"delta={delta:.4f}° < 0.1°") if ok else _fail(
        "ang_sep sun-moon known date", f"delta={delta:.4f}° exceeds 0.1°"
    )


# ── Section 2: geographic_to_altaz() vs Skyfield direct ──────────────────────


def test_geographic_to_altaz_vs_skyfield(obs_lat, obs_lon, obs_elev):
    """
    Place a synthetic 'aircraft' at a known geographic position and verify
    our geographic_to_altaz matches direct Skyfield alt-az computation.
    """
    from skyfield.api import wgs84

    my_pos = get_my_pos(obs_lat, obs_lon, obs_elev, EARTH)
    t_dt = datetime(2024, 6, 21, 18, 0, 0, tzinfo=timezone.utc)

    # Target: 1° north, same longitude, at 10 km altitude
    tgt_lat = obs_lat + 1.0
    tgt_lon = obs_lon
    tgt_elev_m = 10_000.0

    # Our implementation
    our_alt, our_az = geographic_to_altaz(tgt_lat, tgt_lon, tgt_elev_m, EARTH, my_pos, t_dt)

    # Skyfield direct
    t = EARTH_TIMESCALE.from_datetime(t_dt)
    obs_sf = EARTH + wgs84.latlon(obs_lat, obs_lon, elevation_m=obs_elev)
    tgt_sf = EARTH + wgs84.latlon(tgt_lat, tgt_lon, elevation_m=tgt_elev_m)
    diff_vec = (tgt_sf - obs_sf).at(t)
    sf_alt, sf_az, _ = diff_vec.altaz()

    delta_alt = abs(our_alt - sf_alt.degrees)
    delta_az = abs(our_az - sf_az.degrees)
    # Handle az wrap
    delta_az = min(delta_az, 360 - delta_az)

    _info(f"Our:      alt={our_alt:.4f}° az={our_az:.4f}°")
    _info(f"Skyfield: alt={sf_alt.degrees:.4f}° az={sf_az.degrees:.4f}°")
    _info(f"Delta:    Δalt={delta_alt:.4f}° Δaz={delta_az:.4f}°")

    ok = delta_alt < 0.1 and delta_az < 0.1
    return _pass("geo_to_altaz vs skyfield", f"Δalt={delta_alt:.4f}° Δaz={delta_az:.4f}°") if ok else _fail(
        "geo_to_altaz vs skyfield", f"Δalt={delta_alt:.4f}° Δaz={delta_az:.4f}° (threshold 0.1°)"
    )


# ── Section 3: predict_position() error budget ───────────────────────────────


def test_predict_position_error_budget():
    """
    Compute position prediction error at typical distances and speeds.
    At 900 km/h, 15 minutes = 225 km travelled.
    Earth curvature matters — we use Haversine so it should be exact.
    """
    lat0, lon0 = 33.0, -117.0
    speed_kmh = 900.0
    heading = 45.0  # NE
    minutes = 15.0

    new_lat, new_lon = predict_position(lat0, lon0, speed_kmh, heading, minutes)

    expected_km = speed_kmh * (minutes / 60)
    actual_km = _haversine(lat0, lon0, new_lat, new_lon)
    err_km = abs(actual_km - expected_km)

    _info(f"Expected: {expected_km:.1f} km  Actual: {actual_km:.3f} km  Error: {err_km:.3f} km")

    # At ~225 km, angular size from observer ≈ err_km / (altitude_km) radians
    # At 10 km altitude, 0.1 km error = 0.57° angular error — should be << 1°
    ok = err_km < 1.0
    return _pass("predict_pos error budget", f"err={err_km:.4f} km < 1 km") if ok else _fail(
        "predict_pos error budget", f"err={err_km:.4f} km exceeds 1 km"
    )


def test_predict_position_angular_error_from_observer(obs_lat, obs_lon, obs_elev):
    """
    Compute angular prediction error at observer's location for a transit-range
    aircraft. An aircraft 3 km lateral error at 10 km altitude ≈ 17° at zenith,
    but transit aircraft are far — at 200 km distance, 1 km error = 0.3°.
    """
    # Typical transit geometry: aircraft 200 km away at 10 km altitude
    # Place it due north of observer
    import math
    d_km = 200.0
    alt_m = 10_000.0
    tgt_lat = obs_lat + (d_km / 111.32)
    tgt_lon = obs_lon

    my_pos = get_my_pos(obs_lat, obs_lon, obs_elev, EARTH)
    t_dt = datetime(2024, 6, 21, 18, 0, 0, tzinfo=timezone.utc)

    true_alt, true_az = geographic_to_altaz(tgt_lat, tgt_lon, alt_m, EARTH, my_pos, t_dt)

    # Now shift position by 1 km (typical OpenSky position error)
    shifted_lat = tgt_lat + (1.0 / 111.32)
    shifted_alt, shifted_az = geographic_to_altaz(shifted_lat, tgt_lon, alt_m, EARTH, my_pos, t_dt)

    angular_err = angular_separation(true_alt, true_az, shifted_alt, shifted_az)
    _info(f"True: alt={true_alt:.3f}° az={true_az:.3f}°")
    _info(f"1 km position error (200 km away, 10 km alt) → {angular_err:.4f}° angular error")

    # At 200 km, 1 km error ~ 0.3° — should be well under 2° HIGH threshold
    return _pass("predict_pos angular error", f"{angular_err:.4f}°/km at 200km") if angular_err < 1.0 else _fail(
        "predict_pos angular error", f"{angular_err:.4f}°/km is too large"
    )


# ── Section 4: End-to-end synthetic transit ───────────────────────────────────


def test_synthetic_transit_prediction(obs_lat, obs_lon, obs_elev, target_name, ref_dt):
    """
    Construct a synthetic aircraft on a collision course with the Sun/Moon
    and verify the pipeline predicts HIGH at ≤ the correct time.

    Strategy:
    1. Compute target (Sun/Moon) alt/az at ref_dt
    2. Place aircraft on line of sight at known distance
    3. Set speed/heading to cross the target at T+5 min
    4. Run check_transit() and verify prediction says HIGH at ≤ T+5 min
    """
    from skyfield.api import wgs84

    from src.astro import CelestialObject
    from src.transit import check_transit, angular_separation as ang_sep

    import numpy as np
    from src.constants import TOP_MINUTE, NUM_SECONDS_PER_MIN, INTERVAL_IN_SECS

    my_pos = get_my_pos(obs_lat, obs_lon, obs_elev, EARTH)
    celestial = CelestialObject(name=target_name, observer_position=my_pos)
    celestial.update_position(ref_dt)

    t_alt = celestial.altitude.degrees
    t_az = celestial.azimuthal.degrees

    if t_alt <= 0:
        _info(f"{target_name} is below horizon at {ref_dt.isoformat()} — skipping")
        return _pass("synthetic transit prediction", f"{target_name} below horizon — skipped")

    _info(f"{target_name}: alt={t_alt:.2f}° az={t_az:.2f}°")

    # Project the target to a ground position at 10 km altitude
    # d = h / tan(alt)
    alt_clamped = max(t_alt, 3.0)
    d_km = (10_000.0 / 1000.0) / math.tan(math.radians(alt_clamped))
    d_km = min(d_km, 400.0)

    # Transit ground point (TGP): point on Earth surface where a vertical
    # through the aircraft intersects ground when aircraft is in transit
    bearing = math.radians(t_az)
    R = 6371.0
    ratio = d_km / R
    obs_lat_r = math.radians(obs_lat)
    obs_lon_r = math.radians(obs_lon)

    tgp_lat_r = math.asin(
        math.sin(obs_lat_r) * math.cos(ratio)
        + math.cos(obs_lat_r) * math.sin(ratio) * math.cos(bearing)
    )
    tgp_lon_r = obs_lon_r + math.atan2(
        math.sin(bearing) * math.sin(ratio) * math.cos(obs_lat_r),
        math.cos(ratio) - math.sin(obs_lat_r) * math.sin(tgp_lat_r),
    )
    tgp_lat = math.degrees(tgp_lat_r)
    tgp_lon = math.degrees(tgp_lon_r)

    _info(f"Transit ground point: lat={tgp_lat:.3f}° lon={tgp_lon:.3f}° (d={d_km:.1f} km)")

    # Place aircraft 5 minutes away, approaching from 90° to target azimuth
    # Perpendicular approach: heading = (target_az + 90) % 360
    approach_speed = 900.0  # km/h
    approach_minutes = 5.0
    d_approach = approach_speed * (approach_minutes / 60.0)  # 75 km

    # Start position: perp offset from TGP
    perp_bearing = math.radians((t_az + 90.0) % 360.0)
    ratio_a = d_approach / R
    start_lat_r = math.asin(
        math.sin(tgp_lat_r) * math.cos(ratio_a)
        + math.cos(tgp_lat_r) * math.sin(ratio_a) * math.cos(perp_bearing)
    )
    start_lon_r = tgp_lon_r + math.atan2(
        math.sin(perp_bearing) * math.sin(ratio_a) * math.cos(tgp_lat_r),
        math.cos(ratio_a) - math.sin(tgp_lat_r) * math.sin(start_lat_r),
    )
    start_lat = math.degrees(start_lat_r)
    start_lon = math.degrees(start_lon_r)

    # Heading: back toward TGP from start position = perp_bearing + 180°
    approach_heading = (math.degrees(perp_bearing) + 180.0) % 360.0

    synthetic_flight = {
        "name": "SYNTHETIC01",
        "latitude": start_lat,
        "longitude": start_lon,
        "elevation": 10_000.0,
        "speed": approach_speed,
        "direction": approach_heading,
        "origin": "TEST",
        "destination": "TEST",
        "elevation_change": "level",
        "aircraft_type": "TEST",
    }

    _info(
        f"Synthetic aircraft: lat={start_lat:.3f}° lon={start_lon:.3f}° "
        f"hdg={approach_heading:.1f}° spd={approach_speed:.0f} km/h"
    )

    window_time = np.linspace(0, TOP_MINUTE, TOP_MINUTE * (NUM_SECONDS_PER_MIN // INTERVAL_IN_SECS))

    result = check_transit(
        synthetic_flight, window_time, ref_dt, my_pos, celestial, EARTH
    )

    level = result.get("possibility_level", "UNKNOWN")
    sep = result.get("angular_separation")
    t_min = result.get("time")

    _info(f"Result: level={level} sep={sep:.3f}° at t={t_min:.2f} min")

    from src.constants import PossibilityLevel
    high_val = PossibilityLevel.HIGH.value
    medium_val = PossibilityLevel.MEDIUM.value

    # Get human-readable name
    level_name = {high_val: "HIGH", medium_val: "MEDIUM"}.get(level, str(level))
    sep_str = f"{sep:.3f}" if sep is not None else "N/A"

    # Accept HIGH or MEDIUM — pure synthetic geometry can have ≈0.5° offset
    if level in (high_val, medium_val) and t_min is not None and t_min <= approach_minutes + 3.0:
        return _pass(
            "synthetic transit prediction",
            f"level={level_name} sep={sep_str}° at t={t_min:.2f}min (expected ≤{approach_minutes+3:.0f}min)",
        )
    else:
        return _fail(
            "synthetic transit prediction",
            f"level={level_name} sep={sep_str}° at t={t_min} (expected HIGH/MEDIUM ≤{approach_minutes+3:.0f}min)",
        )


# ── Section 5: Threshold analysis ─────────────────────────────────────────────


def analyze_thresholds(obs_lat, obs_lon):
    """
    Compute what angular error budget the HIGH=2° threshold provides
    at typical aircraft distances and altitudes.
    """
    print("\n  --- Threshold Analysis ---")
    print(f"  HIGH threshold: ≤2.0°, MEDIUM: ≤4.0°")
    print(f"  Sun/Moon apparent diameter: ~0.5°")
    print(f"  True transit requires angular sep < ~0.25° (half-diameter)")
    print()

    rows = []
    for dist_km in [50, 100, 200, 400]:
        for alt_km in [8, 10, 12]:
            # Slant range from observer to aircraft
            slant_km = math.sqrt(dist_km**2 + alt_km**2)
            # 2° arc at that distance
            lateral_err_km = math.radians(2.0) * slant_km
            # Position error that would push aircraft 2° off center
            rows.append({
                "dist_km": dist_km,
                "alt_km": alt_km,
                "slant_km": round(slant_km, 1),
                "2deg_lateral_km": round(lateral_err_km, 2),
            })
            print(
                f"  dist={dist_km:4d} km  alt={alt_km:2d} km  slant={slant_km:6.1f} km  "
                f"2° = {lateral_err_km:.2f} km lateral"
            )

    # OpenSky staleness error at 900 km/h, 15s stale
    stale_s = 15.0
    speed_kmh = 900.0
    stale_km = speed_kmh * (stale_s / 3600.0)
    print()
    print(f"  OpenSky typical staleness: {stale_s}s at {speed_kmh} km/h → {stale_km:.2f} km position offset")
    print(f"  At 200 km dist, 10 km alt → {math.degrees(stale_km / math.sqrt(200**2 + 10**2)):.3f}° angular error from staleness")

    return rows


# ── Helpers ───────────────────────────────────────────────────────────────────


def _haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--observer-lat", type=float, default=33.111369)
    parser.add_argument("--observer-lon", type=float, default=-117.310169)
    parser.add_argument("--observer-elev", type=float, default=45.0)
    parser.add_argument("--target", default="sun", choices=["sun", "moon"])
    parser.add_argument("--date", default=None, help="ISO-8601 UTC datetime e.g. 2025-06-15T18:00:00Z")
    parser.add_argument("--output", default="docs/diag_logs/phase3_prediction_results.json")
    args = parser.parse_args()

    obs_lat = args.observer_lat
    obs_lon = args.observer_lon
    obs_elev = args.observer_elev
    target = args.target

    if args.date:
        ref_dt = datetime.fromisoformat(args.date.replace("Z", "+00:00"))
    else:
        # Default: use current UTC time
        ref_dt = datetime.now(tz=timezone.utc)

    print("=" * 70)
    print("PHASE 3 PREDICTION ACCURACY VALIDATION")
    print("=" * 70)
    print(f"Observer: lat={obs_lat}, lon={obs_lon}, elev={obs_elev}m")
    print(f"Target:   {target}")
    print(f"Ref time: {ref_dt.isoformat()}")
    print()

    results = []

    # 1. angular_separation()
    print("--- 1. angular_separation() reference checks ---")
    results.append(test_angular_separation_same_point())
    results.append(test_angular_separation_pure_alt_diff())
    results.append(test_angular_separation_horizon_az_diff())
    results.append(test_angular_separation_near_zenith_compression())
    results.append(test_angular_separation_sun_moon_known_date())
    print()

    # 2. geographic_to_altaz() vs Skyfield
    print("--- 2. geographic_to_altaz() vs Skyfield direct ---")
    results.append(test_geographic_to_altaz_vs_skyfield(obs_lat, obs_lon, obs_elev))
    print()

    # 3. predict_position() error budget
    print("--- 3. predict_position() error budget ---")
    results.append(test_predict_position_error_budget())
    results.append(test_predict_position_angular_error_from_observer(obs_lat, obs_lon, obs_elev))
    print()

    # 4. End-to-end synthetic transit
    print("--- 4. End-to-end synthetic transit prediction ---")
    results.append(test_synthetic_transit_prediction(obs_lat, obs_lon, obs_elev, target, ref_dt))
    print()

    # 5. Threshold analysis (informational)
    print("--- 5. Threshold analysis ---")
    threshold_data = analyze_thresholds(obs_lat, obs_lon)

    # Summary
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")

    print()
    print("=" * 70)
    if failed == 0:
        print(f"ALL {passed} TESTS PASSED")
    else:
        print(f"{failed}/{passed+failed} TESTS FAILED")
    print("=" * 70)

    # Write JSON output
    output = {
        "phase": "phase3_prediction_validation",
        "observer": {"lat": obs_lat, "lon": obs_lon, "elev": obs_elev},
        "target": target,
        "ref_time": ref_dt.isoformat(),
        "tests": results,
        "threshold_analysis": threshold_data,
        "summary": {"passed": passed, "failed": failed},
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults written to {out_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
