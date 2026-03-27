from dataclasses import dataclass
from datetime import datetime
from math import asin, atan2, cos, degrees, radians, sin, sqrt
from typing import List, Optional, Tuple

from skyfield.api import wgs84

from src.constants import EARTH_RADIOUS, EARTH_TIMESCALE, NUM_MINUTES_PER_HOUR


@dataclass
class AreaBoundingBox:
    lat_lower_left: float
    long_lower_left: float
    lat_upper_right: float
    long_upper_right: float


def predict_position(
    lat: float, lon: float, speed: float, direction: float, minutes: float
) -> tuple:
    """Compute the future latitude and longitude of a plane given its current coordinates,
    speed, direction, and the time ahead to know the position.

    Parameters
    ----------
    lat : float
        Current latitude of the plane in decimal degrees.
    lon : float
        Current longitude of the plane in decimal degrees.
    speed : float
        Ground speed of the plane in kilometers per hour (km/h).
    direction : float
        Direction of the plane in degrees from North (0° to 360°).
    minutes : float
        Time ahead in minutes to predict the future position.

    Returns
    -------
    new_lat : float
        Predicted future latitude of the plane in decimal degrees.
    new_lon : float
        Predicted future longitude of the plane in decimal degrees.

    Notes
    -----
    This function uses the Haversine formula to calculate the new position of the plane.
    The following mathematical steps are involved:

    1. Calculate the distance traveled in kilometers.
       Distance (km) = (Speed (km/h) / 60) * Minutes

    2. Convert the direction (bearing) from degrees to radians.
       Bearing (radians) = Direction (degrees) * π / 180

    3. Compute the new latitude using the formula:
       new_lat = asin(sin(lat) * cos(d/R) + cos(lat) * sin(d/R) * cos(bearing))

    4. Compute the new longitude using the formula:
       new_lon = lon + atan2(sin(bearing) * sin(d/R) * cos(lat), cos(d/R) - sin(lat) * sin(new_lat))

    where:
    - lat and lon are the initial latitude and longitude in radians.
    - d is the distance traveled.
    - R is the Earth's radius (mean radius = 6,371 km).
    """
    distance = (speed / NUM_MINUTES_PER_HOUR) * minutes

    # Convert direction to radians
    bearing = radians(direction)

    lat_rads = radians(lat)
    ratio_d_r = distance / EARTH_RADIOUS

    # Calculate new latitude
    new_lat = degrees(
        asin(
            sin(lat_rads) * cos(ratio_d_r)
            + cos(lat_rads) * sin(ratio_d_r) * cos(bearing)
        )
    )
    # Calculate new longitude
    new_lon = degrees(
        radians(lon)
        + atan2(
            sin(bearing) * sin(ratio_d_r) * cos(lat_rads),
            cos(ratio_d_r) - sin(lat_rads) * sin(radians(new_lat)),
        )
    )

    return new_lat, new_lon


def geographic_to_altaz(
    lat: float, lon: float, elevation, earth_ref, your_location, future_time: datetime
):
    time_ = EARTH_TIMESCALE.from_datetime(future_time)
    plane_location = earth_ref + wgs84.latlon(lat, lon, elevation_m=elevation)
    plane_alt, plane_az, _ = (plane_location - your_location).at(time_).altaz()

    return plane_alt.degrees, plane_az.degrees


def get_my_pos(lat, lon, elevation, base_ref):
    return base_ref + wgs84.latlon(lat, lon, elevation_m=elevation)


def transit_corridor_bbox(
    obs_lat: float,
    obs_lon: float,
    target_alt_deg: float,
    target_az_deg: float,
    aircraft_altitude_m: float = 10_000.0,
    time_window_minutes: float = 15.0,
    max_speed_kmh: float = 950.0,
) -> AreaBoundingBox:
    """Compute a geographic bounding box that covers all aircraft that could
    possibly transit the target (Sun/Moon) within the prediction window.

    The box is centred on the ground point directly below where an aircraft
    flying at *aircraft_altitude_m* would need to be to appear in front of
    the target right now (the "transit ground point").  The box is expanded
    by the maximum distance a fast jet could travel in *time_window_minutes*
    so that incoming aircraft are included.

    Parameters
    ----------
    obs_lat, obs_lon : float
        Observer position in decimal degrees.
    target_alt_deg : float
        Current altitude of the celestial target in degrees above horizon.
    target_az_deg : float
        Current azimuth of the target in degrees (0 = north, clockwise).
    aircraft_altitude_m : float
        Assumed cruise altitude for ground-point projection (default 10 km).
    time_window_minutes : float
        Prediction window; box is expanded so incoming aircraft are captured.
    max_speed_kmh : float
        Maximum aircraft speed used to expand the box.

    Returns
    -------
    AreaBoundingBox
        Bounding box guaranteed to contain all candidate aircraft.
    """
    # Distance from observer to the transit ground point.
    # tan(alt) = aircraft_altitude / ground_distance  →  d = alt / tan(alt)
    # Clamp alt to avoid division by zero / absurdly large distances.
    # C4: raised minimum from 3° to 5° — below 5° the bbox geometry degrades
    # significantly and including aircraft from the full hemisphere is safer.
    alt_clamped = max(target_alt_deg, 5.0)
    from math import tan

    ground_dist_km = (aircraft_altitude_m / 1000.0) / tan(radians(alt_clamped))
    # Cap at ~500 km (covers low-altitude targets without going global)
    ground_dist_km = min(ground_dist_km, 500.0)

    # Project along target azimuth to get transit ground point
    bearing = radians(target_az_deg)
    R = EARTH_RADIOUS
    ratio = ground_dist_km / R
    obs_lat_r = radians(obs_lat)
    obs_lon_r = radians(obs_lon)

    tgp_lat_r = asin(
        sin(obs_lat_r) * cos(ratio) + cos(obs_lat_r) * sin(ratio) * cos(bearing)
    )
    tgp_lon_r = obs_lon_r + atan2(
        sin(bearing) * sin(ratio) * cos(obs_lat_r),
        cos(ratio) - sin(obs_lat_r) * sin(tgp_lat_r),
    )
    tgp_lat = degrees(tgp_lat_r)
    tgp_lon = degrees(tgp_lon_r)

    # Radius to expand: distance a fast jet can cover in the time window
    travel_km = max_speed_kmh * (time_window_minutes / 60.0)
    # Also include the ground_dist uncertainty (aircraft may be at 8–12 km alt)
    radius_km = travel_km + ground_dist_km * 0.2  # 20% alt uncertainty margin

    # C4: low-elevation azimuth-margin buffer
    # When the target is near the horizon, the azimuth uncertainty from pointing
    # error or atmospheric refraction maps to a large lateral offset at the
    # transit ground point.  Scale the lateral (lon) extent by an additional
    # factor: azimuth_scale = max(1.0, sin(15°)/sin(alt)) capped at 3×.
    # This widens the bbox east/west to capture aircraft on skewed approach
    # paths that a nominal-azimuth box would miss.
    if target_alt_deg < 15.0:
        az_scale = min(
            3.0, sin(radians(15.0)) / max(sin(radians(target_alt_deg)), 0.087)
        )
    else:
        az_scale = 1.0

    radius_km = min(radius_km, 600.0)

    # Convert radius to degrees (approximate, good enough for a bbox)
    lat_delta = radius_km / 111.32
    lon_delta = (radius_km * az_scale) / (111.32 * cos(radians(tgp_lat)) + 1e-9)

    return AreaBoundingBox(
        lat_lower_left=tgp_lat - lat_delta,
        long_lower_left=tgp_lon - lon_delta,
        lat_upper_right=tgp_lat + lat_delta,
        long_upper_right=tgp_lon + lon_delta,
    )


def compute_track_velocity(
    track_positions: List[dict],
) -> Optional[Tuple[float, float]]:
    """Derive true ground speed and heading from the last two track fixes.

    Uses the most recent pair of positions from FlightAware track data to
    compute the actual velocity vector, which captures recent turns and
    speed changes that the reported groundspeed/heading may lag behind.

    Parameters
    ----------
    track_positions : list of dict
        Each dict must contain 'timestamp' (Unix seconds OR ISO-8601 string),
        'latitude', and 'longitude' keys (FlightAware track format).
        At least two entries required.

    Returns
    -------
    (speed_kmh, heading_deg) tuple, or None if insufficient / invalid data.
    """
    if not track_positions or len(track_positions) < 2:
        return None

    def _to_unix(ts) -> Optional[float]:
        """Convert timestamp to Unix seconds regardless of input format."""
        if ts is None:
            return None
        try:
            return float(ts)
        except (TypeError, ValueError):
            pass
        try:
            from datetime import datetime

            s = str(ts).replace("Z", "+00:00")
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return None

    # Sort by timestamp ascending; take last two valid fixes
    valid = []
    for p in track_positions:
        ts = _to_unix(p.get("timestamp"))
        if (
            ts is not None
            and p.get("latitude") is not None
            and p.get("longitude") is not None
        ):
            valid.append({**p, "_ts": ts})

    if len(valid) < 2:
        return None

    valid_sorted = sorted(valid, key=lambda p: p["_ts"])
    p1 = valid_sorted[-2]
    p2 = valid_sorted[-1]

    dt_s = p2["_ts"] - p1["_ts"]
    if dt_s <= 0:
        return None

    lat1 = radians(float(p1["latitude"]))
    lat2 = radians(float(p2["latitude"]))
    lon1 = radians(float(p1["longitude"]))
    lon2 = radians(float(p2["longitude"]))

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    # Haversine distance (km)
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    dist_km = 2 * EARTH_RADIOUS * asin(sqrt(a))

    speed_kmh = dist_km / (dt_s / 3600.0)

    # Bearing (degrees, 0 = north, clockwise)
    heading_rad = atan2(
        sin(dlon) * cos(lat2),
        cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon),
    )
    heading_deg = (degrees(heading_rad) + 360) % 360

    return speed_kmh, heading_deg
