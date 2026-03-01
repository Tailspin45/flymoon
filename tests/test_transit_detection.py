"""
Tests for transit detection core: check_transit(), get_thresholds(),
get_possibility_level() boundary values.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from skyfield.api import wgs84

from src.astro import CelestialObject
from src.constants import ASTRO_EPHEMERIS, PossibilityLevel
from src.transit import (
    check_transit,
    get_possibility_level,
    get_thresholds,
)

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


# ── get_thresholds ─────────────────────────────────────────────────────────


def test_thresholds_low_altitude():
    """Target alt ≤ 15° → (5.0, 10.0)"""
    assert get_thresholds(10) == (5.0, 10.0)


def test_thresholds_medium_altitude():
    """Target alt ≤ 30° → (10.0, 20.0)"""
    assert get_thresholds(25) == (10.0, 20.0)


def test_thresholds_medium_high_altitude():
    """Target alt ≤ 60° → (10.0, 15.0)"""
    assert get_thresholds(45) == (10.0, 15.0)


def test_thresholds_high_altitude():
    """Target alt > 60° → (8.0, 180.0)"""
    assert get_thresholds(80) == (8.0, 180.0)


# ── get_possibility_level boundary values ─────────────────────────────────


def test_possibility_high_exact_boundary():
    """Exactly 1.5° separation → HIGH"""
    # alt_diff=1.5, az_diff=0 at any altitude → sep=1.5
    result = get_possibility_level(45.0, 1.5, 0.0)
    assert result == PossibilityLevel.HIGH.value


def test_possibility_high_below_boundary():
    assert get_possibility_level(45.0, 0.5, 0.5) == PossibilityLevel.HIGH.value


def test_possibility_medium_just_above_high():
    """1.51° → MEDIUM"""
    result = get_possibility_level(45.0, 1.51, 0.0)
    assert result == PossibilityLevel.MEDIUM.value


def test_possibility_medium_exact_boundary():
    """Exactly 2.5° → MEDIUM"""
    result = get_possibility_level(45.0, 2.5, 0.0)
    assert result == PossibilityLevel.MEDIUM.value


def test_possibility_low_just_above_medium():
    result = get_possibility_level(45.0, 2.51, 0.0)
    assert result == PossibilityLevel.LOW.value


def test_possibility_low_exact_boundary():
    """Exactly 3.0° → LOW"""
    result = get_possibility_level(45.0, 3.0, 0.0)
    assert result == PossibilityLevel.LOW.value


def test_possibility_unlikely():
    """Above 3.0° → UNLIKELY"""
    result = get_possibility_level(45.0, 3.01, 0.0)
    assert result == PossibilityLevel.UNLIKELY.value


def test_possibility_cosine_compression_near_zenith():
    """Near zenith (alt=88°) az differences are compressed — 5° az should still be HIGH."""
    # cos(88°) ≈ 0.035, so az_diff=5° contributes only ~0.17° to sep
    result = get_possibility_level(88.0, 0.0, 5.0)
    assert result == PossibilityLevel.HIGH.value


def test_possibility_cosine_no_compression_at_horizon():
    """At low altitude (alt=10°) az_diff of 2° should push past HIGH."""
    # cos(10°) ≈ 0.985, az_diff=2° contributes ~1.97° to sep
    # alt_diff=1.5: sep ≈ sqrt(1.5² + 1.97²) ≈ 2.47 → MEDIUM
    result = get_possibility_level(10.0, 1.5, 2.0)
    assert result == PossibilityLevel.MEDIUM.value


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
