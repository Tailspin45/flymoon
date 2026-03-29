"""Roundtrip: goto_altaz spherical forward vs _altaz_from_equatorial_for_goto inverse."""

import math

from src.constants import EARTH_TIMESCALE
from src.seestar_client import _altaz_from_equatorial_for_goto


def _forward_altaz_to_radec(alt, az, lat, lon, gast_hours):
    lat_r = math.radians(lat)
    alt_r = math.radians(alt)
    az_r = math.radians(az)
    sin_dec = math.sin(alt_r) * math.sin(lat_r) + math.cos(alt_r) * math.cos(
        lat_r
    ) * math.cos(az_r)
    dec_r = math.asin(max(-1.0, min(1.0, sin_dec)))
    cos_ha = (math.sin(alt_r) - math.sin(lat_r) * math.sin(dec_r)) / (
        math.cos(lat_r) * math.cos(dec_r) + 1e-12
    )
    ha_r = math.acos(max(-1.0, min(1.0, cos_ha)))
    if math.sin(az_r) > 0:
        ha_r = 2 * math.pi - ha_r
    lst = (gast_hours + lon / 15.0) % 24
    ra_h = (lst - ha_r * 12 / math.pi) % 24
    dec_d = math.degrees(dec_r)
    return ra_h, dec_d


def test_roundtrip_south_and_cardinals():
    t = EARTH_TIMESCALE.now()
    g = float(t.gast)
    lat, lon = 33.0, -117.0
    for alt, az in [(45.0, 180.0), (30.0, 0.5), (60.0, 270.0), (10.0, 90.0)]:
        ra, dec = _forward_altaz_to_radec(alt, az, lat, lon, g)
        a2, z2 = _altaz_from_equatorial_for_goto(ra, dec, lat, lon, g)
        assert abs(a2 - alt) < 0.02, (alt, az, a2)
        daz = abs((z2 - az + 180) % 360 - 180)
        assert daz < 0.02, (alt, az, z2)
