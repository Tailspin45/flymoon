"""
Multi-source ADS-B position aggregator.

Queries OpenSky Network, ADSB-One (api.adsb.one), adsb.lol (api.adsb.lol),
adsb.fi opendata (opendata.adsb.fi/api — see their terms: personal /
non-commercial, cite adsb.fi), ADS-B Exchange (adsbexchange.com), and
optionally a local RTL-SDR / dump1090 / tar1090 receiver in parallel, then
merges results by callsign (most-recent position wins).

All sources return a dict of {callsign: position_dict} compatible with
the OpenSky-format consumed by ``_parse_opensky_flight()`` in transit.py.

Environment variables
---------------------
ADSB_ONE_ENABLED     "true" / "false"  (default true — no key needed)
ADSB_LOL_ENABLED     "true" / "false"  (default true — api.adsb.lol, no key)
ADSB_FI_ENABLED      "true" / "false"  (default true — opendata.adsb.fi, no key)
ADSBX_API_KEY        UUID key from adsbexchange.com api-auth header (optional)
ADSBX_ENABLED        "true" / "false"  (default true if ADSBX_API_KEY is set)
ADSB_LOCAL_URL       URL of a local dump1090/tar1090 aircraft.json endpoint
ADSB_LOCAL_ENABLED   "true" / "false"  (default true if ADSB_LOCAL_URL is set)
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from math import atan2, cos, radians, sin, sqrt
from typing import Dict, Optional

import requests

from src import logger

# ---------------------------------------------------------------------------
# Request configuration
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT: int = 5        # seconds per individual HTTP request
MULTI_SOURCE_WALL_TIMEOUT = 12  # max wall-clock seconds for the whole parallel fetch

# ADSB-One (airplanes.live) free public API
ADSB_ONE_BASE = "https://api.adsb.one"
ADSB_ONE_MAX_RADIUS_NM = 250

ADSB_LOL_BASE = "https://api.adsb.lol"
ADSB_LOL_MAX_RADIUS_NM = 250

# https://github.com/adsbfi/opendata/blob/main/README.md — v3 lat/lon/dist (NM)
ADSB_FI_BASE = "https://opendata.adsb.fi/api"
ADSB_FI_MAX_RADIUS_NM = 250

# ADS-B Exchange — requires api-auth UUID key
ADSBX_BASE = "https://adsbexchange.com/api/aircraft"
ADSBX_MAX_RADIUS_NM = 100

# Backoff state (per-source)
_adsb_one_backoff_until: float = 0.0
_adsb_lol_backoff_until: float = 0.0
_adsb_fi_backoff_until: float = 0.0
_adsbx_backoff_until: float = 0.0
_local_backoff_until: float = 0.0
BACKOFF_SECONDS: int = 60

# Short-lived cache for fetch_multi_source_positions — avoids a double fetch
# when the /flights endpoint calls get_transits() for both sun and moon within
# the same request cycle.  TTL is intentionally short (20 s).
_multi_source_cache: Dict[tuple, dict] = {}
_multi_source_cache_ts: Dict[tuple, float] = {}
MULTI_SOURCE_CACHE_TTL: int = 20  # seconds


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def _bbox_to_center_radius(
    lat_ll: float, lon_ll: float, lat_ur: float, lon_ur: float
) -> tuple:
    """Return (center_lat, center_lon, radius_km) for the given bounding box."""
    clat = (lat_ll + lat_ur) / 2
    clon = (lon_ll + lon_ur) / 2
    R = 6371.0
    lat1, lon1 = radians(clat), radians(clon)
    lat2, lon2 = radians(lat_ur), radians(lon_ur)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    dist_km = R * 2 * atan2(sqrt(a), sqrt(1 - a))
    return clat, clon, dist_km


# ---------------------------------------------------------------------------
# Shared readsb v2 parser (ADSB-One / ADSBX / local dump1090 all use this format)
# ---------------------------------------------------------------------------

def _parse_readsb_aircraft(ac: dict, now_ts: float) -> Optional[dict]:
    """Parse a readsb v2 aircraft object into the internal position-dict format.

    Unit conversions (readsb v2 → internal):
        alt_baro / alt_geom : feet     → metres  (* 0.3048)
        gs                  : knots    → km/h    (* 1.852)
        baro_rate/geom_rate : ft/min   → m/s     (* 0.00508)
        seen_pos            : seconds ago (same convention as OpenSky last_contact)

    The returned dict is compatible with OpenSky's position dict so that
    ``_parse_opensky_flight()`` in transit.py can process it unchanged.
    """
    hex_id = (ac.get("hex") or "").strip().lower()
    callsign = (ac.get("flight") or "").strip()
    # Reject callsigns that are clearly corrupted (leading '-', pure hex that looks
    # like a negated ICAO address, or non-printable characters).
    if callsign and (callsign.startswith("-") or not callsign.isprintable()):
        callsign = ""
    if not callsign:
        callsign = hex_id.upper() if hex_id else None
    if not callsign:
        return None

    lat = ac.get("lat")
    lon = ac.get("lon")
    if lat is None or lon is None:
        return None

    # Position age — readsb uses seconds-ago, same staleness gate as OpenSky (30s)
    seen_pos = ac.get("seen_pos")
    if seen_pos is None:
        seen_pos = ac.get("seen") or 0.0
    seen_pos = float(seen_pos)
    if seen_pos > 30:
        return None

    last_contact = now_ts - seen_pos

    # Altitude: prefer geometric, fall back to barometric; feet → metres
    alt_feet = ac.get("alt_geom") if ac.get("alt_geom") not in (None, "ground", "grnd") else None
    if alt_feet is None:
        alt_feet = ac.get("alt_baro") if ac.get("alt_baro") not in (None, "ground", "grnd") else None
    altitude_m = float(alt_feet) * 0.3048 if alt_feet is not None else None

    # Ground detection
    on_ground = bool(
        ac.get("ground")
        or ac.get("alt_baro") in ("ground", "grnd")
        or ac.get("alt_geom") in ("ground", "grnd")
    )
    if on_ground:
        return None  # filter out ground traffic immediately

    # Speed: knots → km/h
    gs_kt = ac.get("gs")
    speed_kmh = float(gs_kt) * 1.852 if gs_kt is not None else None

    # Vertical rate: ft/min → m/s  (prefer geometric rate)
    vr_ftm = ac.get("geom_rate") if ac.get("geom_rate") is not None else ac.get("baro_rate")
    vert_rate_ms = float(vr_ftm) * 0.00508 if vr_ftm is not None else None

    # Emitter category: "A3" → integer 3
    cat_str = ac.get("category")
    category: Optional[int] = None
    if cat_str and len(cat_str) > 1:
        try:
            category = int(cat_str[1])
        except (ValueError, IndexError):
            pass

    source_type = ac.get("type", "adsb_icao")
    if "adsb" in source_type:
        pos_source = "adsb"
    elif "mlat" in source_type:
        pos_source = "mlat"
    else:
        pos_source = "other"

    return {
        "icao24": hex_id,
        "lat": float(lat),
        "lon": float(lon),
        "altitude_m": altitude_m,
        "speed_kmh": speed_kmh,
        "heading": float(ac["track"]) if ac.get("track") is not None else None,
        "vertical_rate_ms": vert_rate_ms,
        "last_contact": last_contact,
        "on_ground": False,  # already filtered above
        "squawk": ac.get("squawk"),
        "spi": False,
        "category": category,
        "origin_country": None,
        "position_source": pos_source,
        # Extra metadata (ignored by _parse_opensky_flight but useful for enrichment)
        "_registration": ac.get("r"),
        "_aircraft_type_code": ac.get("t"),
        "_callsign": callsign,
    }


# ---------------------------------------------------------------------------
# Individual source fetchers
# ---------------------------------------------------------------------------

def _fetch_adsb_one(lat_ll: float, lon_ll: float, lat_ur: float, lon_ur: float) -> Dict[str, dict]:
    """Fetch positions from ADSB-One (api.adsb.one). Free, no authentication."""
    global _adsb_one_backoff_until

    if os.getenv("ADSB_ONE_ENABLED", "true").lower() not in ("true", "1", "yes"):
        return {}

    now = time.time()
    if now < _adsb_one_backoff_until:
        logger.debug(f"[ADSB-One] In backoff ({int(_adsb_one_backoff_until - now)}s remaining)")
        return {}

    clat, clon, dist_km = _bbox_to_center_radius(lat_ll, lon_ll, lat_ur, lon_ur)
    radius_nm = min(dist_km * 0.539957 * 1.1, ADSB_ONE_MAX_RADIUS_NM)

    url = f"{ADSB_ONE_BASE}/v2/point/{clat:.4f}/{clon:.4f}/{radius_nm:.0f}"
    raw = None
    for verify_ssl in (True, False):  # retry without SSL verify on TLS decode errors
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, verify=verify_ssl)
            if resp.status_code == 429:
                _adsb_one_backoff_until = time.time() + BACKOFF_SECONDS
                logger.warning(f"[ADSB-One] Rate limited (429) — backoff {BACKOFF_SECONDS}s")
                return {}
            resp.raise_for_status()
            raw = resp.json()
            break
        except requests.exceptions.SSLError as exc:
            if not verify_ssl:
                logger.warning(f"[ADSB-One] SSL error even without verify — skipping: {exc}")
                return {}
            logger.debug("[ADSB-One] SSL error — retrying without verify")
        except requests.exceptions.Timeout:
            logger.warning("[ADSB-One] Request timed out")
            return {}
        except Exception as exc:
            logger.warning(f"[ADSB-One] Request failed: {exc}")
            return {}
    if raw is None:
        return {}

    aircraft = raw.get("ac") or raw.get("aircraft") or []
    now_ts = float(raw.get("now") or time.time())

    result: Dict[str, dict] = {}
    for ac in aircraft:
        parsed = _parse_readsb_aircraft(ac, now_ts)
        if parsed is None:
            continue
        callsign = parsed["_callsign"]
        result[callsign] = parsed

    logger.info(f"[ADSB-One] {len(result)} aircraft (radius={radius_nm:.0f} nm)")
    return result


def _fetch_adsb_lol(lat_ll: float, lon_ll: float, lat_ur: float, lon_ur: float) -> Dict[str, dict]:
    """Fetch positions from api.adsb.lol (readsb v2 /point — same shape as ADSB-One)."""
    global _adsb_lol_backoff_until

    if os.getenv("ADSB_LOL_ENABLED", "true").lower() not in ("true", "1", "yes"):
        return {}

    now = time.time()
    if now < _adsb_lol_backoff_until:
        logger.debug(f"[adsb.lol] In backoff ({int(_adsb_lol_backoff_until - now)}s remaining)")
        return {}

    clat, clon, dist_km = _bbox_to_center_radius(lat_ll, lon_ll, lat_ur, lon_ur)
    radius_nm = min(dist_km * 0.539957 * 1.1, ADSB_LOL_MAX_RADIUS_NM)

    url = f"{ADSB_LOL_BASE}/v2/point/{clat:.4f}/{clon:.4f}/{radius_nm:.0f}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            _adsb_lol_backoff_until = time.time() + BACKOFF_SECONDS
            logger.warning(f"[adsb.lol] Rate limited (429) — backoff {BACKOFF_SECONDS}s")
            return {}
        resp.raise_for_status()
        raw = resp.json()
    except requests.exceptions.Timeout:
        logger.warning("[adsb.lol] Request timed out")
        return {}
    except Exception as exc:
        logger.warning(f"[adsb.lol] Request failed: {exc}")
        return {}

    aircraft = raw.get("ac") or raw.get("aircraft") or []
    now_ts = float(raw.get("now") or time.time())

    result: Dict[str, dict] = {}
    for ac in aircraft:
        parsed = _parse_readsb_aircraft(ac, now_ts)
        if parsed is None:
            continue
        callsign = parsed["_callsign"]
        result[callsign] = parsed

    logger.info(f"[adsb.lol] {len(result)} aircraft (radius={radius_nm:.0f} nm)")
    return result


def _fetch_adsb_fi(lat_ll: float, lon_ll: float, lat_ur: float, lon_ur: float) -> Dict[str, dict]:
    """Fetch positions from adsb.fi opendata API (readsb-compatible JSON).

    See https://opendata.adsb.fi/api/ — v3/lat/lon/dist with dist in NM.
    """
    global _adsb_fi_backoff_until

    if os.getenv("ADSB_FI_ENABLED", "true").lower() not in ("true", "1", "yes"):
        return {}

    now = time.time()
    if now < _adsb_fi_backoff_until:
        logger.debug(f"[adsb.fi] In backoff ({int(_adsb_fi_backoff_until - now)}s remaining)")
        return {}

    clat, clon, dist_km = _bbox_to_center_radius(lat_ll, lon_ll, lat_ur, lon_ur)
    dist_nm = min(dist_km * 0.539957 * 1.1, ADSB_FI_MAX_RADIUS_NM)

    url = f"{ADSB_FI_BASE}/v3/lat/{clat:.4f}/lon/{clon:.4f}/dist/{dist_nm:.0f}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            _adsb_fi_backoff_until = time.time() + BACKOFF_SECONDS
            logger.warning(f"[adsb.fi] Rate limited (429) — backoff {BACKOFF_SECONDS}s")
            return {}
        resp.raise_for_status()
        raw = resp.json()
    except requests.exceptions.Timeout:
        logger.warning("[adsb.fi] Request timed out")
        return {}
    except Exception as exc:
        logger.warning(f"[adsb.fi] Request failed: {exc}")
        return {}

    aircraft = raw.get("ac") or raw.get("aircraft") or []
    now_ts = float(raw.get("now") or time.time())

    result: Dict[str, dict] = {}
    for ac in aircraft:
        parsed = _parse_readsb_aircraft(ac, now_ts)
        if parsed is None:
            continue
        callsign = parsed["_callsign"]
        result[callsign] = parsed

    logger.info(f"[adsb.fi] {len(result)} aircraft (radius={dist_nm:.0f} nm)")
    return result


def _fetch_adsbexchange(lat_ll: float, lon_ll: float, lat_ur: float, lon_ur: float) -> Dict[str, dict]:
    """Fetch positions from ADS-B Exchange. Requires ADSBX_API_KEY env var."""
    global _adsbx_backoff_until

    api_key = os.getenv("ADSBX_API_KEY", "").strip()
    if not api_key:
        return {}
    if os.getenv("ADSBX_ENABLED", "true").lower() not in ("true", "1", "yes"):
        return {}

    now = time.time()
    if now < _adsbx_backoff_until:
        logger.debug(f"[ADSBX] In backoff ({int(_adsbx_backoff_until - now)}s remaining)")
        return {}

    clat, clon, dist_km = _bbox_to_center_radius(lat_ll, lon_ll, lat_ur, lon_ur)
    dist_nm = min(dist_km * 0.539957 * 1.1, ADSBX_MAX_RADIUS_NM)

    url = f"{ADSBX_BASE}/lat/{clat:.4f}/lon/{clon:.4f}/dist/{dist_nm:.0f}/"
    headers = {"api-auth": api_key}
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            _adsbx_backoff_until = time.time() + BACKOFF_SECONDS
            logger.warning(f"[ADSBX] Rate limited (429) — backoff {BACKOFF_SECONDS}s")
            return {}
        if resp.status_code == 403:
            logger.warning("[ADSBX] Authentication failed (403) — check ADSBX_API_KEY")
            return {}
        resp.raise_for_status()
        raw = resp.json()
    except requests.exceptions.Timeout:
        logger.warning("[ADSBX] Request timed out")
        return {}
    except Exception as exc:
        logger.warning(f"[ADSBX] Request failed: {exc}")
        return {}

    aircraft = raw.get("ac") or []
    now_ts = float(raw.get("now") or time.time())

    result: Dict[str, dict] = {}
    for ac in aircraft:
        parsed = _parse_readsb_aircraft(ac, now_ts)
        if parsed is None:
            continue
        callsign = parsed["_callsign"]
        result[callsign] = parsed

    logger.info(f"[ADSBX] {len(result)} aircraft (radius={dist_nm:.0f} nm)")
    return result


def _fetch_adsb_local(lat_ll: float, lon_ll: float, lat_ur: float, lon_ur: float) -> Dict[str, dict]:
    """Fetch from a local RTL-SDR / dump1090 / tar1090 receiver.

    Reads the standard ``aircraft.json`` endpoint served by dump1090, tar1090,
    readsb, and compatible software.

    Configure with:
        ADSB_LOCAL_URL=http://192.168.x.y/data/aircraft.json
    """
    global _local_backoff_until

    url = os.getenv("ADSB_LOCAL_URL", "").strip()
    if not url:
        return {}
    if os.getenv("ADSB_LOCAL_ENABLED", "true").lower() not in ("true", "1", "yes"):
        return {}

    now = time.time()
    if now < _local_backoff_until:
        return {}

    try:
        resp = requests.get(url, timeout=3)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        _local_backoff_until = time.time() + BACKOFF_SECONDS
        logger.warning(f"[ADS-B Local] Request failed: {exc} — backoff {BACKOFF_SECONDS}s")
        return {}

    # dump1090 / tar1090 / readsb: {"aircraft": [...]} or {"ac": [...]}
    aircraft = raw.get("aircraft") or raw.get("ac") or []
    now_ts = float(raw.get("now") or time.time())

    result: Dict[str, dict] = {}
    for ac in aircraft:
        parsed = _parse_readsb_aircraft(ac, now_ts)
        if parsed is None:
            continue
        # Filter to bbox (local receiver covers a wide area)
        if not (lat_ll <= parsed["lat"] <= lat_ur and lon_ll <= parsed["lon"] <= lon_ur):
            continue
        callsign = parsed["_callsign"]
        result[callsign] = parsed

    logger.info(f"[ADS-B Local] {len(result)} aircraft in bbox from {url}")
    return result


# ---------------------------------------------------------------------------
# Multi-source entry point
# ---------------------------------------------------------------------------

def fetch_multi_source_positions(
    lat_ll: float,
    lon_ll: float,
    lat_ur: float,
    lon_ur: float,
) -> Dict[str, dict]:
    """Query all enabled ADS-B sources concurrently and merge by callsign.

    Merge strategy: for each callsign, keep the position with the largest
    ``last_contact`` Unix timestamp (i.e. most recently updated).

    Returns a dict of {callsign: position_dict} in the same format as
    ``src.opensky.fetch_opensky_positions()``.
    """
    # When /flights calls get_transits() for both sun and moon in the same
    # request cycle they use the same bbox; serve the second call from cache.
    cache_key = (round(lat_ll, 4), round(lon_ll, 4), round(lat_ur, 4), round(lon_ur, 4))
    now_ts = time.time()
    cached_ts = _multi_source_cache_ts.get(cache_key, 0.0)
    if now_ts - cached_ts < MULTI_SOURCE_CACHE_TTL and cache_key in _multi_source_cache:
        logger.debug(f"[MultiSource] Returning cached result ({now_ts - cached_ts:.1f}s old)")
        return _multi_source_cache[cache_key]

    from src.opensky import fetch_opensky_positions

    # Each source is a no-arg lambda so failures in one don't block others
    sources = {
        "opensky": lambda: fetch_opensky_positions(lat_ll, lon_ll, lat_ur, lon_ur),
        "adsb_one": lambda: _fetch_adsb_one(lat_ll, lon_ll, lat_ur, lon_ur),
        "adsb_lol": lambda: _fetch_adsb_lol(lat_ll, lon_ll, lat_ur, lon_ur),
        "adsb_fi": lambda: _fetch_adsb_fi(lat_ll, lon_ll, lat_ur, lon_ur),
        "adsbx": lambda: _fetch_adsbexchange(lat_ll, lon_ll, lat_ur, lon_ur),
        "local": lambda: _fetch_adsb_local(lat_ll, lon_ll, lat_ur, lon_ur),
    }

    merged: Dict[str, dict] = {}
    source_counts: Dict[str, int] = {}

    # cancel_futures=True (Py 3.9+) drops pending work on timeout;
    # fall back gracefully on older Python.
    import sys
    _shutdown_kwargs = {"wait": False}
    if sys.version_info >= (3, 9):
        _shutdown_kwargs["cancel_futures"] = True

    pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="adsb-src")
    try:
        futures = {pool.submit(fn): name for name, fn in sources.items()}
        for future in as_completed(futures, timeout=MULTI_SOURCE_WALL_TIMEOUT):
            name = futures[future]
            try:
                batch = future.result()
            except Exception as exc:
                logger.warning(f"[MultiSource] {name} raised: {exc}")
                batch = {}

            source_counts[name] = len(batch)
            for callsign, pos in batch.items():
                existing = merged.get(callsign)
                if existing is None:
                    merged[callsign] = pos
                else:
                    # Prefer the fresher position
                    if (pos.get("last_contact") or 0) > (existing.get("last_contact") or 0):
                        merged[callsign] = pos
    except FuturesTimeoutError:
        completed = [n for f, n in futures.items() if f.done()]
        pending   = [n for f, n in futures.items() if not f.done()]
        logger.warning(
            f"[MultiSource] Wall-clock timeout ({MULTI_SOURCE_WALL_TIMEOUT}s) — "
            f"completed: {completed}, still pending (dropped): {pending}"
        )
    finally:
        pool.shutdown(**_shutdown_kwargs)

    active = [f"{n}={c}" for n, c in source_counts.items() if c > 0]
    logger.info(
        f"[MultiSource] {len(merged)} unique aircraft — "
        + (", ".join(active) if active else "no data from any source")
    )
    _multi_source_cache[cache_key] = merged
    _multi_source_cache_ts[cache_key] = time.time()
    return merged
