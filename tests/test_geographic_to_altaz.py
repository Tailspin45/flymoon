"""
Tests for geographic_to_altaz() coordinate transform.
Verifies that known observer/target geometry produces correct alt-az results.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))


from src.constants import ASTRO_EPHEMERIS
from src.position import geographic_to_altaz, get_my_pos

EARTH = ASTRO_EPHEMERIS["earth"]

OBS_LAT = 37.77
OBS_LON = -122.42
OBS_ELEV = 50.0
MY_POS = get_my_pos(OBS_LAT, OBS_LON, OBS_ELEV, EARTH)
REF_TIME = datetime(2024, 6, 21, 18, 0, 0, tzinfo=timezone.utc)


def test_returns_two_floats():
    """geographic_to_altaz returns a (float, float) tuple."""
    alt, az = geographic_to_altaz(OBS_LAT + 1, OBS_LON, 10000, EARTH, MY_POS, REF_TIME)
    assert isinstance(alt, float)
    assert isinstance(az, float)


def test_azimuth_in_range():
    """Azimuth is always in [0, 360)."""
    alt, az = geographic_to_altaz(OBS_LAT + 1, OBS_LON, 10000, EARTH, MY_POS, REF_TIME)
    assert 0.0 <= az < 360.0


def test_altitude_range():
    """Altitude is in [-90, 90]."""
    alt, az = geographic_to_altaz(OBS_LAT + 1, OBS_LON, 10000, EARTH, MY_POS, REF_TIME)
    assert -90.0 <= alt <= 90.0


def test_directly_overhead_high_altitude():
    """An object at the observer's exact lat/lon but very high elevation
    should appear close to zenith (altitude ~90°)."""
    alt, az = geographic_to_altaz(OBS_LAT, OBS_LON, 500_000, EARTH, MY_POS, REF_TIME)
    assert alt > 80.0, f"Expected near-zenith but got alt={alt:.1f}°"


def test_north_object_has_northerly_azimuth():
    """An object due north of the observer should have azimuth near 0° (or 360°)."""
    alt, az = geographic_to_altaz(OBS_LAT + 5, OBS_LON, 10000, EARTH, MY_POS, REF_TIME)
    # Allow for projection effects — should be in northern semicircle (270–360 or 0–90)
    assert az < 90 or az > 270, f"Expected northerly az but got {az:.1f}°"


def test_south_object_has_southerly_azimuth():
    """An object due south should have azimuth near 180°."""
    alt, az = geographic_to_altaz(OBS_LAT - 5, OBS_LON, 10000, EARTH, MY_POS, REF_TIME)
    assert 90 < az < 270, f"Expected southerly az but got {az:.1f}°"


def test_east_object_has_easterly_azimuth():
    """An object due east should have azimuth near 90°."""
    alt, az = geographic_to_altaz(OBS_LAT, OBS_LON + 5, 10000, EARTH, MY_POS, REF_TIME)
    assert 45 < az < 135, f"Expected easterly az but got {az:.1f}°"


def test_west_object_has_westerly_azimuth():
    """An object due west should have azimuth near 270°."""
    alt, az = geographic_to_altaz(OBS_LAT, OBS_LON - 5, 10000, EARTH, MY_POS, REF_TIME)
    assert 225 < az < 315, f"Expected westerly az but got {az:.1f}°"


def test_higher_elevation_means_higher_altitude():
    """Same ground position but higher elevation → higher apparent altitude."""
    alt_low, _ = geographic_to_altaz(
        OBS_LAT + 1, OBS_LON, 1_000, EARTH, MY_POS, REF_TIME
    )
    alt_high, _ = geographic_to_altaz(
        OBS_LAT + 1, OBS_LON, 100_000, EARTH, MY_POS, REF_TIME
    )
    assert alt_high > alt_low


def test_deterministic():
    """Same inputs always give same outputs."""
    r1 = geographic_to_altaz(OBS_LAT + 2, OBS_LON + 1, 10000, EARTH, MY_POS, REF_TIME)
    r2 = geographic_to_altaz(OBS_LAT + 2, OBS_LON + 1, 10000, EARTH, MY_POS, REF_TIME)
    assert r1 == r2
