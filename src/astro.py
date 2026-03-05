from datetime import datetime, timedelta

from skyfield import almanac
from skyfield.api import wgs84
from tzlocal import get_localzone

from src.constants import ASTRO_EPHEMERIS, EARTH_TIMESCALE


class CelestialObject:

    def __init__(self, name: str, observer_position):
        self.name = name
        self.altitude = None
        self.azimuthal = None
        self.observer_position = observer_position
        self.data_obj = ASTRO_EPHEMERIS[name]

    def update_position(self, ref_datetime: datetime):
        """Get the position of celestial object given the datetime reference from the
        current observer position.

        Parameters
        ----------
        ref_datetime : datetime
            Python datetime object to get the future or past position of the celestial object,
        """
        time_ = EARTH_TIMESCALE.from_datetime(ref_datetime)
        astrometric = self.observer_position.at(time_).observe(self.data_obj)
        alt, az, distance = astrometric.apparent().altaz()

        self.altitude = alt
        self.azimuthal = az

    def __str__(self):
        return f"{self.name=}, {self.altitude=}, {self.azimuthal=}"

    def get_coordinates(self, precision: int = 2) -> dict:
        return {
            "altitude": round(self.altitude.degrees, precision),
            "azimuthal": round(self.azimuthal.degrees, precision),
        }


def get_rise_set_times(lat: float, lon: float, elevation: float) -> dict:
    """Return today's rise/set times for Sun and Moon as HH:MM strings.

    Uses a 2-day search window for the Moon so that a moonset that falls
    after midnight (next calendar day) is still captured.

    Returns a flat dict with keys: ``sun_rise``, ``sun_set``,
    ``moon_rise``, ``moon_set``.  Any key may be absent if the event
    doesn't occur today.  Moonset times that fall on the next calendar
    day are suffixed with ``+1`` (e.g. ``"00:32+1"``).
    """
    tz = get_localzone()
    today = datetime.now(tz=tz).replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = EARTH_TIMESCALE.from_datetime(today)
    t1_sun = EARTH_TIMESCALE.from_datetime(today + timedelta(days=1))
    t1_moon = EARTH_TIMESCALE.from_datetime(today + timedelta(days=2))
    location = wgs84.latlon(lat, lon, elevation_m=elevation)
    result = {}
    try:
        f = almanac.sunrise_sunset(ASTRO_EPHEMERIS, location)
        times, events = almanac.find_discrete(t0, t1_sun, f)
        for t, event in zip(times, events):
            s = t.astimezone(tz).strftime("%H:%M")
            if event == 1 and "sun_rise" not in result:
                result["sun_rise"] = s
            elif event == 0 and "sun_set" not in result:
                result["sun_set"] = s
    except Exception:
        pass
    try:
        moon = ASTRO_EPHEMERIS["moon"]
        f = almanac.risings_and_settings(ASTRO_EPHEMERIS, moon, location)
        # Search 2 days so a moonset after midnight is not missed
        times, events = almanac.find_discrete(t0, t1_moon, f)
        for t, event in zip(times, events):
            local_t = t.astimezone(tz)
            # Append "+1" if the event falls tomorrow so the user knows
            tomorrow = (today + timedelta(days=1)).date()
            suffix = "+1" if local_t.date() == tomorrow else ""
            s = local_t.strftime("%H:%M") + suffix
            if event == 1 and "moon_rise" not in result:
                result["moon_rise"] = s
            elif event == 0 and "moon_set" not in result:
                result["moon_set"] = s
    except Exception:
        pass
    return result
