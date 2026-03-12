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


def targets_above_horizon(lat: float, lon: float, elevation: float = 0) -> bool:
    """Return True if the Sun or Moon is currently above the horizon.

    Used to gate Seestar reconnect attempts — no point trying to connect
    when there is no observable target.
    """
    from datetime import timezone

    try:
        from skyfield.api import wgs84

        observer = wgs84.latlon(lat, lon, elevation_m=elevation)
        now = EARTH_TIMESCALE.from_datetime(datetime.now(tz=timezone.utc))
        for body_name in ("sun", "moon"):
            body = ASTRO_EPHEMERIS[body_name]
            alt, _, _ = observer.at(now).observe(body).apparent().altaz()
            if alt.degrees > 0:
                return True
        return False
    except Exception:
        return True  # fail open — don't suppress reconnects on error


def target_above_min_altitude(
    target: str,
    lat: float,
    lon: float,
    elevation: float = 0,
    min_altitude: float = 10.0,
) -> bool:
    """Return True if *target* is currently at or above *min_altitude* degrees.

    Used to gate Seestar reconnect attempts so the scope only reconnects when
    the selected viewing target is actually at a useful elevation.
    Falls back to ``targets_above_horizon`` when target is unknown/None.

    Parameters
    ----------
    target:
        Celestial body name — ``"sun"`` or ``"moon"``.
    lat, lon, elevation:
        Observer coordinates (degrees / metres).
    min_altitude:
        Minimum altitude in degrees above the horizon.
    """
    from datetime import timezone

    if target not in ("sun", "moon"):
        return targets_above_horizon(lat, lon, elevation)

    try:
        from skyfield.api import wgs84

        observer = wgs84.latlon(lat, lon, elevation_m=elevation)
        now = EARTH_TIMESCALE.from_datetime(datetime.now(tz=timezone.utc))
        body = ASTRO_EPHEMERIS[target]
        alt, _, _ = observer.at(now).observe(body).apparent().altaz()
        return alt.degrees >= min_altitude
    except Exception:
        return True  # fail open — don't suppress reconnects on error
