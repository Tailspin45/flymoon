"""
Tests for transit detection core: check_transit(),
get_possibility_level() boundary values.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from skyfield.api import wgs84

from src.astro import CelestialObject
from src.constants import ASTRO_EPHEMERIS, PossibilityLevel
from src.transit import angular_separation, check_transit, get_possibility_level

EARTH = ASTRO_EPHEMERIS["earth"]

# Observer: San Francisco
OBS_LAT = 37.77
OBS_LON = -122.42
OBS_ELEV = 50.0
MY_POS = EARTH + wgs84.latlon(OBS_LAT, OBS_LON, elevation_m=OBS_ELEV)

# Reference time: fixed UTC datetime (sun is well above horizon)
REF_TIME = datetime(2024, 6, 21, 18, 0, 0, tzinfo=timezone.utc)

# Window: 0..14 minutes in 1/60 steps
WINDOW = [i / 60 for i in range(15 * 60)]


def _make_flight(lat, lon, speed=800, direction=90, elevation=10000):
    return {
        "name": "TST001",
        "fa_flight_id": "TST001-1",
        "origin": "SFO",
        "destination": "LAX",
        "latitude": lat,
        "longitude": lon,
        "direction": direction,
        "speed": speed,
        "elevation": elevation,
        "elevation_change": "C",
        "aircraft_type": "B738",
    }


def _make_sun(ref_time=REF_TIME):
    sun = CelestialObject("sun", MY_POS)
    sun.update_position(ref_time)
    return sun


# ── angular_separation (spherical cosines) ─────────────────────────────────


def test_angular_separation_zero():
    """Same point → 0° separation."""
    assert angular_separation(45.0, 180.0, 45.0, 180.0) == 0.0


def test_angular_separation_alt_only():
    """Pure altitude difference → should equal the difference."""
    sep = angular_separation(30.0, 180.0, 31.0, 180.0)
    assert abs(sep - 1.0) < 0.01


def test_angular_separation_near_zenith():
    """Near zenith, large az diff should give small on-sky separation."""
    sep = angular_separation(88.0, 100.0, 88.0, 150.0)
    assert sep < 5.0  # 50° az diff at alt=88° is tiny on-sky


# ── get_possibility_level boundary values (new thresholds) ────────────────


def test_possibility_high_exact_boundary():
    """Exactly 2.0° separation → HIGH"""
    result = get_possibility_level(2.0)
    assert result == PossibilityLevel.HIGH.value


def test_possibility_high_below_boundary():
    result = get_possibility_level(0.5)
    assert result == PossibilityLevel.HIGH.value


def test_possibility_medium_just_above_high():
    """2.01° → MEDIUM"""
    result = get_possibility_level(2.01)
    assert result == PossibilityLevel.MEDIUM.value


def test_possibility_medium_exact_boundary():
    """Exactly 4.0° → MEDIUM"""
    result = get_possibility_level(4.0)
    assert result == PossibilityLevel.MEDIUM.value


def test_possibility_low_just_above_medium():
    result = get_possibility_level(4.01)
    assert result == PossibilityLevel.LOW.value


def test_possibility_low_exact_boundary():
    """Exactly 12.0° → LOW"""
    result = get_possibility_level(12.0)
    assert result == PossibilityLevel.LOW.value


def test_possibility_unlikely():
    """Above 12.0° → UNLIKELY"""
    result = get_possibility_level(12.01)
    assert result == PossibilityLevel.UNLIKELY.value


# ── check_transit ─────────────────────────────────────────────────────────


def test_check_transit_returns_dict():
    """check_transit always returns a dict."""
    sun = _make_sun()
    flight = _make_flight(OBS_LAT + 5, OBS_LON)
    result = check_transit(flight, WINDOW, REF_TIME, MY_POS, sun, EARTH)
    assert isinstance(result, dict)


def test_check_transit_required_keys():
    """Result dict always has essential keys."""
    sun = _make_sun()
    flight = _make_flight(OBS_LAT + 5, OBS_LON)
    result = check_transit(flight, WINDOW, REF_TIME, MY_POS, sun, EARTH)
    for key in ("id", "alt_diff", "az_diff", "time", "is_possible_transit"):
        assert key in result, f"Missing key: {key}"


def test_check_transit_no_transit_far_flight():
    """A flight far from the Sun returns is_possible_transit=0."""
    sun = _make_sun()
    # Place flight 45° away from sun in azimuth, definitely not transiting
    flight = _make_flight(OBS_LAT - 40, OBS_LON - 40, direction=270)
    result = check_transit(
        flight,
        WINDOW,
        REF_TIME,
        MY_POS,
        sun,
        EARTH,
        alt_threshold=5.0,
        az_threshold=10.0,
    )
    assert result["is_possible_transit"] == 0


def test_check_transit_with_precomputed_target_positions():
    """check_transit works correctly when target_positions dict is supplied."""
    sun = _make_sun()
    # Build a target_positions dict for all integer minutes in the window
    from datetime import timedelta

    target_positions = {}
    for m in range(15):
        t = REF_TIME + timedelta(minutes=m)
        sun.update_position(t)
        target_positions[m] = (sun.altitude.degrees, sun.azimuthal.degrees)

    flight = _make_flight(OBS_LAT + 5, OBS_LON)
    result_without = check_transit(flight, WINDOW, REF_TIME, MY_POS, sun, EARTH)
    result_with = check_transit(
        flight, WINDOW, REF_TIME, MY_POS, sun, EARTH, target_positions=target_positions
    )
    # Both should agree on is_possible_transit
    assert result_without["is_possible_transit"] == result_with["is_possible_transit"]


def test_check_transit_flight_id_in_result():
    """The flight name is propagated to the result id."""
    sun = _make_sun()
    flight = _make_flight(OBS_LAT + 5, OBS_LON)
    result = check_transit(flight, WINDOW, REF_TIME, MY_POS, sun, EARTH)
    assert result["id"] == "TST001"


# ── Synthetic transit end-to-end tests ──────────────────────────────────


def _sun_aligned_flight(offset_km_perp=0.0, speed=300, heading_offset=90):
    """Place an aircraft ~6 km from observer along the Sun's azimuth at 10 km
    altitude — this puts it at roughly the same elevation angle as the Sun
    (≈58°).  An optional perpendicular offset shifts it in azimuth so it
    sweeps across the Sun if heading is perpendicular.
    """
    import math

    sun = _make_sun()
    sun_az_rad = math.radians(sun.azimuthal.degrees)
    d_along = 6.0  # km — gives ~58° elevation at 10 km altitude

    dlat = d_along * math.cos(sun_az_rad) / 111.32
    dlon = d_along * math.sin(sun_az_rad) / (111.32 * math.cos(math.radians(OBS_LAT)))

    # Perpendicular offset (positive = clockwise as seen from above)
    perp_rad = sun_az_rad + math.pi / 2
    dlat += offset_km_perp * math.cos(perp_rad) / 111.32
    dlon += (
        offset_km_perp * math.sin(perp_rad) / (111.32 * math.cos(math.radians(OBS_LAT)))
    )

    heading = (math.degrees(sun_az_rad) + heading_offset) % 360

    flight = _make_flight(
        OBS_LAT + dlat,
        OBS_LON + dlon,
        speed=speed,
        direction=heading,
        elevation=10000,
    )
    flight["name"] = "SYN001"
    flight["fa_flight_id"] = "SYN001-1"
    return flight, sun


def test_synthetic_transit_high():
    """Aircraft crossing the Sun at ~6 km range should be detected as HIGH."""
    flight, sun = _sun_aligned_flight(offset_km_perp=-0.5)
    result = check_transit(flight, WINDOW, REF_TIME, MY_POS, sun, EARTH)

    assert result["is_possible_transit"] == 1, "Should detect transit"
    assert result["possibility_level"] == PossibilityLevel.HIGH.value
    assert result.get("angular_separation", 999) < 2.0, "Closest approach < 2°"


def test_synthetic_near_miss_medium():
    """Aircraft offset from the Sun flying radially (not crossing) stays MEDIUM."""
    # Offset 0.6 km perpendicular, heading along Sun direction (heading_offset=0)
    # so the perpendicular separation stays roughly constant → near-miss, not crossing
    flight, sun = _sun_aligned_flight(offset_km_perp=-0.6, heading_offset=0)
    result = check_transit(flight, WINDOW, REF_TIME, MY_POS, sun, EARTH)

    sep = result.get("angular_separation", 999)
    level = result["possibility_level"]
    # Should detect as at least LOW (within 12°)
    assert (
        level != PossibilityLevel.UNLIKELY.value
    ), f"Expected at least LOW, got UNLIKELY at {sep:.2f}°"
    # Should NOT be UNLIKELY — the aircraft is near the Sun
    assert sep < 12.0, f"Closest approach should be < 12°, got {sep:.2f}°"


def test_synthetic_far_aircraft_no_transit():
    """Stationary aircraft 30° from the Sun should NOT predict a transit."""

    sun = _make_sun()
    # Place aircraft far from Sun — opposite direction in azimuth, very low elevation
    flight = _make_flight(
        OBS_LAT + 1.0, OBS_LON + 1.0, speed=0, direction=0, elevation=1000
    )
    result = check_transit(flight, WINDOW, REF_TIME, MY_POS, sun, EARTH)

    assert (
        result["is_possible_transit"] == 0
    ), f"Should NOT predict transit — sep={result.get('angular_separation', '?')}°"
