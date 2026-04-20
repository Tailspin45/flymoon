"""Skyfield-based alt/az -> RA/Dec round-trip tests.

Runs the new conversion path in src/alpaca_client.AlpacaClient._altaz_to_radec
and compares the result to Skyfield's direct ephemeris on five representative
targets. A round-trip error under 0.05 degrees is required for each case.

Run:
    python3 -m unittest tests.test_altaz_skyfield_conversion

Requires: skyfield (already a project dependency), de421.bsp accessible
to ``src.constants``.
"""

import math
import os
import unittest

from src.alpaca_client import AlpacaClient
from src.constants import ASTRO_EPHEMERIS, EARTH_TIMESCALE

try:
    from skyfield.api import wgs84
except ImportError:  # pragma: no cover - environmental
    wgs84 = None  # type: ignore


def _angular_sep_deg(ra1_h: float, dec1_d: float, ra2_h: float, dec2_d: float) -> float:
    """Great-circle separation between two RA/Dec pairs, in degrees."""
    ra1 = math.radians(ra1_h * 15.0)
    ra2 = math.radians(ra2_h * 15.0)
    d1 = math.radians(dec1_d)
    d2 = math.radians(dec2_d)
    cos_s = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(
        ra1 - ra2
    )
    cos_s = max(-1.0, min(1.0, cos_s))
    return math.degrees(math.acos(cos_s))


@unittest.skipIf(wgs84 is None, "skyfield not installed")
class AltAzSkyfieldConversionTests(unittest.TestCase):
    TOLERANCE_DEG = 0.05

    @classmethod
    def setUpClass(cls):
        # Observer from env if present; otherwise a San Diego default matching
        # the diag_phase5 scripts so results are reproducible.
        cls.lat = float(os.getenv("OBSERVER_LATITUDE", "33.111369"))
        cls.lon = float(os.getenv("OBSERVER_LONGITUDE", "-117.310169"))
        cls.elev = float(os.getenv("OBSERVER_ELEVATION", "20"))
        cls.t = EARTH_TIMESCALE.now()
        cls.topos = wgs84.latlon(cls.lat, cls.lon, elevation_m=cls.elev)
        cls.observer = ASTRO_EPHEMERIS["earth"] + cls.topos

    def _observe(self, target_name: str):
        """Return apparent (alt_deg, az_deg, ra_h, dec_deg) for a body name."""
        body = ASTRO_EPHEMERIS[target_name]
        astrometric = self.observer.at(self.t).observe(body)
        apparent = astrometric.apparent()
        alt, az, _ = apparent.altaz()
        ra, dec, _ = apparent.radec(epoch="date")
        return alt.degrees, az.degrees, ra.hours, dec.degrees

    def _check_roundtrip(self, label: str, alt_deg: float, az_deg: float,
                         ra_h: float, dec_deg: float):
        ra_conv, dec_conv = AlpacaClient._altaz_to_radec(
            alt_deg, az_deg, self.lat, self.lon
        )
        sep = _angular_sep_deg(ra_conv, dec_conv, ra_h, dec_deg)
        self.assertLess(
            sep,
            self.TOLERANCE_DEG,
            msg=(
                f"{label}: round-trip error {sep:.4f}° >= {self.TOLERANCE_DEG}° "
                f"(alt={alt_deg:.3f} az={az_deg:.3f} -> RA={ra_conv:.4f}h "
                f"Dec={dec_conv:.4f}° vs expected RA={ra_h:.4f}h Dec={dec_deg:.4f}°)"
            ),
        )

    def test_sun(self):
        alt, az, ra, dec = self._observe("sun")
        if alt < 1.0:
            self.skipTest(f"Sun below horizon (alt={alt:.2f})")
        self._check_roundtrip("sun", alt, az, ra, dec)

    def test_moon(self):
        alt, az, ra, dec = self._observe("moon")
        if alt < 1.0:
            self.skipTest(f"Moon below horizon (alt={alt:.2f})")
        self._check_roundtrip("moon", alt, az, ra, dec)

    def test_polaris(self):
        # Polaris is a star, not in de421 — use a fixed RA/Dec apparent for
        # the epoch and convert forward via Skyfield. Dec ~89.26° keeps this
        # near the pole where our old formula degrades.
        from skyfield.api import Star

        polaris = Star(ra_hours=2.530301, dec_degrees=89.264109)
        astrometric = self.observer.at(self.t).observe(polaris)
        apparent = astrometric.apparent()
        alt, az, _ = apparent.altaz()
        ra, dec, _ = apparent.radec(epoch="date")
        if alt.degrees < 1.0:
            self.skipTest("Polaris below horizon")
        self._check_roundtrip("polaris", alt.degrees, az.degrees, ra.hours, dec.degrees)

    def test_sirius(self):
        from skyfield.api import Star

        sirius = Star(ra_hours=6.752569, dec_degrees=-16.716116)
        astrometric = self.observer.at(self.t).observe(sirius)
        apparent = astrometric.apparent()
        alt, az, _ = apparent.altaz()
        ra, dec, _ = apparent.radec(epoch="date")
        if alt.degrees < 1.0:
            self.skipTest("Sirius below horizon")
        self._check_roundtrip("sirius", alt.degrees, az.degrees, ra.hours, dec.degrees)

    def test_zenith(self):
        # alt=89.9, az=180 — edge case where the old formula lost precision.
        from skyfield.api import Star

        zenith = self.topos.at(self.t).from_altaz(alt_degrees=89.9, az_degrees=180.0)
        ra, dec, _ = zenith.radec(epoch="date")
        self._check_roundtrip("zenith", 89.9, 180.0, ra.hours, dec.degrees)


if __name__ == "__main__":
    unittest.main()
