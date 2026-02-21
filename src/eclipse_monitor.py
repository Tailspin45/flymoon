"""
Eclipse detection for Flymoon.

Detects solar and lunar eclipses within a configurable lookahead window
(default 48 hours) for a given observer location.

## Lunar eclipses
Uses skyfield.eclipselib.lunar_eclipses() which returns the time of greatest
eclipse and the geometric parameters needed to derive contact times.

Contact times are computed with the formula:
    half_duration = sqrt((R_a ± R_moon)² - d²) / v_moon
where:
    R_a       = umbra_radius_radians  (the Earth's umbral shadow radius)
    R_moon    = moon_radius_radians   (Moon's angular radius)
    d         = closest_approach_radians  (min Moon-centre to umbra-centre dist)
    v_moon    = 0.00959 rad/h         (Moon's angular speed relative to umbra)

    Half-duration of umbral eclipse (U1→max, max→U4):
        hd_umbra = sqrt((R_a + R_moon)² - d²) / v_moon
    Half-duration of totality (U2→max, max→U3) — total eclipses only:
        hd_total = sqrt((R_a - R_moon)² - d²) / v_moon
        (valid only when R_a - R_moon > d, i.e. umbral_magnitude >= 1)

Penumbral eclipses (skyfield type 0) are intentionally ignored — the Moon
barely dims and there is nothing visually interesting to record.

## Solar eclipses
Skyfield has no built-in solar eclipse finder.  We detect them geometrically:
a solar eclipse begins for the observer when the angular separation between
the Moon and Sun (as seen from the observer's location) drops below the sum of
their angular radii (partial contact), or below the absolute difference of
their radii (totality / annularity).

Algorithm:
  1. Scan forward in 10-minute steps over the lookahead window.
  2. When the separation first dips below (R_sun + R_moon), we are near an
     eclipse.  Binary-search in ±30-min window to find precise C1 (and C4).
  3. Check whether the minimum separation is less than |R_sun - R_moon| to
     classify as total/annular vs partial.

Observer location must be set via environment variables:
    OBSERVER_LATITUDE   (decimal degrees, N positive)
    OBSERVER_LONGITUDE  (decimal degrees, E positive)
    OBSERVER_ELEVATION  (metres above sea level)
"""

import math
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from src import logger

# ── Skyfield resources (already loaded by constants.py) ──────────────────────
from src.constants import ASTRO_EPHEMERIS as eph, EARTH_TIMESCALE as ts

# Moon's mean angular speed relative to Earth's shadow centre (rad / hour).
# Derived from sidereal orbital period: 360° / (27.32 days * 24 h) ≈ 0.549°/h
# relative to the stars, plus the Sun's apparent motion (~0.041°/h), giving
# ~0.508°/h = 0.00887 rad/h relative to the umbra.  An empirical value of
# 0.00959 rad/h (as used by the USNO) gives slightly better agreement with
# tabulated eclipse contact times.
_V_MOON_RAD_PER_HOUR = 0.00959


def _observer_topos(lat: float, lon: float, elevation_m: float):
    """Return a Skyfield Topos (observer on Earth's surface)."""
    from skyfield.api import wgs84
    return wgs84.latlon(lat, lon, elevation_m=elevation_m)


def _angular_separation_rad(pos_a, pos_b) -> float:
    """Angular separation in radians between two Skyfield astrometric positions."""
    import numpy as np
    a = pos_a.position.au
    b = pos_b.position.au
    cos_angle = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
    return math.acos(max(-1.0, min(1.0, cos_angle)))


def _angular_radius_rad(body_pos, observer_pos, body_name: Optional[str] = None) -> float:
    """
    Angular radius of a solar-system body in radians.

    Parameters
    ----------
    body_pos:
        Astrometric position from observer (in AU).
    observer_pos:
        Observer position (currently unused, kept for API compatibility).
    body_name:
        Explicit body name, e.g. "sun" or "moon". If omitted the function
        falls back to the public ``body_name`` attribute then the private
        ``_body_name`` attribute on ``body_pos`` for backwards compatibility.

    Raises
    ------
    ValueError
        If the body name cannot be determined or is not supported.
    """
    AU_KM = 149_597_870.7
    RADII_KM = {"sun": 696_000.0, "moon": 1_737.4}

    name = body_name
    if name is None:
        name = getattr(body_pos, "body_name", None)
    if name is None:
        name = getattr(body_pos, "_body_name", None)

    if not isinstance(name, str):
        logger.warning("Unable to determine body name for angular radius computation.")
        raise ValueError("Unknown body for angular radius computation (no name provided).")

    name_key = name.lower()
    if name_key not in RADII_KM:
        logger.warning("Unsupported body '%s' for angular radius computation.", name)
        raise ValueError(f"Unsupported body '{name}' for angular radius computation.")

    distance_au = float(body_pos.distance().au)
    return math.atan(RADII_KM[name_key] / (distance_au * AU_KM))


# ─────────────────────────────────────────────────────────────────────────────
#  Lunar eclipse detection
# ─────────────────────────────────────────────────────────────────────────────

def _lunar_eclipse_contacts(t_max, details: dict) -> dict:
    """
    Derive U1, U2, U3, U4 from skyfield lunar_eclipses() details.

    Returns a dict with keys: u1, u2, u3, u4 as Python UTC datetimes.
    u2 / u3 are None for partial eclipses.
    """
    d    = float(details["closest_approach_radians"])
    R_u  = float(details["umbra_radius_radians"])
    R_m  = float(details["moon_radius_radians"])
    v    = _V_MOON_RAD_PER_HOUR  # rad / hour

    # Umbral half-duration (hours): Moon entering/exiting umbra
    inner = (R_u + R_m) ** 2 - d ** 2
    if inner < 0:
        raise ValueError("Geometry inconsistency: umbra half-duration imaginary")
    hd_umbra = math.sqrt(inner) / v  # hours

    t_max_utc = t_max.utc_datetime()
    u1 = t_max_utc - timedelta(hours=hd_umbra)
    u4 = t_max_utc + timedelta(hours=hd_umbra)

    # Totality half-duration — only when umbral_magnitude >= 1
    umbral_mag = float(details["umbral_magnitude"])
    u2 = u3 = None
    if umbral_mag >= 1.0:
        inner_t = (R_u - R_m) ** 2 - d ** 2
        if inner_t >= 0:
            hd_total = math.sqrt(inner_t) / v
            u2 = t_max_utc - timedelta(hours=hd_total)
            u3 = t_max_utc + timedelta(hours=hd_total)

    return {"u1": u1, "u2": u2, "u3": u3, "u4": u4}


def _find_lunar_eclipse(hours_ahead: float, lat: float, lon: float, elev: float) -> Optional[dict]:
    """
    Return the next lunar eclipse starting within `hours_ahead` hours, or None.

    Lunar eclipses are global events — observer location only affects whether
    the Moon is above the horizon, which we do NOT filter here (the user may
    be setting up in advance).  Eclipse type 0 = penumbral (ignored),
    1 = partial, 2 = total.
    """
    try:
        from skyfield.eclipselib import lunar_eclipses

        now_utc = datetime.now(timezone.utc)
        t0 = ts.from_datetime(now_utc)
        t1 = ts.from_datetime(now_utc + timedelta(hours=hours_ahead))

        times, e_types, details = lunar_eclipses(t0, t1, eph)

        for i, e_type in enumerate(e_types):
            if e_type == 0:
                continue  # penumbral — ignore

            # Slice detail arrays for this eclipse
            eclipse_details = {k: v[i] for k, v in details.items()}
            contacts = _lunar_eclipse_contacts(times[i], eclipse_details)

            # Only include if C1 (u1) is within our lookahead window
            if contacts["u1"] > now_utc + timedelta(hours=hours_ahead):
                continue

            eclipse_class = "total" if e_type == 2 else "partial"
            t_max_utc = times[i].utc_datetime()

            return {
                "type":          "lunar",
                "eclipse_class": eclipse_class,
                "target":        "Moon",
                "c1":            contacts["u1"].isoformat(),
                "c2":            contacts["u2"].isoformat() if contacts["u2"] else None,
                "c3":            contacts["u3"].isoformat() if contacts["u3"] else None,
                "c4":            contacts["u4"].isoformat(),
                "max":           t_max_utc.isoformat(),
                "seconds_to_c1": int((contacts["u1"] - now_utc).total_seconds()),
            }

    except Exception as e:
        logger.warning(f"[EclipseMonitor] Lunar eclipse detection error: {e}")

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Solar eclipse detection
# ─────────────────────────────────────────────────────────────────────────────

def _sun_moon_separation(t_sky, observer_topos):
    """
    Returns (separation_rad, r_sun_rad, r_moon_rad) at Skyfield time t_sky.
    """
    earth = eph["earth"]
    sun   = eph["sun"]
    moon  = eph["moon"]

    observer = earth + observer_topos
    astr_sun  = observer.at(t_sky).observe(sun).apparent()
    astr_moon = observer.at(t_sky).observe(moon).apparent()

    sep = _angular_separation_rad(astr_sun, astr_moon)

    AU_KM = 149_597_870.7
    dist_sun  = float(astr_sun.distance().au)
    dist_moon = float(astr_moon.distance().au)
    r_sun  = math.atan(696_000.0  / (dist_sun  * AU_KM))
    r_moon = math.atan(1_737.4    / (dist_moon * AU_KM))

    return sep, r_sun, r_moon


def _find_solar_eclipse(hours_ahead: float, lat: float, lon: float, elev: float) -> Optional[dict]:
    """
    Return the next solar eclipse visible at observer within `hours_ahead` hours, or None.

    Strategy:
      1. Find new moons within the window (solar eclipses only happen at new moon).
      2. Scan ±6 h around each new moon in 10-min steps (~72 iterations max).
      3. Binary-search to find C1 / C4 (±10 s accuracy).
      4. Classify partial / total / annular and find C2 / C3 if applicable.
    """
    try:
        from skyfield import almanac

        observer_topos = _observer_topos(lat, lon, elev)
        now_utc = datetime.now(timezone.utc)

        # Solar eclipses ONLY happen at new moon.  Find new moons in the window
        # (expanded ±6 h so we don't miss eclipses whose C1 falls at the edge).
        t_search_start = ts.from_datetime(now_utc - timedelta(hours=6))
        t_search_end   = ts.from_datetime(now_utc + timedelta(hours=hours_ahead + 6))
        moon_times, moon_phases_arr = almanac.find_discrete(
            t_search_start, t_search_end, almanac.moon_phases(eph)
        )
        new_moons = [t for t, p in zip(moon_times, moon_phases_arr) if p == 0]

        if not new_moons:
            return None

        # --- Phase 1: coarse scan ±6 h around each new moon (≤72 steps) --
        step_minutes     = 10
        eclipse_start_jd = None
        eclipse_end_jd   = None
        min_sep          = float("inf")
        min_sep_jd       = None

        for new_moon_t in new_moons:
            prev_in_eclipse = False
            for i in range(-36, 37):  # ±6 h in 10-min steps
                t_check = new_moon_t.utc_datetime() + timedelta(minutes=i * step_minutes)
                if t_check < now_utc - timedelta(minutes=step_minutes):
                    continue
                if t_check > now_utc + timedelta(hours=hours_ahead + 1):
                    break
                t_sky = ts.from_datetime(t_check)
                sep, r_sun, r_moon = _sun_moon_separation(t_sky, observer_topos)
                in_eclipse = sep < r_sun + r_moon

                if in_eclipse and sep < min_sep:
                    min_sep    = sep
                    min_sep_jd = t_sky.tt
                if in_eclipse and not prev_in_eclipse:
                    eclipse_start_jd = t_sky.tt
                if not in_eclipse and prev_in_eclipse:
                    eclipse_end_jd = t_sky.tt
                prev_in_eclipse = in_eclipse

            if eclipse_start_jd is not None:
                break  # Found — no need to check other new moons

        if eclipse_start_jd is None:
            return None  # No solar eclipse at this observer location in window

        # If we never saw the end, approximate
        if eclipse_end_jd is None:
            eclipse_end_jd = ts.from_datetime(
                now_utc + timedelta(hours=hours_ahead)
            ).tt

        # --- Phase 2: binary-search for precise C1 and C4 ---------------
        def _bisect_contact(jd_before, jd_after, want_in_eclipse: bool, tol_sec=10):
            """Return JD where eclipse contact occurs (within tol_sec seconds)."""
            lo, hi = jd_before, jd_after
            tol_jd = tol_sec / 86400.0
            while hi - lo > tol_jd:
                mid = (lo + hi) / 2
                t_mid = ts.tt_jd(mid)
                sep, r_sun, r_moon = _sun_moon_separation(t_mid, observer_topos)
                if (sep < r_sun + r_moon) == want_in_eclipse:
                    hi = mid
                else:
                    lo = mid
            return (lo + hi) / 2

        # C1: transition from outside → inside eclipse
        c1_jd = _bisect_contact(
            eclipse_start_jd - step_minutes / 1440.0,
            eclipse_start_jd,
            want_in_eclipse=True
        )
        # C4: transition from inside → outside eclipse
        c4_jd = _bisect_contact(
            eclipse_end_jd - step_minutes / 1440.0,
            eclipse_end_jd,
            want_in_eclipse=False
        )

        def jd_to_utc(jd: float) -> datetime:
            return ts.tt_jd(jd).utc_datetime()

        c1_utc = jd_to_utc(c1_jd)
        c4_utc = jd_to_utc(c4_jd)

        # --- Phase 3: classify and find C2/C3 for total/annular ----------
        t_max_sky = ts.tt_jd(min_sep_jd)
        sep_min, r_sun_max, r_moon_max = _sun_moon_separation(t_max_sky, observer_topos)

        c2_utc = c3_utc = None
        eclipse_class = "partial"

        if sep_min < abs(r_sun_max - r_moon_max):
            # Total or annular
            eclipse_class = "total" if r_moon_max >= r_sun_max else "annular"

            # C2: Moon fully covers Sun (inner contact, ingress)
            # C3: Moon starts to uncover Sun (inner contact, egress)
            # Binary-search for inner contact (sep < |r_sun - r_moon|)
            inner_thresh = abs(r_sun_max - r_moon_max)

            def _inside_totality(jd):
                t = ts.tt_jd(jd)
                s, rs, rm = _sun_moon_separation(t, observer_topos)
                return s < abs(rs - rm)

            # Find C2 between c1_jd and min_sep_jd
            lo, hi = c1_jd, min_sep_jd
            tol_jd = 10 / 86400.0
            while hi - lo > tol_jd:
                mid = (lo + hi) / 2
                if _inside_totality(mid):
                    hi = mid
                else:
                    lo = mid
            c2_utc = jd_to_utc((lo + hi) / 2)

            # Find C3 between min_sep_jd and c4_jd
            lo, hi = min_sep_jd, c4_jd
            while hi - lo > tol_jd:
                mid = (lo + hi) / 2
                if _inside_totality(mid):
                    lo = mid
                else:
                    hi = mid
            c3_utc = jd_to_utc((lo + hi) / 2)

        t_max_utc = t_max_sky.utc_datetime()

        # Only include if C1 is within our lookahead window
        if c1_utc > now_utc + timedelta(hours=hours_ahead):
            return None

        return {
            "type":          "solar",
            "eclipse_class": eclipse_class,
            "target":        "Sun",
            "c1":            c1_utc.isoformat(),
            "c2":            c2_utc.isoformat() if c2_utc else None,
            "c3":            c3_utc.isoformat() if c3_utc else None,
            "c4":            c4_utc.isoformat(),
            "max":           t_max_utc.isoformat(),
            "seconds_to_c1": int((c1_utc - now_utc).total_seconds()),
        }

    except Exception as e:
        logger.warning(f"[EclipseMonitor] Solar eclipse detection error: {e}")

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────────────

class EclipseMonitor:
    """
    Detects the next solar or lunar eclipse visible within a configurable
    lookahead window.

    Usage:
        monitor = EclipseMonitor()
        eclipse = monitor.get_upcoming_eclipse()
        # Returns dict or None
    """

    def __init__(self, hours_ahead: float = 48.0):
        self.hours_ahead = hours_ahead

    def get_upcoming_eclipse(
        self,
        lat:  Optional[float] = None,
        lon:  Optional[float] = None,
        elev: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Return the nearest upcoming eclipse (solar or lunar) within
        self.hours_ahead hours, or None.

        If lat/lon/elev are not provided, they are read from the environment
        variables OBSERVER_LATITUDE, OBSERVER_LONGITUDE, OBSERVER_ELEVATION.

        The returned dict always has these keys:
            type          "solar" | "lunar"
            eclipse_class "total" | "partial" | "annular"
            target        "Sun"   | "Moon"
            c1            ISO-8601 UTC string — first contact (recording start)
            c2            ISO-8601 UTC string | None — totality/annularity start
            c3            ISO-8601 UTC string | None — totality/annularity end
            c4            ISO-8601 UTC string — last contact (recording end)
            max           ISO-8601 UTC string — greatest eclipse
            seconds_to_c1 int  (negative = C1 already passed, eclipse in progress)

        Alert levels (for UI):
            seconds_to_c1 > 48 * 3600   → no alert (beyond window)
            seconds_to_c1 in (3600, 48h] → 'outlook'  (banner only)
            seconds_to_c1 in (30, 3600]  → 'watch'    (countdown card)
            seconds_to_c1 in (-10, 30]   → 'warning'  (pulsing, arm recording)
            c1 ≤ now ≤ c4               → 'active'   (recording in progress)
            now > c4 (within 30 min)    → 'cleared'  (post-event summary)
        """
        if lat is None:
            lat  = float(os.getenv("OBSERVER_LATITUDE",  "0"))
        if lon is None:
            lon  = float(os.getenv("OBSERVER_LONGITUDE", "0"))
        if elev is None:
            elev = float(os.getenv("OBSERVER_ELEVATION", "0"))

        # Check both types; return whichever C1 is sooner
        lunar = _find_lunar_eclipse(self.hours_ahead, lat, lon, elev)
        solar = _find_solar_eclipse(self.hours_ahead, lat, lon, elev)

        candidates = [e for e in [lunar, solar] if e is not None]
        if not candidates:
            return None

        # Return the one whose C1 comes first
        return min(candidates, key=lambda e: e["seconds_to_c1"])


# Module-level singleton (created lazily)
_eclipse_monitor: Optional[EclipseMonitor] = None


def get_eclipse_monitor() -> EclipseMonitor:
    global _eclipse_monitor
    if _eclipse_monitor is None:
        _eclipse_monitor = EclipseMonitor(hours_ahead=48.0)
    return _eclipse_monitor
