"""
OpenSky Network position refresh module.

Queries the free OpenSky REST API for near-real-time aircraft positions
(~10-second latency vs FlightAware's 60–300 seconds).

API docs: https://openskynetwork.github.io/opensky-api/rest.html

Authentication (in priority order):
  1. OAuth2 client credentials — set OPENSKY_CLIENT_ID + OPENSKY_CLIENT_SECRET
  2. Basic auth (legacy)       — set OPENSKY_USERNAME + OPENSKY_PASSWORD
  3. Anonymous                 — no credentials (100 req/day limit)

Rate limits:
  Anonymous:  ~100 API credits/day
  Registered: ~400 API credits/day (free account)

This module only queries OpenSky when get_transits() is called, and caches
the bounding-box result for CACHE_TTL seconds to avoid redundant calls.
"""

import os
import time
from typing import Dict, Optional

import requests
from dotenv import load_dotenv

from src import logger

load_dotenv()

# How long (seconds) to cache a bounding-box query
CACHE_TTL: int = 60

# How long to pause after a 429 rate-limit response (seconds)
BACKOFF_429: int = 300

# Maximum age of an OpenSky position before we ignore it (seconds)
MAX_POSITION_AGE: int = 60

# Timeout for OpenSky HTTP requests
REQUEST_TIMEOUT: int = 5

_cache: Dict = {}           # {bbox_key: {"ts": float, "data": dict}}
_backoff_until: float = 0   # epoch time until which OpenSky requests are paused

# OAuth2 token cache
_token: Optional[str] = None
_token_expires_at: float = 0

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"


def _bbox_key(lat_ll, lon_ll, lat_ur, lon_ur) -> str:
    return f"{lat_ll:.3f},{lon_ll:.3f},{lat_ur:.3f},{lon_ur:.3f}"


def _get_bearer_token() -> Optional[str]:
    """Fetch (or return cached) OAuth2 bearer token using client credentials.

    Returns None if no credentials configured or token fetch fails.
    """
    global _token, _token_expires_at

    client_id     = os.getenv("OPENSKY_CLIENT_ID", "")
    client_secret = os.getenv("OPENSKY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None

    now = time.time()
    if _token and now < _token_expires_at - 30:  # 30s safety margin
        return _token

    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        _token = data["access_token"]
        _token_expires_at = now + data.get("expires_in", 3600)
        logger.debug(f"OpenSky token refreshed (expires in {data.get('expires_in', 3600)}s)")
        return _token
    except Exception as exc:
        logger.warning(f"OpenSky token fetch failed: {exc}")
        return None


def _get_auth() -> tuple:
    """Return (headers, auth) for the states/all request.

    Priority:
      1. OAuth2 bearer token (OPENSKY_CLIENT_ID + OPENSKY_CLIENT_SECRET)
      2. Legacy basic auth  (OPENSKY_USERNAME + OPENSKY_PASSWORD)
      3. Anonymous
    """
    token = _get_bearer_token()
    if token:
        return {"Authorization": f"Bearer {token}"}, None

    username = os.getenv("OPENSKY_USERNAME", "")
    password = os.getenv("OPENSKY_PASSWORD", "")
    if username:
        return {}, (username, password)

    return {}, None  # anonymous


def fetch_opensky_positions(
    lat_ll: float,
    lon_ll: float,
    lat_ur: float,
    lon_ur: float,
) -> Dict[str, dict]:
    """Return a dict of {callsign: position_dict} for all aircraft in the
    bounding box, using a short-lived cache.

    Returns an empty dict (or last cached value) on error so callers degrade
    gracefully. A 429 response triggers a BACKOFF_429-second pause.

    Each position_dict contains:
        lat, lon, altitude_m, speed_kmh, heading, vertical_rate_ms,
        last_contact (Unix timestamp), icao24, on_ground
    """
    global _backoff_until

    key = _bbox_key(lat_ll, lon_ll, lat_ur, lon_ur)
    now = time.time()

    # Return cached data if fresh enough
    cached = _cache.get(key)
    if cached and (now - cached["ts"]) < CACHE_TTL:
        logger.debug(f"OpenSky cache HIT (age {now - cached['ts']:.1f}s)")
        return cached["data"]

    # Respect backoff after a 429
    if now < _backoff_until:
        remaining = int(_backoff_until - now)
        logger.debug(f"OpenSky in backoff — {remaining}s remaining; using cached data")
        return cached["data"] if cached else {}

    extra_headers, auth = _get_auth()
    url = "https://opensky-network.org/api/states/all"
    params = {
        "lamin": lat_ll,
        "lomin": lon_ll,
        "lamax": lat_ur,
        "lomax": lon_ur,
    }

    try:
        resp = requests.get(
            url,
            params=params,
            headers=extra_headers,
            auth=auth,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            _backoff_until = now + BACKOFF_429
            logger.warning(
                f"OpenSky rate-limited (429) — pausing for {BACKOFF_429}s. "
                "Add OPENSKY_CLIENT_ID/SECRET to .env for higher limits."
            )
            return cached["data"] if cached else {}
        resp.raise_for_status()
        raw = resp.json()
    except requests.exceptions.Timeout:
        logger.warning("OpenSky request timed out")
        return cached["data"] if cached else {}
    except requests.exceptions.RequestException as exc:
        logger.warning(f"OpenSky request failed: {exc}")
        return cached["data"] if cached else {}
    except ValueError:
        logger.warning("OpenSky returned invalid JSON")
        return {}

    states = raw.get("states") or []
    result: Dict[str, dict] = {}

    for s in states:
        # OpenSky state vector layout (index → field):
        # 0 icao24, 1 callsign, 2 origin_country, 3 time_position,
        # 4 last_contact, 5 lon, 6 lat, 7 baro_altitude, 8 on_ground,
        # 9 velocity(m/s), 10 true_track(deg), 11 vertical_rate(m/s),
        # 12 sensors, 13 geo_altitude, 14 squawk, 15 spi, 16 position_source
        if len(s) < 11:
            continue

        callsign = (s[1] or "").strip()
        if not callsign:
            continue

        last_contact = s[4]
        if last_contact is None or (now - last_contact) > MAX_POSITION_AGE:
            continue  # stale — skip

        lon = s[5]
        lat = s[6]
        if lat is None or lon is None:
            continue

        baro_alt = s[7]   # metres (may be None)
        geo_alt  = s[13]  # metres (may be None)
        altitude_m = geo_alt or baro_alt  # prefer geometric altitude

        velocity_ms = s[9]   # m/s (may be None)
        true_track   = s[10]  # degrees (may be None)
        vert_rate    = s[11]  # m/s (may be None)
        on_ground    = s[8]

        result[callsign] = {
            "icao24":           s[0],
            "lat":              float(lat),
            "lon":              float(lon),
            "altitude_m":       float(altitude_m) if altitude_m is not None else None,
            "speed_kmh":        float(velocity_ms) * 3.6 if velocity_ms is not None else None,
            "heading":          float(true_track) if true_track is not None else None,
            "vertical_rate_ms": float(vert_rate) if vert_rate is not None else None,
            "last_contact":     float(last_contact),
            "on_ground":        bool(on_ground),
            "source":           "opensky",
        }

    logger.info(f"OpenSky: {len(result)} aircraft in bbox (of {len(states)} states)")
    _cache[key] = {"ts": now, "data": result}
    return result
