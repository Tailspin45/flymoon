"""
Tests for CelestialObject and get_rise_set_times().
"""

import sys
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from skyfield.api import wgs84

from src.astro import CelestialObject, get_rise_set_times
from src.constants import ASTRO_EPHEMERIS

EARTH = ASTRO_EPHEMERIS["earth"]
OBS_LAT = 37.77
OBS_LON = -122.42
OBS_ELEV = 50.0
MY_POS = EARTH + wgs84.latlon(OBS_LAT, OBS_LON, elevation_m=OBS_ELEV)
REF_TIME = datetime(2024, 6, 21, 18, 0, 0, tzinfo=timezone.utc)


# ── CelestialObject ────────────────────────────────────────────────────────


def test_sun_update_position_sets_altitude():
    """After update_position(), altitude is set and is a reasonable value."""
    sun = CelestialObject("sun", MY_POS)
    sun.update_position(REF_TIME)
    assert sun.altitude is not None
    assert -90 <= sun.altitude.degrees <= 90


def test_sun_update_position_sets_azimuth():
    """After update_position(), azimuth is in [0, 360)."""
    sun = CelestialObject("sun", MY_POS)
    sun.update_position(REF_TIME)
    assert sun.azimuthal is not None
    assert 0 <= sun.azimuthal.degrees < 360


def test_moon_update_position():
    """Moon position is computed without error."""
    moon = CelestialObject("moon", MY_POS)
    moon.update_position(REF_TIME)
    assert moon.altitude is not None
    assert moon.azimuthal is not None


def test_sun_above_horizon_at_noon():
    """San Francisco summer solstice noon UTC — sun should be well above horizon."""
    noon = datetime(2024, 6, 21, 19, 0, 0, tzinfo=timezone.utc)  # ~noon PDT
    sun = CelestialObject("sun", MY_POS)
    sun.update_position(noon)
    assert (
        sun.altitude.degrees > 20
    ), f"Expected sun above horizon, got {sun.altitude.degrees:.1f}°"


def test_sun_below_horizon_at_midnight():
    """SF midnight — sun should be below the horizon."""
    midnight = datetime(2024, 6, 22, 7, 0, 0, tzinfo=timezone.utc)  # midnight PDT
    sun = CelestialObject("sun", MY_POS)
    sun.update_position(midnight)
    assert (
        sun.altitude.degrees < 0
    ), f"Expected sun below horizon, got {sun.altitude.degrees:.1f}°"


def test_position_changes_over_time():
    """Sun position at two different times should differ."""
    sun = CelestialObject("sun", MY_POS)
    sun.update_position(REF_TIME)
    alt1 = sun.altitude.degrees

    later = datetime(2024, 6, 21, 20, 0, 0, tzinfo=timezone.utc)
    sun.update_position(later)
    alt2 = sun.altitude.degrees

    assert alt1 != alt2


def test_get_coordinates_returns_dict():
    sun = CelestialObject("sun", MY_POS)
    sun.update_position(REF_TIME)
    coords = sun.get_coordinates()
    assert "altitude" in coords
    assert "azimuthal" in coords


def test_get_coordinates_precision():
    sun = CelestialObject("sun", MY_POS)
    sun.update_position(REF_TIME)
    coords = sun.get_coordinates(precision=4)
    # Should have at most 4 decimal places
    alt_str = str(coords["altitude"])
    decimals = len(alt_str.split(".")[-1]) if "." in alt_str else 0
    assert decimals <= 4


# ── get_rise_set_times ─────────────────────────────────────────────────────


def test_rise_set_returns_dict():
    result = get_rise_set_times(OBS_LAT, OBS_LON, OBS_ELEV)
    assert isinstance(result, dict)


def test_rise_set_has_sun_and_moon_keys():
    result = get_rise_set_times(OBS_LAT, OBS_LON, OBS_ELEV)
    assert "sun_rise" in result
    assert "sun_set" in result
    assert "moon_rise" in result
    assert "moon_set" in result


def test_rise_set_sun_has_rise_and_set():
    result = get_rise_set_times(OBS_LAT, OBS_LON, OBS_ELEV)
    assert "sun_rise" in result
    assert "sun_set" in result


def test_rise_set_time_format():
    """Rise/set times are HH:MM strings or None (circumpolar / below horizon)."""
    result = get_rise_set_times(OBS_LAT, OBS_LON, OBS_ELEV)
    for key in ("sun_rise", "sun_set", "moon_rise", "moon_set"):
        val = result.get(key)
        if val is not None:
            assert len(val) == 5, f"{key} = {val!r} is not HH:MM"
            assert val[2] == ":", f"{key} = {val!r} missing colon"
