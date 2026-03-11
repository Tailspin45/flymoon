import os
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from math import acos, cos
from math import degrees as _degrees
from math import radians, sin, sqrt
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import requests
from skyfield.api import Topos
from tzlocal import get_localzone_name

from src import logger
from src.astro import CelestialObject
from src.constants import (
    API_URL,
    ASTRO_EPHEMERIS,
    CHANGE_ELEVATION,
    INTERVAL_IN_SECS,
    NUM_SECONDS_PER_MIN,
    TEST_DATA_PATH,
    TOP_MINUTE,
    PossibilityLevel,
    get_aeroapi_key,
)
from src.flight_cache import get_cache
from src.flight_data import get_flight_data, load_existing_flight_data, parse_fligh_data

# ── FA enrichment cache ──────────────────────────────────────────────────────
# Per-callsign metadata cache (aircraft type, airline, origin, destination).
# Only populated for HIGH-probability transits — avoids per-refresh FA charges.
_FA_ENRICHMENT_CACHE: Dict[str, dict] = {}
_FA_ENRICHMENT_TTL: float = 7200.0  # 2 hours
_FA_ENRICHMENT_ERROR_TTL: float = (
    180.0  # short cache for transient HTTP/transport errors
)
_FA_ENRICHMENT_429_BACKOFF: float = 300.0  # global cooldown after AeroAPI rate limit
_FA_ENRICHMENT_BACKOFF_UNTIL: float = 0.0
_FA_ENRICHMENT_LAST_BACKOFF_LOG: float = 0.0
_FA_ENRICHMENT_BACKOFF_LOG_EVERY: float = 60.0
_FA_ENRICHMENT_LOCK = threading.Lock()


def _enrich_from_fa(callsign: str, api_key: str) -> dict:
    """Fetch flight metadata from FlightAware for a single callsign.

    Costs one FA result-set ($0.02).  Result is cached per-callsign for
    FA_ENRICHMENT_TTL seconds so repeated HIGH-prob alerts incur no extra cost.
    Returns {} silently on any error.
    """
    if not api_key:
        return {}

    global _FA_ENRICHMENT_BACKOFF_UNTIL, _FA_ENRICHMENT_LAST_BACKOFF_LOG

    now = _time.time()

    with _FA_ENRICHMENT_LOCK:
        cached = _FA_ENRICHMENT_CACHE.get(callsign)
    if cached and (now - cached["ts"]) < float(cached.get("ttl", _FA_ENRICHMENT_TTL)):
        if cached["data"]:
            logger.info(f"[FA-enrich] cache HIT for {callsign}")
        else:
            reason = cached.get("reason", "cached_miss")
            if reason == "untracked":
                logger.debug(
                    f"[FA-enrich] cached miss for {callsign} (VFR/untracked) — skipping FA call"
                )
            elif reason == "rate_limited":
                logger.debug(f"[FA-enrich] cached 429 backoff for {callsign}")
            else:
                logger.debug(f"[FA-enrich] cached miss for {callsign} ({reason})")
        return cached["data"]

    if now < _FA_ENRICHMENT_BACKOFF_UNTIL:
        remaining = int(_FA_ENRICHMENT_BACKOFF_UNTIL - now)
        if (now - _FA_ENRICHMENT_LAST_BACKOFF_LOG) >= _FA_ENRICHMENT_BACKOFF_LOG_EVERY:
            logger.warning(
                f"[FA-enrich] global 429 backoff active ({remaining}s left) — skipping {callsign}"
            )
            _FA_ENRICHMENT_LAST_BACKOFF_LOG = now
        else:
            logger.debug(
                f"[FA-enrich] backoff active ({remaining}s left), skip {callsign}"
            )
        with _FA_ENRICHMENT_LOCK:
            _FA_ENRICHMENT_CACHE[callsign] = {
                "data": {},
                "ts": now,
                "ttl": min(_FA_ENRICHMENT_ERROR_TTL, max(1.0, remaining)),
                "reason": "rate_limited",
            }
        return {}

    url = f"https://aeroapi.flightaware.com/aeroapi/flights/{callsign}"
    headers = {"Accept": "application/json; charset=UTF-8", "x-apikey": api_key}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            flights = resp.json().get("flights", [])
            if flights:
                f = flights[0]
                data = {
                    "aircraft_type": f.get("aircraft_type") or "N/A",
                    "fa_flight_id": f.get("fa_flight_id") or "",
                    "origin": (f.get("origin") or {}).get("city") or "N/A",
                    "destination": (f.get("destination") or {}).get("city") or "N/D",
                }
                with _FA_ENRICHMENT_LOCK:
                    _FA_ENRICHMENT_CACHE[callsign] = {
                        "data": data,
                        "ts": now,
                        "ttl": _FA_ENRICHMENT_TTL,
                        "reason": "ok",
                    }
                logger.info(f"[FA-enrich] fetched {callsign}: {data}")
                return data
            # VFR or untracked — no flight plan on file; cache the miss so we
            # don't burn another FA result-set on the next transit detection.
            with _FA_ENRICHMENT_LOCK:
                _FA_ENRICHMENT_CACHE[callsign] = {
                    "data": {},
                    "ts": now,
                    "ttl": _FA_ENRICHMENT_TTL,
                    "reason": "untracked",
                }
            logger.debug(
                f"[FA-enrich] no flight records for {callsign} (VFR/untracked) — cached miss"
            )
        elif resp.status_code == 429:
            with _FA_ENRICHMENT_LOCK:
                _FA_ENRICHMENT_BACKOFF_UNTIL = max(
                    _FA_ENRICHMENT_BACKOFF_UNTIL, now + _FA_ENRICHMENT_429_BACKOFF
                )
                _FA_ENRICHMENT_CACHE[callsign] = {
                    "data": {},
                    "ts": now,
                    "ttl": _FA_ENRICHMENT_429_BACKOFF,
                    "reason": "rate_limited",
                }
            logger.warning(
                f"[FA-enrich] HTTP 429 for {callsign} — backing off "
                f"{int(_FA_ENRICHMENT_429_BACKOFF)}s"
            )
        else:
            with _FA_ENRICHMENT_LOCK:
                _FA_ENRICHMENT_CACHE[callsign] = {
                    "data": {},
                    "ts": now,
                    "ttl": _FA_ENRICHMENT_ERROR_TTL,
                    "reason": f"http_{resp.status_code}",
                }
            logger.warning(
                f"[FA-enrich] HTTP {resp.status_code} for {callsign} — "
                f"cooldown {int(_FA_ENRICHMENT_ERROR_TTL)}s"
            )
    except Exception as exc:
        with _FA_ENRICHMENT_LOCK:
            _FA_ENRICHMENT_CACHE[callsign] = {
                "data": {},
                "ts": now,
                "ttl": _FA_ENRICHMENT_ERROR_TTL,
                "reason": "exception",
            }
        logger.warning(
            f"[FA-enrich] exception for {callsign}: {exc} "
            f"(cooldown {int(_FA_ENRICHMENT_ERROR_TTL)}s)"
        )
    return {}


def _parse_opensky_flight(callsign: str, os_data: dict) -> dict:
    """Convert an OpenSky state-vector dict to the internal flight format."""
    alt_m = os_data.get("altitude_m") or 0
    vr = os_data.get("vertical_rate_ms")  # preserve None when not reported

    elev_change = "level"
    if vr is not None:
        if vr > 0.5:
            elev_change = "climbing"
        elif vr < -0.5:
            elev_change = "descending"

    return {
        "name": callsign,
        "aircraft_type": "N/A",  # filled by FA enrichment on HIGH transit
        "fa_flight_id": "",
        "origin": "N/A",
        "destination": "N/A",
        "latitude": os_data["lat"],
        "longitude": os_data["lon"],
        "direction": os_data.get("heading") or 0,
        "speed": os_data.get("speed_kmh") or 0,
        "elevation": float(alt_m),
        "elevation_feet": int(alt_m * 3.28084),
        "elevation_change": elev_change,
        "position_source": os_data.get("position_source", "opensky"),
        "position_age_s": (
            round(_time.time() - os_data["last_contact"], 1)
            if os_data.get("last_contact")
            else None
        ),
        "icao24": os_data.get("icao24", ""),
        "vertical_rate": vr,
        "squawk": os_data.get("squawk"),
        "spi": os_data.get("spi", False),
        "on_ground": os_data.get("on_ground", False),
        "category": os_data.get("category"),
        "origin_country": os_data.get("origin_country"),
    }


from src.position import (
    AreaBoundingBox,
    geographic_to_altaz,
    get_my_pos,
    predict_position,
    transit_corridor_bbox,
)

EARTH = ASTRO_EPHEMERIS["earth"]

# Fallback bbox from .env — used only when observer position is missing or
# the user has explicitly configured a custom bbox in the UI.
_fallback_bbox = AreaBoundingBox(
    lat_lower_left=float(os.getenv("LAT_LOWER_LEFT", "0")),
    long_lower_left=float(os.getenv("LONG_LOWER_LEFT", "0")),
    lat_upper_right=float(os.getenv("LAT_UPPER_RIGHT", "0")),
    long_upper_right=float(os.getenv("LONG_UPPER_RIGHT", "0")),
)


def angular_separation(alt1: float, az1: float, alt2: float, az2: float) -> float:
    """Great-circle angular separation using the spherical law of cosines.

    Numerically stable for all separations and altitudes, including
    near the zenith where azimuth differences are geometrically compressed.

    Parameters
    ----------
    alt1, az1 : altitude and azimuth of first object (degrees)
    alt2, az2 : altitude and azimuth of second object (degrees)

    Returns
    -------
    Angular separation in degrees.
    """
    a1 = radians(alt1)
    a2 = radians(alt2)
    daz = radians(abs(az1 - az2))
    cos_theta = sin(a1) * sin(a2) + cos(a1) * cos(a2) * cos(daz)
    cos_theta = min(1.0, max(-1.0, cos_theta))
    return _degrees(acos(cos_theta))


# Backward-compatible alias used by some callers
def _angular_separation(alt_diff: float, az_diff: float, target_alt: float) -> float:
    """Deprecated — use angular_separation() instead."""
    return sqrt(alt_diff**2 + (az_diff * cos(radians(target_alt))) ** 2)


def calculate_angular_separation(alt_diff: float, az_diff: float) -> float:
    """Simple Euclidean angular separation in alt-az space (degrees).

    Used in tests and simple comparisons where cosine-weighting is not needed.
    """
    return sqrt(alt_diff**2 + az_diff**2)


def get_possibility_level(sep: float) -> str:
    """Classify transit probability based on angular separation.

    Uses generous thresholds so near-misses are visible for pipeline
    validation.  An aircraft at 10° is definitely not transiting, but
    seeing it classified LOW confirms the geometry is working.

    Thresholds (on-sky degrees):
    - HIGH:     ≤2.0° — direct transit very likely
    - MEDIUM:   ≤4.0° — near miss, worth recording
    - LOW:      ≤12.0° — possible distant transit / near-miss
    - UNLIKELY: >12°
    """
    if sep <= 2.0:
        return PossibilityLevel.HIGH.value
    elif sep <= 4.0:
        return PossibilityLevel.MEDIUM.value
    elif sep <= 12.0:
        return PossibilityLevel.LOW.value
    return PossibilityLevel.UNLIKELY.value


def check_transit(
    flight: dict,
    window_time: list,
    ref_datetime: datetime,
    my_position: Topos,
    target: CelestialObject,
    earth_ref,
    alt_threshold: float = 5.0,
    az_threshold: float = 10.0,
    target_positions: Optional[Dict[int, Tuple[float, float]]] = None,
) -> dict:
    """Given the data of a flight, compute a possible transit with the target.

    Parameters
    ----------
    flight : dict
        Dictionary containing the fligh data: latitude, longitude, speed, direction, elevation,
        name (which is the id of the flight), origin, destination, and coded elevation_change.
    window_time : array_like
        Data points of time in minutes to compute ahead from reference datetime.
    ref_datetime: datetime
        Reference datetime, deltas from window_time will be add to this reference to compute the future position
        of plane and target.
    my_position: Topos
        Object from skifield library which was instanced with current position of the observer (
        latitude, longitude and elevation).
    target: CelestialObject
        It could be the Moon or Sun, or whatever celestial object to compute a possible transit.
    earth_ref: Any
        Earth data gotten from the de421.bsp database by NASA's JPL.

    Returns
    -------
    ans : dict
        Dictionary with the results data, completely filled when it's a possible transit. The data includes:
        id, origin, destination, time, target_alt, plane_alt, target_az, plane_az, alt_diff, az_diff,
        is_possible_transit, and change_elev.
    """
    min_diff_combined = float("inf")
    min_sep_seen = float("inf")  # track true minimum angular separation
    response = None
    closest_approach = None  # Track closest approach even if threshold not met
    no_decreasing_count = 0
    _pts_per_min = NUM_SECONDS_PER_MIN // INTERVAL_IN_SECS
    _no_decrease_limit = 3 * _pts_per_min  # bail after 3 min of increasing diff

    for idx, minute in enumerate(window_time):
        # Get future position of plane
        future_lat, future_lon = predict_position(
            lat=flight["latitude"],
            lon=flight["longitude"],
            speed=flight["speed"],
            direction=flight["direction"],
            minutes=minute,
        )

        future_time = ref_datetime + timedelta(minutes=minute)

        # Convert future position of plane to alt-azimuthal coordinates
        # Support both 'elevation' (internal) and 'aircraft_elevation' (API response) field names
        flight_elevation = flight.get("elevation") or flight.get(
            "aircraft_elevation", 0
        )
        future_alt, future_az = geographic_to_altaz(
            future_lat,
            future_lon,
            flight_elevation,
            earth_ref,
            my_position,
            future_time,
        )

        if target_positions is not None:
            # Use precomputed target position for this minute step (no Skyfield call needed)
            t_alt, t_az = target_positions[int(minute)]
        else:
            if idx > 0 and idx % _pts_per_min == 0:
                # Update target position every 1 minute of data points
                target.update_position(future_time)
            t_alt, t_az = target.altitude.degrees, target.azimuthal.degrees

        alt_diff = abs(future_alt - t_alt)
        az_diff_raw = abs(future_az - t_az)
        az_diff = min(az_diff_raw, 360 - az_diff_raw)  # shortest arc
        diff_combined = alt_diff + az_diff

        # Initialize closest_approach with first data point if not set
        if closest_approach is None:
            closest_approach = {
                "alt_diff": round(float(alt_diff), 3),
                "az_diff": round(float(az_diff), 3),
                "time": round(float(minute), 3),
                "target_alt": round(float(t_alt), 2),
                "plane_alt": round(float(future_alt), 2),
                "target_az": round(float(t_az), 2),
                "plane_az": round(float(future_az), 2),
            }

        # Early exit: if diff has been consistently increasing for ~3 min, skip rest
        if no_decreasing_count >= _no_decrease_limit:
            logger.debug(f"diff is increasing, stop checking, min={round(minute, 2)}")
            break

        if diff_combined < min_diff_combined:
            no_decreasing_count = 0
            min_diff_combined = diff_combined
            # Always track closest approach
            closest_approach = {
                "alt_diff": round(float(alt_diff), 3),
                "az_diff": round(float(az_diff), 3),
                "time": round(float(minute), 3),
                "target_alt": round(float(t_alt), 2),
                "plane_alt": round(float(future_alt), 2),
                "target_az": round(float(t_az), 2),
                "plane_az": round(float(future_az), 2),
            }
        else:
            no_decreasing_count += 1

        # Compute true angular separation (spherical law of cosines)
        sep = angular_separation(t_alt, t_az, future_alt, future_az)

        # Track closest approach for ALL aircraft above horizon
        # (no hard gate — classification handles thresholds)
        if future_alt > 0 and sep < min_sep_seen:
            min_sep_seen = sep
            response = {
                "id": flight.get("name") or flight.get("id", ""),
                "fa_flight_id": flight.get("fa_flight_id", ""),
                "origin": flight.get("origin", ""),
                "destination": flight.get("destination", ""),
                "latitude": flight["latitude"],
                "longitude": flight["longitude"],
                "aircraft_elevation": flight.get("elevation")
                or flight.get("aircraft_elevation", 0),
                "aircraft_type": flight.get("aircraft_type", "N/A"),
                "speed": flight.get("speed", 0),
                "alt_diff": round(float(alt_diff), 3),
                "az_diff": round(float(az_diff), 3),
                "angular_separation": round(float(sep), 3),
                "time": round(float(minute), 3),
                "target_alt": round(float(t_alt), 2),
                "plane_alt": round(float(future_alt), 2),
                "target_az": round(float(t_az), 2),
                "plane_az": round(float(future_az), 2),
                "is_possible_transit": 1,  # corrected after loop
                "possibility_level": get_possibility_level(sep),
                "elevation_change": CHANGE_ELEVATION.get(
                    flight.get("elevation_change"),
                    flight.get("elevation_change"),  # pass through OpenSky full words
                ),
                "vertical_rate": flight.get("vertical_rate"),
                "category": flight.get("category"),
                "squawk": flight.get("squawk"),
                "on_ground": flight.get("on_ground", False),
                "icao24": flight.get("icao24", ""),
                "origin_country": flight.get("origin_country"),
                "direction": flight.get("direction", 0),
                "waypoints": flight.get("waypoints", []),
                "position_source": flight.get("position_source", "flightaware"),
                "position_age_s": flight.get("position_age_s"),
            }

    if response:
        # Set is_possible_transit based on classification
        level = response["possibility_level"]
        response["is_possible_transit"] = (
            0 if level == PossibilityLevel.UNLIKELY.value else 1
        )
        return response

    # Return closest approach data even if threshold not met
    result = {
        "id": flight.get("name") or flight.get("id", ""),
        "fa_flight_id": flight.get("fa_flight_id", ""),
        "origin": flight.get("origin", ""),
        "destination": flight.get("destination", ""),
        "latitude": flight["latitude"],
        "longitude": flight["longitude"],
        "aircraft_elevation": flight.get("elevation")
        or flight.get("aircraft_elevation", 0),
        "aircraft_type": flight.get("aircraft_type", "N/A"),
        "speed": flight.get("speed", 0),
        "is_possible_transit": 0,
        "possibility_level": PossibilityLevel.UNLIKELY.value,
        "elevation_change": CHANGE_ELEVATION.get(
            flight.get("elevation_change"),
            flight.get("elevation_change"),  # pass through OpenSky full words
        ),
        "vertical_rate": flight.get("vertical_rate"),
        "category": flight.get("category"),
        "squawk": flight.get("squawk"),
        "on_ground": flight.get("on_ground", False),
        "icao24": flight.get("icao24", ""),
        "origin_country": flight.get("origin_country"),
        "direction": flight.get("direction", 0),
        "waypoints": flight.get("waypoints", []),
        "position_source": flight.get("position_source", "flightaware"),
        "position_age_s": flight.get("position_age_s"),
    }

    # Include closest approach data if we found any
    if closest_approach:
        result.update(closest_approach)
    else:
        result.update(
            {
                "alt_diff": None,
                "az_diff": None,
                "time": None,
                "target_alt": None,
                "plane_alt": None,
                "target_az": None,
                "plane_az": None,
            }
        )

    return result


def get_transits(
    latitude: float,
    longitude: float,
    elevation: float,
    target_name: str = "moon",
    test_mode: bool = False,
    alt_threshold: float = 5.0,
    az_threshold: float = 10.0,
    custom_bbox: dict = None,
    data_source: str = "hybrid",
    enrich: bool = True,
) -> Dict[str, Any]:
    API_KEY = get_aeroapi_key()

    # Ensure thresholds are floats (in case they're passed as strings from Flask)
    alt_threshold = float(alt_threshold)
    az_threshold = float(az_threshold)

    logger.debug(f"{latitude=}, {longitude=}, {elevation=}")

    MY_POSITION = get_my_pos(
        lat=latitude,
        lon=longitude,
        elevation=elevation,
        base_ref=EARTH,
    )

    window_time = np.linspace(
        0, TOP_MINUTE, TOP_MINUTE * (NUM_SECONDS_PER_MIN // INTERVAL_IN_SECS)
    )
    logger.debug(f"number of times to check for each flight: {len(window_time)}")
    # Get the local timezone using tzlocal
    local_timezone = get_localzone_name()
    naive_datetime_now = datetime.now()
    # Make the datetime object timezone-aware
    ref_datetime = naive_datetime_now.replace(tzinfo=ZoneInfo(local_timezone))

    celestial_obj = CelestialObject(name=target_name, observer_position=MY_POSITION)
    celestial_obj.update_position(ref_datetime=ref_datetime)
    current_target_coordinates = celestial_obj.get_coordinates()

    logger.debug(celestial_obj.__str__())

    data = list()

    # ── Select bounding box ───────────────────────────────────────────────────
    # Priority: 1) user custom bbox from UI  2) dynamic transit corridor
    #           3) .env fallback (legacy / zero-config)
    t_alt = current_target_coordinates["altitude"]
    t_az = current_target_coordinates["azimuthal"]
    if custom_bbox:
        bbox = AreaBoundingBox(
            lat_lower_left=custom_bbox["lat_lower_left"],
            long_lower_left=custom_bbox["lon_lower_left"],
            lat_upper_right=custom_bbox["lat_upper_right"],
            long_upper_right=custom_bbox["lon_upper_right"],
        )
        logger.info("[Bbox] Using user-supplied custom bbox")
    elif t_alt > 0:
        bbox = transit_corridor_bbox(
            obs_lat=latitude,
            obs_lon=longitude,
            target_alt_deg=t_alt,
            target_az_deg=t_az,
        )
        logger.debug(
            f"[Bbox] Dynamic corridor: "
            f"({bbox.lat_lower_left:.2f},{bbox.long_lower_left:.2f}) → "
            f"({bbox.lat_upper_right:.2f},{bbox.long_upper_right:.2f})"
        )
    else:
        bbox = _fallback_bbox
        logger.info("[Bbox] Target below horizon — using .env fallback bbox")

    if current_target_coordinates["altitude"] > 0:
        if test_mode:
            raw_flight_data = load_existing_flight_data(TEST_DATA_PATH)
            logger.info("Loading existing flight data since is using TEST mode")
            flight_data = list()
            filtered_count = 0
            for flight in raw_flight_data["flights"]:
                parsed = parse_fligh_data(flight)
                lat, lon = parsed["latitude"], parsed["longitude"]
                if (
                    bbox.lat_lower_left <= lat <= bbox.lat_upper_right
                    and bbox.long_lower_left <= lon <= bbox.long_upper_right
                ):
                    flight_data.append(parsed)
                else:
                    filtered_count += 1
            if filtered_count > 0:
                logger.debug(f"Bbox filter: {filtered_count} flights outside box")

        elif data_source == "fa-only":
            # ── FA-only mode: call FlightAware bbox search every refresh ──
            # Expensive but provides full metadata for all aircraft.
            logger.info("[Data] FA-only mode — calling FlightAware bbox API")
            cache = get_cache()
            cached_data = cache.get(
                bbox.lat_lower_left,
                bbox.long_lower_left,
                bbox.lat_upper_right,
                bbox.long_upper_right,
            )
            if cached_data is not None:
                raw_flight_data = cached_data
                logger.info(
                    f"Using cached FA data ({cache.get_stats()['hit_rate_percent']}% hit rate)"
                )
            else:
                raw_flight_data = get_flight_data(bbox, API_URL, API_KEY)
                cache.set(
                    bbox.lat_lower_left,
                    bbox.long_lower_left,
                    bbox.lat_upper_right,
                    bbox.long_upper_right,
                    raw_flight_data,
                )

            flight_data = list()
            filtered_count = 0
            for flight in raw_flight_data["flights"]:
                parsed = parse_fligh_data(flight)
                lat, lon = parsed["latitude"], parsed["longitude"]
                if (
                    bbox.lat_lower_left <= lat <= bbox.lat_upper_right
                    and bbox.long_lower_left <= lon <= bbox.long_upper_right
                ):
                    flight_data.append(parsed)
                else:
                    filtered_count += 1
            if filtered_count > 0:
                logger.debug(f"Bbox filter (FA): {filtered_count} flights outside box")

        elif data_source == "adsb-local":
            # ── ADS-B local receiver mode ─────────────────────────────────
            adsb_url = os.getenv("ADSB_LOCAL_URL", "")
            if not adsb_url:
                logger.warning(
                    "[ADS-B] data_source=adsb-local but ADSB_LOCAL_URL is not set in .env. "
                    "Falling back to OpenSky. Set ADSB_LOCAL_URL=http://<receiver-ip>/data/aircraft.json "
                    "to use a local RTL-SDR / dump1090 / tar1090 receiver."
                )
                data_source = "hybrid"  # fall through to OpenSky block below
            else:
                # TODO: implement local ADS-B JSON fetch from ADSB_LOCAL_URL
                logger.warning(
                    f"[ADS-B] Local receiver at {adsb_url} — not yet implemented; falling back to OpenSky"
                )
                data_source = "hybrid"

        if not test_mode and data_source in ("hybrid", "opensky-only"):
            # ── Hybrid / OpenSky-only mode (default) ─────────────────────
            # Use OpenSky Network for all position data (~60s cache, free).
            # FA is only called on HIGH-probability transits for metadata.
            logger.info(f"[Data] {data_source} mode — using OpenSky as primary source")
            from src.opensky import fetch_opensky_positions

            try:
                opensky_data = fetch_opensky_positions(
                    bbox.lat_lower_left,
                    bbox.long_lower_left,
                    bbox.lat_upper_right,
                    bbox.long_upper_right,
                )
            except Exception as exc:
                logger.warning(
                    f"[OpenSky] fetch failed: {exc}; flight_data will be empty"
                )
                opensky_data = {}

            flight_data = []
            for callsign, os_pos in opensky_data.items():
                if os_pos.get("on_ground"):
                    continue  # skip ground traffic
                lat, lon = os_pos.get("lat"), os_pos.get("lon")
                if lat is None or lon is None:
                    continue
                if not (
                    bbox.lat_lower_left <= lat <= bbox.lat_upper_right
                    and bbox.long_lower_left <= lon <= bbox.long_upper_right
                ):
                    continue  # outside bounding box
                flight_data.append(_parse_opensky_flight(callsign, os_pos))

            logger.info(f"[OpenSky] {len(flight_data)} airborne aircraft in bbox")

        # ── Coarse angular pre-filter ─────────────────────────────────────
        # Discard aircraft that cannot possibly reach the target within the
        # 15-minute window.  One cheap Skyfield call per aircraft replaces
        # 180 full trajectory steps for aircraft that are clearly out of range.
        # Uses the same angular-separation metric as check_transit().
        _combined_threshold = max(alt_threshold, az_threshold)
        _COARSE_SEP = max(_combined_threshold * 5, 30.0)  # generous margin

        prefiltered = []
        excluded = []  # aircraft too far from target to transit — still shown in table
        for f in flight_data:
            try:
                f_alt, f_az = geographic_to_altaz(
                    f["latitude"],
                    f["longitude"],
                    f.get("elevation", 0) or 0,
                    EARTH,
                    MY_POSITION,
                    ref_datetime,
                )
            except Exception:
                prefiltered.append(f)  # keep on error
                continue
            sep = angular_separation(t_alt, t_az, f_alt, f_az)
            if sep <= _COARSE_SEP:
                prefiltered.append(f)
            else:
                # Store current angular position so sky Δ can be shown in the table
                f = dict(f)
                f["_current_alt"] = f_alt
                f["_current_az"] = f_az
                excluded.append(f)

        logger.info(
            f"[Pre-filter] {len(prefiltered)}/{len(flight_data)} aircraft kept "
            f"(coarse angular sep ≤{_COARSE_SEP:.0f}°)"
        )
        flight_data = prefiltered

        # ── Precompute target positions for all minute steps ──────────────
        # Avoids 50+ redundant Skyfield calls (one per flight per minute step).
        target_positions: Dict[int, Tuple[float, float]] = {}
        for step in range(int(window_time[-1]) + 1):
            celestial_obj.update_position(ref_datetime + timedelta(minutes=step))
            target_positions[step] = (
                celestial_obj.altitude.degrees,
                celestial_obj.azimuthal.degrees,
            )
        celestial_obj.update_position(ref_datetime=ref_datetime)  # restore t=0

        # ── Transit detection (parallel across flights) ───────────────────
        def _check_and_enrich(flight):
            result = check_transit(
                flight,
                window_time,
                ref_datetime,
                MY_POSITION,
                celestial_obj,
                EARTH,
                alt_threshold,
                az_threshold,
                target_positions=target_positions,
            )
            if (
                enrich
                and data_source not in ("fa-only",)
                and not test_mode
                and result.get("possibility_level") == PossibilityLevel.HIGH.value
                and API_KEY
            ):
                callsign = flight["name"]
                enrichment = _enrich_from_fa(callsign, API_KEY)
                if enrichment:
                    result.update(enrichment)
                    logger.info(f"[FA-enrich] enriched HIGH transit {callsign}")
            return result

        max_workers = min(8, len(flight_data)) if flight_data else 1
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_check_and_enrich, f): f for f in flight_data}
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    data.append(result)

        # Add aircraft excluded by pre-filter as position-only rows (no transit analysis)
        for f in excluded:
            data.append(
                {
                    "id": f.get("name") or f.get("id", ""),
                    "fa_flight_id": f.get("fa_flight_id", ""),
                    "origin": f.get("origin", ""),
                    "destination": f.get("destination", ""),
                    "latitude": f["latitude"],
                    "longitude": f["longitude"],
                    "aircraft_elevation": f.get("elevation")
                    or f.get("aircraft_elevation", 0),
                    "aircraft_type": f.get("aircraft_type", "N/A"),
                    "speed": f.get("speed", 0),
                    "direction": f.get("direction", 0),
                    "is_possible_transit": 0,
                    "possibility_level": PossibilityLevel.UNLIKELY.value,
                    "elevation_change": CHANGE_ELEVATION.get(
                        f.get("elevation_change"), f.get("elevation_change")
                    ),
                    "vertical_rate": f.get("vertical_rate"),
                    "category": f.get("category"),
                    "squawk": f.get("squawk"),
                    "on_ground": f.get("on_ground", False),
                    "icao24": f.get("icao24", ""),
                    "origin_country": f.get("origin_country"),
                    "waypoints": f.get("waypoints", []),
                    "position_source": f.get("position_source", "flightaware"),
                    "position_age_s": f.get("position_age_s"),
                    "alt_diff": (
                        round(f["_current_alt"] - t_alt, 2)
                        if "_current_alt" in f
                        else None
                    ),
                    "az_diff": (
                        round(
                            min(
                                abs(f["_current_az"] - t_az),
                                360 - abs(f["_current_az"] - t_az),
                            ),
                            2,
                        )
                        if "_current_az" in f
                        else None
                    ),
                    "time": None,
                    "target_alt": round(t_alt, 2),
                    "plane_alt": (
                        round(f["_current_alt"], 2) if "_current_alt" in f else None
                    ),
                    "target_az": round(t_az, 2),
                    "plane_az": (
                        round(f["_current_az"], 2) if "_current_az" in f else None
                    ),
                }
            )
    else:
        logger.debug(
            f"{target_name} target is under horizon, skipping checking for transits..."
        )

    # ── Transit summary logging ───────────────────────────────────────────
    # Critical observability: nearest-miss + per-level counts
    if data:
        level_counts = {
            "HIGH": 0,
            "MEDIUM": 0,
            "LOW": 0,
            "UNLIKELY": 0,
        }
        nearest_sep = float("inf")
        nearest_id = ""
        for d in data:
            level = d.get("possibility_level")
            for name, enum_val in PossibilityLevel.__members__.items():
                if enum_val.value == level:
                    level_counts[name] = level_counts.get(name, 0) + 1
                    break
            d_sep = d.get("angular_separation")
            if d_sep is not None and d_sep < nearest_sep:
                nearest_sep = d_sep
                nearest_id = d.get("id", "?")

        logger.info(
            f"[Transit Summary] {len(data)} aircraft total → "
            f"HIGH={level_counts['HIGH']}, MEDIUM={level_counts['MEDIUM']}, "
            f"LOW={level_counts['LOW']}, UNLIKELY={level_counts['UNLIKELY']}"
        )
        if nearest_sep < float("inf"):
            logger.info(
                f"[Transit Summary] Nearest: {nearest_id} at "
                f"{nearest_sep:.2f}° from {target_name}"
            )

    return {
        "flights": data,
        "targetCoordinates": current_target_coordinates,
        "bbox_used": {
            "latLowerLeft": bbox.lat_lower_left,
            "lonLowerLeft": bbox.long_lower_left,
            "latUpperRight": bbox.lat_upper_right,
            "lonUpperRight": bbox.long_upper_right,
        },
    }


def recalculate_transits(
    flights: List[dict],
    latitude: float,
    longitude: float,
    elevation: float,
    target_name: str = "moon",
    alt_threshold: float = 5.0,
    az_threshold: float = 10.0,
) -> Dict[str, Any]:
    """
    Recalculate transit predictions for existing flights with updated positions.
    Does NOT call FlightAware API - uses provided flight data.

    Args:
        flights: List of flight dicts with updated positions (lat, lon, elevation, speed, direction)
        latitude: Observer latitude
        longitude: Observer longitude
        elevation: Observer elevation (meters)
        target_name: "sun" or "moon"
        alt_threshold: Altitude threshold in degrees
        az_threshold: Azimuth threshold in degrees

    Returns:
        List of flight dicts with updated transit predictions
    """
    # Ensure thresholds are floats
    alt_threshold = float(alt_threshold)
    az_threshold = float(az_threshold)

    MY_POSITION = get_my_pos(
        lat=latitude,
        lon=longitude,
        elevation=elevation,
        base_ref=EARTH,
    )

    window_time = np.linspace(
        0, TOP_MINUTE, TOP_MINUTE * (NUM_SECONDS_PER_MIN // INTERVAL_IN_SECS)
    )

    # Get local timezone
    local_timezone = get_localzone_name()
    naive_datetime_now = datetime.now()
    ref_datetime = naive_datetime_now.replace(tzinfo=ZoneInfo(local_timezone))

    celestial_obj = CelestialObject(name=target_name, observer_position=MY_POSITION)
    celestial_obj.update_position(ref_datetime=ref_datetime)
    current_target_coordinates = celestial_obj.get_coordinates()

    data = []

    if current_target_coordinates["altitude"] > 0:
        target_positions: Dict[int, Tuple[float, float]] = {}
        for step in range(int(window_time[-1]) + 1):
            celestial_obj.update_position(ref_datetime + timedelta(minutes=step))
            target_positions[step] = (
                celestial_obj.altitude.degrees,
                celestial_obj.azimuthal.degrees,
            )
        celestial_obj.update_position(ref_datetime=ref_datetime)

        def _check(flight):
            return check_transit(
                flight,
                window_time,
                ref_datetime,
                MY_POSITION,
                celestial_obj,
                EARTH,
                alt_threshold,
                az_threshold,
                target_positions=target_positions,
            )

        max_workers = min(8, len(flights)) if flights else 1
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            data = list(pool.map(_check, flights))

    return {"flights": data, "targetCoordinates": current_target_coordinates}
