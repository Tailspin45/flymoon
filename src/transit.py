import os
import time as _time
from datetime import datetime, timedelta
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
    Altitude,
    PossibilityLevel,
    get_aeroapi_key,
)
from src.flight_data import get_flight_data, load_existing_flight_data, parse_fligh_data
from src.flight_cache import get_cache

# ── FA enrichment cache ──────────────────────────────────────────────────────
# Per-callsign metadata cache (aircraft type, airline, origin, destination).
# Only populated for HIGH-probability transits — avoids per-refresh FA charges.
_FA_ENRICHMENT_CACHE: Dict[str, dict] = {}
_FA_ENRICHMENT_TTL: float = 7200.0  # 2 hours


def _enrich_from_fa(callsign: str, api_key: str) -> dict:
    """Fetch flight metadata from FlightAware for a single callsign.

    Costs one FA result-set ($0.02).  Result is cached per-callsign for
    FA_ENRICHMENT_TTL seconds so repeated HIGH-prob alerts incur no extra cost.
    Returns {} silently on any error.
    """
    if not api_key:
        return {}

    now = _time.time()
    cached = _FA_ENRICHMENT_CACHE.get(callsign)
    if cached and (now - cached["ts"]) < _FA_ENRICHMENT_TTL:
        logger.info(f"[FA-enrich] cache HIT for {callsign}")
        return cached["data"]

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
                    "fa_flight_id":  f.get("fa_flight_id") or "",
                    "origin":        (f.get("origin") or {}).get("city") or "N/A",
                    "destination":   (f.get("destination") or {}).get("city") or "N/D",
                }
                _FA_ENRICHMENT_CACHE[callsign] = {"data": data, "ts": now}
                logger.info(f"[FA-enrich] fetched {callsign}: {data}")
                return data
        logger.warning(f"[FA-enrich] HTTP {resp.status_code} for {callsign}")
    except Exception as exc:
        logger.warning(f"[FA-enrich] exception for {callsign}: {exc}")
    return {}


def _parse_opensky_flight(callsign: str, os_data: dict) -> dict:
    """Convert an OpenSky state-vector dict to the internal flight format."""
    alt_m = os_data.get("altitude_m") or 0
    vr = os_data.get("vertical_rate_ms") or 0

    if vr > 0.5:
        elev_change = "climbing"
    elif vr < -0.5:
        elev_change = "descending"
    else:
        elev_change = "level"

    return {
        "name":              callsign,
        "aircraft_type":     "N/A",  # filled by FA enrichment on HIGH transit
        "fa_flight_id":      "",
        "origin":            "N/A",
        "destination":       "N/A",
        "latitude":          os_data["lat"],
        "longitude":         os_data["lon"],
        "direction":         os_data.get("heading") or 0,
        "speed":             os_data.get("speed_kmh") or 0,
        "elevation":         float(alt_m),
        "elevation_feet":    int(alt_m * 3.28084),
        "elevation_change":  elev_change,
        "position_source":   "opensky",
        "position_age_s":    (
            round(_time.time() - os_data["last_contact"], 1)
            if os_data.get("last_contact") else None
        ),
        "icao24":            os_data.get("icao24", ""),
        "vertical_rate":     vr,
        "squawk":            os_data.get("squawk"),
        "on_ground":         os_data.get("on_ground", False),
    }
from src.position import (
    AreaBoundingBox,
    geographic_to_altaz,
    get_my_pos,
    predict_position,
)

EARTH = ASTRO_EPHEMERIS["earth"]

area_bbox = AreaBoundingBox(
    lat_lower_left=float(os.getenv("LAT_LOWER_LEFT", "0")),
    long_lower_left=float(os.getenv("LONG_LOWER_LEFT", "0")),
    lat_upper_right=float(os.getenv("LAT_UPPER_RIGHT", "0")),
    long_upper_right=float(os.getenv("LONG_UPPER_RIGHT", "0")),
)


def get_thresholds(altitude: float) -> Tuple[float, float]:
    """Receives target altitude and return the suggested threshold for both coordinates:
    altitude and azimuthal.
    """
    if Altitude.LOW(altitude):
        return (5.0, 10.0)
    elif Altitude.MEDIUM(altitude):
        return (10.0, 20.0)
    elif Altitude.MEDIUM_HIGH(altitude):
        return (10.0, 15.0)
    elif Altitude.HIGH(altitude):
        return (8.0, 180.0)

    logger.warning(f"{altitude=}")
    raise Exception("Given altitude is not valid!")


def get_possibility_level(
    altitude: float, alt_diff: float, az_diff: float
) -> str:
    """
    Determine transit possibility level based on angular separation.
    
    Assumes 1° target size for sun/moon (0.5° actual + 0.5° margin for near misses).
    Thresholds:
    - HIGH: ≤1° in both alt and az (direct transit very likely)
    - MEDIUM: ≤2° in both alt and az (near miss, worth recording)
    - LOW: ≤3° in both alt and az (possible distant transit)
    - UNLIKELY: >3° separation
    """
    possibility_level = PossibilityLevel.UNLIKELY
    
    # HIGH: Within 1° - direct transit very likely
    if alt_diff <= 1.0 and az_diff <= 1.0:
        possibility_level = PossibilityLevel.HIGH
    
    # MEDIUM: Within 2° - near miss worth recording
    elif alt_diff <= 2.0 and az_diff <= 2.0:
        possibility_level = PossibilityLevel.MEDIUM
    
    # LOW: Within 3° - possible distant transit
    elif alt_diff <= 3.0 and az_diff <= 3.0:
        possibility_level = PossibilityLevel.LOW
    
    return possibility_level.value



def check_transit(
    flight: dict,
    window_time: list,
    ref_datetime: datetime,
    my_position: Topos,
    target: CelestialObject,
    earth_ref,
    alt_threshold: float = 5.0,
    az_threshold: float = 10.0,
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
    response = None
    closest_approach = None  # Track closest approach even if threshold not met
    no_decreasing_count = 0
    update_response = False
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

        future_time = ref_datetime + timedelta(minutes=int(minute))

        # Convert future position of plane to alt-azimuthal coordinates
        # Support both 'elevation' (internal) and 'aircraft_elevation' (API response) field names
        flight_elevation = flight.get("elevation") or flight.get("aircraft_elevation", 0)
        future_alt, future_az = geographic_to_altaz(
            future_lat,
            future_lon,
            flight_elevation,
            earth_ref,
            my_position,
            future_time,
        )

        if idx > 0 and idx % _pts_per_min == 0:
            # Update target position every 1 minute of data points
            target.update_position(future_time)

        alt_diff = abs(future_alt - target.altitude.degrees)
        az_diff = abs(future_az - target.azimuthal.degrees)
        diff_combined = alt_diff + az_diff

        # Initialize closest_approach with first data point if not set
        if closest_approach is None:
            closest_approach = {
                "alt_diff": round(float(alt_diff), 3),
                "az_diff": round(float(az_diff), 3),
                "time": round(float(minute), 3),
                "target_alt": round(float(target.altitude.degrees), 2),
                "plane_alt": round(float(future_alt), 2),
                "target_az": round(float(target.azimuthal.degrees), 2),
                "plane_az": round(float(future_az), 2),
            }

        # Early exit: if diff has been consistently increasing for ~3 min, skip rest
        if no_decreasing_count >= _no_decrease_limit:
            logger.info(f"diff is increasing, stop checking, min={round(minute, 2)}")
            break

        if diff_combined < min_diff_combined:
            no_decreasing_count = 0
            min_diff_combined = diff_combined
            update_response = True
            # Always track closest approach
            closest_approach = {
                "alt_diff": round(float(alt_diff), 3),
                "az_diff": round(float(az_diff), 3),
                "time": round(float(minute), 3),
                "target_alt": round(float(target.altitude.degrees), 2),
                "plane_alt": round(float(future_alt), 2),
                "target_az": round(float(target.azimuthal.degrees), 2),
                "plane_az": round(float(future_az), 2),
            }
        else:
            no_decreasing_count += 1

        # Use user-provided thresholds (already passed as parameters)
        if future_alt > 0 and alt_diff < alt_threshold and az_diff < az_threshold:

            if update_response:
                response = {
                    "id": flight.get("name") or flight.get("id", ""),
                    "fa_flight_id": flight.get("fa_flight_id", ""),
                    "origin": flight.get("origin", ""),
                    "destination": flight.get("destination", ""),
                    "latitude": flight["latitude"],
                    "longitude": flight["longitude"],
                    "aircraft_elevation": flight.get("elevation") or flight.get("aircraft_elevation", 0),
                    "aircraft_type": flight.get("aircraft_type", "N/A"),
                    "speed": flight.get("speed", 0),
                    "alt_diff": round(float(alt_diff), 3),
                    "az_diff": round(float(az_diff), 3),
                    "time": round(float(minute), 3),
                    "target_alt": round(float(target.altitude.degrees), 2),
                    "plane_alt": round(float(future_alt), 2),
                    "target_az": round(float(target.azimuthal.degrees), 2),
                    "plane_az": round(float(future_az), 2),
                    "is_possible_transit": 1,
                    "possibility_level": get_possibility_level(
                        target.altitude.degrees, alt_diff, az_diff
                    ),
                    "elevation_change": CHANGE_ELEVATION.get(
                        flight.get("elevation_change"), None
                    ),
                    "direction": flight.get("direction", 0),
                    "waypoints": flight.get("waypoints", []),
                    "position_source": flight.get("position_source", "flightaware"),
                    "position_age_s": flight.get("position_age_s"),
                }
        update_response = False

    if response:
        return response

    # Return closest approach data even if threshold not met
    result = {
        "id": flight.get("name") or flight.get("id", ""),
        "fa_flight_id": flight.get("fa_flight_id", ""),
        "origin": flight.get("origin", ""),
        "destination": flight.get("destination", ""),
        "latitude": flight["latitude"],
        "longitude": flight["longitude"],
        "aircraft_elevation": flight.get("elevation") or flight.get("aircraft_elevation", 0),
        "aircraft_type": flight.get("aircraft_type", "N/A"),
        "speed": flight.get("speed", 0),
        "is_possible_transit": 0,
        "possibility_level": PossibilityLevel.UNLIKELY.value,
        "elevation_change": CHANGE_ELEVATION.get(flight.get("elevation_change"), None),
        "direction": flight.get("direction", 0),
        "waypoints": flight.get("waypoints", []),
        "position_source": flight.get("position_source", "flightaware"),
        "position_age_s": flight.get("position_age_s"),
    }
    
    # Include closest approach data if we found any
    if closest_approach:
        result.update(closest_approach)
    else:
        result.update({
            "alt_diff": None,
            "az_diff": None,
            "time": None,
            "target_alt": None,
            "plane_alt": None,
            "target_az": None,
            "plane_az": None,
        })
    
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
) -> Dict[str, Any]:
    API_KEY = get_aeroapi_key()

    # Ensure thresholds are floats (in case they're passed as strings from Flask)
    alt_threshold = float(alt_threshold)
    az_threshold = float(az_threshold)

    logger.info(f"{latitude=}, {longitude=}, {elevation=}")

    MY_POSITION = get_my_pos(
        lat=latitude,
        lon=longitude,
        elevation=elevation,
        base_ref=EARTH,
    )

    window_time = np.linspace(
        0, TOP_MINUTE, TOP_MINUTE * (NUM_SECONDS_PER_MIN // INTERVAL_IN_SECS)
    )
    logger.info(f"number of times to check for each flight: {len(window_time)}")
    # Get the local timezone using tzlocal
    local_timezone = get_localzone_name()
    naive_datetime_now = datetime.now()
    # Make the datetime object timezone-aware
    ref_datetime = naive_datetime_now.replace(tzinfo=ZoneInfo(local_timezone))

    celestial_obj = CelestialObject(name=target_name, observer_position=MY_POSITION)
    celestial_obj.update_position(ref_datetime=ref_datetime)
    current_target_coordinates = celestial_obj.get_coordinates()

    logger.info(celestial_obj.__str__())

    data = list()

    # Use custom bounding box if provided, otherwise use global default
    if custom_bbox:
        bbox = AreaBoundingBox(
            lat_lower_left=custom_bbox["lat_lower_left"],
            long_lower_left=custom_bbox["lon_lower_left"],
            lat_upper_right=custom_bbox["lat_upper_right"],
            long_upper_right=custom_bbox["lon_upper_right"],
        )
    else:
        bbox = area_bbox

    if current_target_coordinates["altitude"] > 0:
        if test_mode:
            raw_flight_data = load_existing_flight_data(TEST_DATA_PATH)
            logger.info("Loading existing flight data since is using TEST mode")
            flight_data = list()
            filtered_count = 0
            for flight in raw_flight_data["flights"]:
                parsed = parse_fligh_data(flight)
                lat, lon = parsed["latitude"], parsed["longitude"]
                if (bbox.lat_lower_left <= lat <= bbox.lat_upper_right and
                    bbox.long_lower_left <= lon <= bbox.long_upper_right):
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
                bbox.lat_lower_left, bbox.long_lower_left,
                bbox.lat_upper_right, bbox.long_upper_right,
            )
            if cached_data is not None:
                raw_flight_data = cached_data
                logger.info(f"Using cached FA data ({cache.get_stats()['hit_rate_percent']}% hit rate)")
            else:
                raw_flight_data = get_flight_data(bbox, API_URL, API_KEY)
                cache.set(
                    bbox.lat_lower_left, bbox.long_lower_left,
                    bbox.lat_upper_right, bbox.long_upper_right,
                    raw_flight_data,
                )

            flight_data = list()
            filtered_count = 0
            for flight in raw_flight_data["flights"]:
                parsed = parse_fligh_data(flight)
                lat, lon = parsed["latitude"], parsed["longitude"]
                if (bbox.lat_lower_left <= lat <= bbox.lat_upper_right and
                    bbox.long_lower_left <= lon <= bbox.long_upper_right):
                    flight_data.append(parsed)
                else:
                    filtered_count += 1
            if filtered_count > 0:
                logger.debug(f"Bbox filter (FA): {filtered_count} flights outside box")

        else:
            # ── Hybrid / OpenSky-only mode (default) ─────────────────────
            # Use OpenSky Network for all position data (~60s cache, free).
            # FA is only called on HIGH-probability transits for metadata.
            logger.info(f"[Data] {data_source} mode — using OpenSky as primary source")
            from src.opensky import fetch_opensky_positions
            try:
                opensky_data = fetch_opensky_positions(
                    bbox.lat_lower_left, bbox.long_lower_left,
                    bbox.lat_upper_right, bbox.long_upper_right,
                )
            except Exception as exc:
                logger.warning(f"[OpenSky] fetch failed: {exc}; flight_data will be empty")
                opensky_data = {}

            flight_data = []
            for callsign, os_pos in opensky_data.items():
                if os_pos.get("on_ground"):
                    continue  # skip ground traffic
                flight_data.append(_parse_opensky_flight(callsign, os_pos))

            logger.info(f"[OpenSky] {len(flight_data)} airborne aircraft in bbox")

        # ── Transit detection ─────────────────────────────────────────────
        for flight in flight_data:
            celestial_obj.update_position(ref_datetime=ref_datetime)

            result = check_transit(
                flight,
                window_time,
                ref_datetime,
                MY_POSITION,
                celestial_obj,
                EARTH,
                alt_threshold,
                az_threshold,
            )

            # ── FA enrichment for HIGH transits (hybrid mode only) ────────
            if (
                data_source not in ("fa-only",)
                and not test_mode
                and result.get("possibility_level") == PossibilityLevel.HIGH.value
                and API_KEY
            ):
                callsign = flight["name"]
                enrichment = _enrich_from_fa(callsign, API_KEY)
                if enrichment:
                    result.update(enrichment)
                    logger.info(f"[FA-enrich] enriched HIGH transit {callsign}")

            data.append(result)
            logger.info(data[-1])
    else:
        logger.debug(
            f"{target_name} target is under horizon, skipping checking for transits..."
        )

    return {"flights": data, "targetCoordinates": current_target_coordinates}


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
        for flight in flights:
            celestial_obj.update_position(ref_datetime=ref_datetime)
            
            data.append(
                check_transit(
                    flight,
                    window_time,
                    ref_datetime,
                    MY_POSITION,
                    celestial_obj,
                    EARTH,
                    alt_threshold,
                    az_threshold,
                )
            )
    
    return {"flights": data, "targetCoordinates": current_target_coordinates}
