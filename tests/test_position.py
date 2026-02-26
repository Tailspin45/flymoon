#!/usr/bin/env python3
"""
Unit tests for src/position.py — pure-math functions with no external API deps.

Tests:
  - predict_position()        Haversine dead-reckoning
  - compute_track_velocity()  Speed + heading from track fixes
  - transit_corridor_bbox()   Dynamic bounding box geometry
"""
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.position import predict_position, compute_track_velocity, transit_corridor_bbox


# ── helpers ──────────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Forward bearing in degrees (0=north, clockwise)."""
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dλ = math.radians(lon2 - lon1)
    x = math.sin(dλ) * math.cos(φ2)
    y = math.cos(φ1)*math.sin(φ2) - math.sin(φ1)*math.cos(φ2)*math.cos(dλ)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ── tests ─────────────────────────────────────────────────────────────────────

def test_predict_position_due_north():
    """Aircraft heading due north at 900 km/h for 10 min → ~150 km north."""
    lat0, lon0 = 33.11, -117.31
    speed_kmh = 900.0
    heading = 0.0   # due north
    minutes = 10.0

    new_lat, new_lon = predict_position(lat0, lon0, speed_kmh, heading, minutes)

    expected_km = speed_kmh * (minutes / 60)   # 150 km
    actual_km = haversine_km(lat0, lon0, new_lat, new_lon)
    actual_bearing = bearing_deg(lat0, lon0, new_lat, new_lon)

    assert abs(actual_km - expected_km) < 0.5, \
        f"Distance should be ~{expected_km} km, got {actual_km:.2f} km"
    assert abs(actual_bearing - 0.0) < 0.1 or abs(actual_bearing - 360.0) < 0.1, \
        f"Bearing should be ~0° (north), got {actual_bearing:.2f}°"
    print(f"✓ Due north 900 km/h × 10 min → {actual_km:.2f} km north")


def test_predict_position_due_east():
    """Aircraft heading due east at 600 km/h for 60 min → ~600 km east."""
    lat0, lon0 = 0.0, 0.0   # equator for clean geometry
    speed_kmh = 600.0
    heading = 90.0  # due east
    minutes = 60.0

    new_lat, new_lon = predict_position(lat0, lon0, speed_kmh, heading, minutes)

    expected_km = 600.0
    actual_km = haversine_km(lat0, lon0, new_lat, new_lon)
    actual_bearing = bearing_deg(lat0, lon0, new_lat, new_lon)

    assert abs(actual_km - expected_km) < 1.0, \
        f"Distance should be ~600 km, got {actual_km:.2f} km"
    assert abs(actual_bearing - 90.0) < 0.5, \
        f"Bearing should be ~90° (east), got {actual_bearing:.2f}°"
    print(f"✓ Due east 600 km/h × 60 min → {actual_km:.2f} km east")


def test_predict_position_zero_time():
    """Zero elapsed time returns same position."""
    lat0, lon0 = 51.5, -0.1
    new_lat, new_lon = predict_position(lat0, lon0, 900.0, 45.0, 0.0)
    assert abs(new_lat - lat0) < 1e-9 and abs(new_lon - lon0) < 1e-9, \
        "Zero-time prediction should return original position"
    print("✓ Zero elapsed time → same position")


def test_predict_position_zero_speed():
    """Zero speed returns same position regardless of heading or time."""
    lat0, lon0 = 20.0, 30.0
    new_lat, new_lon = predict_position(lat0, lon0, 0.0, 270.0, 15.0)
    assert abs(new_lat - lat0) < 1e-9 and abs(new_lon - lon0) < 1e-9, \
        "Zero-speed prediction should return original position"
    print("✓ Zero speed → same position")


def test_compute_track_velocity_simple():
    """Two fixes 60 s apart, 1° north → ~111 km/h, heading ~0°."""
    now = datetime.now(timezone.utc).timestamp()
    lat0, lon0 = 33.0, -117.0
    lat1 = lat0 + 1.0 / 111.32  # ~1 km north

    track = [
        {"timestamp": now,       "latitude": lat0, "longitude": lon0},
        {"timestamp": now + 60,  "latitude": lat1, "longitude": lon0},
    ]
    result = compute_track_velocity(track)
    assert result is not None, "Expected a (speed, heading) tuple"
    speed, heading = result

    expected_speed = haversine_km(lat0, lon0, lat1, lon0) / (60/3600)
    assert abs(speed - expected_speed) < 1.0, \
        f"Speed should be ~{expected_speed:.1f} km/h, got {speed:.1f}"
    assert abs(heading) < 1.0 or abs(heading - 360) < 1.0, \
        f"Heading should be ~0° (north), got {heading:.1f}°"
    print(f"✓ 1 km north in 60 s → speed={speed:.1f} km/h, heading={heading:.1f}°")


def test_compute_track_velocity_insufficient_data():
    """Single fix returns None."""
    track = [{"timestamp": 1000.0, "latitude": 33.0, "longitude": -117.0}]
    assert compute_track_velocity(track) is None, \
        "Single fix should return None"
    print("✓ Single track fix → None")


def test_compute_track_velocity_same_timestamps():
    """Two fixes with identical timestamps returns None (dt=0)."""
    track = [
        {"timestamp": 1000.0, "latitude": 33.0, "longitude": -117.0},
        {"timestamp": 1000.0, "latitude": 33.1, "longitude": -117.0},
    ]
    assert compute_track_velocity(track) is None, \
        "Zero dt should return None"
    print("✓ Zero time delta → None")


def test_transit_corridor_bbox_contains_ground_point():
    """The dynamic bbox must contain the transit ground point."""
    obs_lat, obs_lon = 33.11, -117.31
    target_alt, target_az = 45.0, 180.0  # directly south

    # Ground point lies due south at distance h/tan(alt)
    h_km = 10.0
    d_km = h_km / math.tan(math.radians(target_alt))
    d_deg = d_km / 111.32
    gp_lat = obs_lat - d_deg   # south
    gp_lon = obs_lon

    bbox = transit_corridor_bbox(obs_lat, obs_lon, target_alt, target_az)

    assert bbox.lat_lower_left  <= gp_lat <= bbox.lat_upper_right,  \
        f"Ground point lat {gp_lat:.3f} not in bbox lat [{bbox.lat_lower_left:.3f}, {bbox.lat_upper_right:.3f}]"
    assert bbox.long_lower_left <= gp_lon <= bbox.long_upper_right, \
        f"Ground point lon {gp_lon:.3f} not in bbox lon [{bbox.long_lower_left:.3f}, {bbox.long_upper_right:.3f}]"
    print(f"✓ Ground point ({gp_lat:.3f}, {gp_lon:.3f}) is inside bbox")


def test_transit_corridor_bbox_wider_at_low_altitude():
    """Lower target altitude → ground point farther away → wider bbox."""
    obs_lat, obs_lon = 33.11, -117.31
    bbox_low  = transit_corridor_bbox(obs_lat, obs_lon, 10.0, 180.0)
    bbox_high = transit_corridor_bbox(obs_lat, obs_lon, 60.0, 180.0)

    width_low  = bbox_low.long_upper_right  - bbox_low.long_lower_left
    width_high = bbox_high.long_upper_right - bbox_high.long_lower_left

    assert width_low > width_high, \
        f"Low-altitude bbox ({width_low:.2f}°) should be wider than high ({width_high:.2f}°)"
    print(f"✓ Bbox width: low alt={width_low:.2f}°, high alt={width_high:.2f}° — wider when lower")


def test_transit_corridor_bbox_is_valid():
    """Bbox lower-left must be south-west of upper-right."""
    bbox = transit_corridor_bbox(33.11, -117.31, 30.0, 200.0)
    assert bbox.lat_lower_left  < bbox.lat_upper_right,  "lat_ll < lat_ur"
    assert bbox.long_lower_left < bbox.long_upper_right, "lon_ll < lon_ur"
    print(f"✓ Bbox is geometrically valid: "
          f"({bbox.lat_lower_left:.2f},{bbox.long_lower_left:.2f}) → "
          f"({bbox.lat_upper_right:.2f},{bbox.long_upper_right:.2f})")


# ── runner ────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_predict_position_due_north,
        test_predict_position_due_east,
        test_predict_position_zero_time,
        test_predict_position_zero_speed,
        test_compute_track_velocity_simple,
        test_compute_track_velocity_insufficient_data,
        test_compute_track_velocity_same_timestamps,
        test_transit_corridor_bbox_contains_ground_point,
        test_transit_corridor_bbox_wider_at_low_altitude,
        test_transit_corridor_bbox_is_valid,
    ]

    print("=" * 70)
    print("POSITION MODULE UNIT TESTS")
    print("=" * 70)
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"✗ FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ ERROR {t.__name__}: {e}")
            failed += 1

    print()
    print("=" * 70)
    if failed == 0:
        print(f"✓ ALL {passed} TESTS PASSED")
        return 0
    else:
        print(f"✗ {failed}/{passed+failed} TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
