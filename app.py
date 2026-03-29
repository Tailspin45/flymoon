"""
Flymoon - Aircraft Transit Tracker
===================================

Main Flask application for tracking aircraft transiting celestial objects (Sun/Moon).

Features:
- Real-time flight data from FlightAware AeroAPI
- Celestial position calculations using Skyfield
- Interactive web UI with map visualization
- Automatic telescope control for transit photography (Seestar S50)
- Telegram notifications for possible transits

Routes:
- / - Main web interface
- /flights - API endpoint for flight data queries
- /flights/<id>/route - Flight route information
- /flights/<id>/track - Flight historical track
- /telescope/* - Telescope control endpoints

Environment Variables (see SETUP.md):
- AEROAPI_API_KEY - FlightAware API key (required)
- TELEGRAM_BOT_TOKEN - Telegram bot token (optional)
- TELEGRAM_CHAT_ID - Telegram chat ID (optional)
- SEESTAR_IP - Telescope IP address (optional)

@author Flymoon Team
@version 1.0
"""

import argparse
import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request
from tzlocal import get_localzone_name

from src.constants import (
    ASTRO_EPHEMERIS,
    POSSIBLE_TRANSITS_LOGFILENAME,
    get_aeroapi_key,
)

# SETUP
load_dotenv()


def _flymoon_code_revision() -> str:
    """Short git SHA for “am I running the code I think I am?” (see /config)."""
    rev = os.getenv("FLYMOON_REVISION", "").strip()
    if rev:
        return rev
    try:
        import subprocess

        root = os.path.dirname(os.path.abspath(__file__))
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


from src import logger, telescope_routes
from src.astro import CelestialObject, get_rise_set_times
from src.config_wizard import ConfigWizard
from src.constants import PossibilityLevel
from src.flight_cache import get_cache
from src.flight_data import save_possible_transits, sort_results
from src.position import compute_track_velocity, get_my_pos
from src.seestar_client import TransitRecorder
from src.telegram_notify import send_telegram_notification
from src.transit import get_transits

# Module-level cache: {fa_flight_id: (speed_kmh, heading_deg)}
# Populated when a user loads a flight's track; consumed by soft-refresh recalculation.
_track_velocity_cache: dict = {}

# Server-side route/track response cache to avoid redundant FlightAware API calls.
# {fa_flight_id: (timestamp, response_dict)}
_route_response_cache: dict = {}
_track_response_cache: dict = {}
ROUTE_TRACK_CACHE_TTL = 3600  # 1 hour — a flight's historical track doesn't change much


def _evict_expired_caches() -> None:
    """Remove stale entries from all TTL-based module-level caches.

    Called on every cache write so caches never grow beyond the number of
    unique flights seen within one TTL window.  Cheap — iterates only the
    keys that currently exist, which is bounded by real flight activity.
    """
    now = time.time()
    cutoff = now - ROUTE_TRACK_CACHE_TTL
    for cache in (_route_response_cache, _track_response_cache):
        stale = [k for k, v in cache.items() if v[0] < cutoff]
        for k in stale:
            cache.pop(k, None)
    # _track_velocity_cache entries have no timestamp; cap at 500 entries
    # (one per unique flight; realistic daily traffic is well under this).
    if len(_track_velocity_cache) > 500:
        # Drop the oldest half by insertion order (Python 3.7+ dicts are ordered)
        drop = list(_track_velocity_cache.keys())[:250]
        for k in drop:
            _track_velocity_cache.pop(k, None)


# Bounded thread pool for background tasks (save transits, Telegram, track pre-fetch).
# Max 4 workers prevents unbounded thread growth under high request rates while
# still allowing concurrent work.  Tasks that arrive while all workers are busy
# are queued by the executor's internal queue (also bounded in Python ≥3.12 but
# effectively fine here since tasks complete in seconds).
_bg_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="flymoon-bg")

# Global test/demo mode flag
test_mode = False

# Validate configuration on startup
wizard = ConfigWizard()
if not wizard.validate(interactive=False):
    print("\n⚠️  Configuration issues detected:")
    print(wizard.get_status_report())
    print("\n💡 Run 'python3 src/config_wizard.py --setup' to configure\n")

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # never cache static files
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

# Compute a stable app version from git commit hash for cache-busting static assets.
import os as _os
import time as _time

# Use the most recent mtime of any static asset so the cache busts on every
# file save during development, even without a new commit.
_static_dir = _os.path.join(_os.path.dirname(__file__), "static")
_template_dir = _os.path.join(_os.path.dirname(__file__), "templates")
try:
    _mtimes = [
        _os.path.getmtime(_os.path.join(d, f))
        for d in (_static_dir, _template_dir)
        for f in _os.listdir(d)
        if f.endswith((".js", ".css", ".html"))
    ]
    APP_VERSION = str(int(max(_mtimes)))
except Exception:
    APP_VERSION = str(int(_time.time()))


@app.context_processor
def inject_app_version():
    return {"app_version": APP_VERSION}


# Configure logging to suppress telescope status polling
import logging

werkzeug_logger = logging.getLogger("werkzeug")


class TelescopeStatusFilter(logging.Filter):
    def filter(self, record):
        # Filter out telescope status endpoint logs (polled every 2 seconds)
        return "/telescope/status" not in record.getMessage()


werkzeug_logger.addFilter(TelescopeStatusFilter())

# Transit recorder for automatic video capture
_transit_recorder = None


def get_transit_recorder():
    """Get or create TransitRecorder instance if telescope is connected."""
    global _transit_recorder

    # Check if telescope is enabled and connected
    telescope_client = telescope_routes.get_telescope_client()
    if not telescope_client or not telescope_client.is_connected():
        return None

    # Create recorder if it doesn't exist
    if _transit_recorder is None:
        _transit_recorder = TransitRecorder(
            seestar_client=telescope_client,
            pre_buffer_seconds=10,
            post_buffer_seconds=10,
        )
        logger.info("✅ TransitRecorder initialized")

    return _transit_recorder


def calculate_adaptive_interval(flights: list) -> int:
    """
    Calculate adaptive polling interval based on transit proximity.

    Returns interval in seconds:
    - 30s if transit <2 min away
    - 60s if transit <5 min away
    - 120s if transit <10 min away
    - 600s (10 min) otherwise
    """
    if not flights:
        return 120  # 2 minutes if no flights

    # Find closest high/medium probability transit
    priority_transits = [
        f.get("time", 999)
        for f in flights
        if f.get("is_possible_transit") == 1
        and f.get("possibility_level")
        in [PossibilityLevel.HIGH.value, PossibilityLevel.MEDIUM.value]
    ]

    if not priority_transits:
        # Only low probability or no transits
        return 120  # 2 minutes

    closest_transit_time = min(priority_transits)

    if closest_transit_time < 2:  # <2 min away
        return 10  # 10 seconds
    elif closest_transit_time < 5:  # <5 min away
        return 30  # 30 seconds
    elif closest_transit_time < 10:  # <10 min away
        return 60  # 1 minute
    else:
        return 120  # 2 minutes (default)


@app.route("/")
def index():
    return render_template(
        "index.html", openaip_api_key=os.getenv("OPENAIP_API_KEY", "")
    )


@app.route("/cost-models")
def cost_models():
    """Flight data acquisition cost-benefit analysis paper."""
    return render_template("cost_models.html")


@app.route("/config")
def get_config():
    """Return app configuration for client."""
    load_dotenv(override=True)  # Always pick up latest .env without restarting
    return jsonify(
        {
            "autoRefreshIntervalMinutes": int(
                os.getenv("AUTO_REFRESH_INTERVAL_MINUTES", 10)
            ),
            "cacheEnabled": True,
            "cacheTTLSeconds": 600,
            "codeRevision": _flymoon_code_revision(),
            "openaipApiKey": os.getenv("OPENAIP_API_KEY", ""),
            "observerLatitude": os.getenv("OBSERVER_LATITUDE", ""),
            "observerLongitude": os.getenv("OBSERVER_LONGITUDE", ""),
            "observerElevation": os.getenv("OBSERVER_ELEVATION", "0"),
            "bboxLatLL": os.getenv("LAT_LOWER_LEFT", ""),
            "bboxLonLL": os.getenv("LONG_LOWER_LEFT", ""),
            "bboxLatUR": os.getenv("LAT_UPPER_RIGHT", ""),
            "bboxLonUR": os.getenv("LONG_UPPER_RIGHT", ""),
        }
    )


import threading as _threading

# Server-side OpenAIP tile cache — avoids re-fetching the same tile from OpenAIP
# on every page load. Each tile is stored for TILE_CACHE_TTL seconds (1 hour).
_tile_cache: dict = {}
_tile_cache_lock = _threading.Lock()
TILE_CACHE_TTL = 3600  # seconds


def _get_cached_tile(key):
    with _tile_cache_lock:
        entry = _tile_cache.get(key)
        if entry and (time.time() - entry["ts"]) < TILE_CACHE_TTL:
            return entry["data"], entry["ct"]
    return None, None


def _set_cached_tile(key, data, content_type):
    with _tile_cache_lock:
        _tile_cache[key] = {"data": data, "ct": content_type, "ts": time.time()}
        # Evict expired tiles on every write to keep memory bounded.
        now = time.time()
        stale = [k for k, v in _tile_cache.items() if (now - v["ts"]) >= TILE_CACHE_TTL]
        for k in stale:
            _tile_cache.pop(k, None)


@app.route("/tiles/openaip/<int:z>/<int:x>/<int:y>.png")
def proxy_openaip_tile(z, x, y):
    """Proxy OpenAIP tiles through Flask to avoid browser extension blocking.

    Server-side cache (1 hour TTL) prevents each tile from making a new
    outbound request on every page load, eliminating thread saturation that
    could delay /flights responses.
    """
    api_key = os.getenv("OPENAIP_API_KEY", "")
    if not api_key:
        return Response(status=404)

    cache_key = f"{z}/{x}/{y}"
    cached_data, cached_ct = _get_cached_tile(cache_key)
    if cached_data is not None:
        return Response(
            cached_data,
            status=200,
            content_type=cached_ct or "image/png",
            headers={"Cache-Control": "public, max-age=3600", "X-Tile-Cache": "HIT"},
        )

    url = f"https://api.tiles.openaip.net/api/data/openaip/{z}/{x}/{y}.png"
    try:
        resp = requests.get(url, headers={"x-openaip-api-key": api_key}, timeout=5)
        ct = resp.headers.get("Content-Type", "image/png")
        if resp.status_code == 200:
            _set_cached_tile(cache_key, resp.content, ct)
        return Response(
            resp.content,
            status=resp.status_code,
            content_type=ct,
            headers={"Cache-Control": "public, max-age=3600", "X-Tile-Cache": "MISS"},
        )
    except Exception:
        return Response(status=502)


@app.route("/cache/stats")
def cache_stats():
    """Return cache statistics for monitoring."""
    cache = get_cache()
    return jsonify(cache.get_stats())


def _resolve_min_altitude(
    azimuth_deg: float, args_or_data, default: float = 15.0
) -> float:
    """Return the directional min-altitude threshold for *azimuth_deg*.

    Reads ``min_alt_n/e/s/w`` from *args_or_data* (a Flask ``request.args``
    or a plain ``dict``).  Falls back to the legacy ``min_altitude`` key, then
    to *default*.  This ensures the correct quadrant value is used rather than
    collapsing all four down to their minimum.
    """

    def _get(key):
        if hasattr(args_or_data, "get"):
            v = args_or_data.get(key)
        else:
            v = args_or_data.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    az = ((azimuth_deg or 0) % 360 + 360) % 360
    # Quadrants: N=0-90° (NE), E=90-180° (SE), S=180-270° (SW), W=270-360° (NW)
    if az < 90:
        quadrant_val = _get("min_alt_n")
    elif az < 180:
        quadrant_val = _get("min_alt_e")
    elif az < 270:
        quadrant_val = _get("min_alt_s")
    else:
        quadrant_val = _get("min_alt_w")

    if quadrant_val is not None:
        return quadrant_val
    # Legacy single-value fallback
    legacy = _get("min_altitude")
    return legacy if legacy is not None else default


@app.route("/flights")
def get_all_flights():
    try:
        start_time = time.time()

        _lat_raw = request.args.get("latitude")
        _lon_raw = request.args.get("longitude")
        if _lat_raw is None or _lon_raw is None:
            logger.warning("[Flights] Missing or invalid coordinates")
            return (
                jsonify(
                    {"error": "Missing required parameter: 'latitude' and 'longitude'"}
                ),
                400,
            )
        latitude = float(_lat_raw)
        longitude = float(_lon_raw)
        elevation = float(request.args.get("elevation") or 0)

        has_send_notification = request.args.get("send-notification") == "true"

        # Data source mode: 'hybrid' (default), 'fa-only', 'opensky-only', 'adsb-local'
        data_source = request.args.get("data_source", "hybrid")

        # Targets disabled by the user in the UI (comma-separated: "sun", "moon", or "sun,moon")
        _disabled_raw = request.args.get("disabled_targets", "")
        disabled_targets = {
            t.strip().lower() for t in _disabled_raw.split(",") if t.strip()
        }
        transit_monitor.set_disabled_targets(disabled_targets)

        # Check for custom bounding box from user
        custom_bbox = None
        if all(
            key in request.args
            for key in ["bbox_lat_ll", "bbox_lon_ll", "bbox_lat_ur", "bbox_lon_ur"]
        ):
            custom_bbox = {
                "lat_lower_left": float(request.args["bbox_lat_ll"]),
                "lon_lower_left": float(request.args["bbox_lon_ll"]),
                "lat_upper_right": float(request.args["bbox_lat_ur"]),
                "lon_upper_right": float(request.args["bbox_lon_ur"]),
            }
            logger.info(f"Using custom bounding box: {custom_bbox}")

        # Always check both sun and moon for transits
        all_flights = []
        target_coordinates = {}
        tracking_targets = (
            []
        )  # List of targets actually being tracked (above min altitude)

        # Check if target is above minimum altitude
        EARTH = ASTRO_EPHEMERIS["earth"]
        MY_POSITION = get_my_pos(
            lat=latitude, lon=longitude, elevation=elevation, base_ref=EARTH
        )
        local_timezone = get_localzone_name()
        ref_datetime = datetime.now().replace(tzinfo=ZoneInfo(local_timezone))

        all_bboxes_used = []
        for target in ["sun", "moon"]:
            # Check altitude and calculate coordinates for both tracking and display
            celestial_obj = CelestialObject(name=target, observer_position=MY_POSITION)
            celestial_obj.update_position(ref_datetime=ref_datetime)
            coords = celestial_obj.get_coordinates()

            # Always save coordinates for display in header (even if below horizon)
            target_coordinates[target] = coords

            # Skip if user has toggled this target off in the UI
            if target in disabled_targets:
                logger.info(
                    f"{target.capitalize()} disabled by user toggle, skipping transit check"
                )
                continue

            # Check directional minimum altitude for this target
            min_altitude = _resolve_min_altitude(
                coords.get("azimuthal", 0), request.args
            )
            target_above_min = coords["altitude"] >= min_altitude
            target_above_horizon = coords["altitude"] > 0

            if target_above_min:
                tracking_targets.append(target)

            # Compute transits for any target above the horizon (even if below
            # the quadrant min-altitude).  Flights against a below-min target
            # are tagged so the UI can dim them.
            if target_above_horizon:
                logger.info(
                    f"Checking transits for {target} (altitude: {coords['altitude']:.1f}°, "
                    f"min for az {coords.get('azimuthal',0):.0f}°: {min_altitude}°, "
                    f"above_min={target_above_min})"
                )
                # Never enrich on prediction. FA is only used post-capture.
                data = get_transits(
                    latitude,
                    longitude,
                    elevation,
                    target,
                    test_mode,
                    custom_bbox,
                    data_source,
                    enrich=False,
                )
                if data.get("bbox_used"):
                    all_bboxes_used.append(data["bbox_used"])

                # Tag each flight with which target it's for
                for flight in data["flights"]:
                    flight["target"] = target
                    flight["target_below_min_alt"] = not target_above_min
                    # Add computed fields for display
                    if "aircraft_elevation" in flight:
                        flight["aircraft_elevation_feet"] = int(
                            flight["aircraft_elevation"] * 3.28084
                        )
                    # Calculate distance from observer to aircraft in nautical miles
                    if "latitude" in flight and "longitude" in flight:
                        from math import atan2, cos, radians, sin, sqrt

                        lat1, lon1 = radians(latitude), radians(longitude)
                        lat2, lon2 = radians(flight["latitude"]), radians(
                            flight["longitude"]
                        )
                        dlat, dlon = lat2 - lat1, lon2 - lon1
                        a = (
                            sin(dlat / 2) ** 2
                            + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
                        )
                        c = 2 * atan2(sqrt(a), sqrt(1 - a))
                        distance_km = 6371 * c  # Earth radius in km
                        flight["distance_nm"] = (
                            distance_km * 0.539957
                        )  # Convert km to nautical miles
                all_flights.extend(data["flights"])
            else:
                logger.info(
                    f"{target.capitalize()} below horizon ({coords['altitude']:.1f}°), skipping transit check"
                )

        # Deduplicate: sun+moon calls both include excluded aircraft — keep the
        # entry with the highest possibility_level for each flight ID.
        seen: dict = {}
        for f in all_flights:
            fid = f.get("id") or f.get("name", "")
            if fid not in seen or (f.get("possibility_level") or 0) > (
                seen[fid].get("possibility_level") or 0
            ):
                seen[fid] = f
        all_flights = list(seen.values())

        # Calculate adaptive refresh interval based on closest transit
        next_check_interval = calculate_adaptive_interval(all_flights)

        # Compute the bbox for the client — union of all corridor bboxes actually used.
        # This avoids the earlier mismatched-target bug where _best_alt came from one
        # target and _best_az came from the other.
        if all_bboxes_used:
            _bbox_for_client = {
                "latLowerLeft": min(b["latLowerLeft"] for b in all_bboxes_used),
                "lonLowerLeft": min(b["lonLowerLeft"] for b in all_bboxes_used),
                "latUpperRight": max(b["latUpperRight"] for b in all_bboxes_used),
                "lonUpperRight": max(b["lonUpperRight"] for b in all_bboxes_used),
            }
        else:
            _bbox_for_client = None  # both targets below horizon — no API query made

        # Exclude flights where the celestial target is below the user's obstruction threshold
        visible_flights = [f for f in all_flights if not f.get("target_below_min_alt")]

        # Combine results
        data = {
            "flights": sort_results(visible_flights),
            "targetCoordinates": target_coordinates,
            "riseSetTimes": get_rise_set_times(latitude, longitude, elevation),
            "trackingTargets": tracking_targets,
            "disabledTargets": list(disabled_targets),
            "nextCheckInterval": next_check_interval,  # Seconds until next check
            "weather": None,  # Weather functionality not implemented yet
            "boundingBox": _bbox_for_client,
            "generated_at_ms": int(time.time() * 1000),
        }

        end_time = time.time()
        elapsed_time = end_time - start_time
        logger.info(f"Elapsed time: {elapsed_time} seconds")

        # Run file-save and Telegram notification in a background thread so they
        # don't block the HTTP response.  Both are best-effort — failures are logged.
        def _background_tasks(flights_snapshot, send_notif):
            if not test_mode:
                try:
                    date_ = date.today().strftime("%Y%m%d")
                    tc = telescope_routes.get_telescope_client()
                    scope_connected = bool(tc and tc.is_connected())
                    scope_mode = (
                        tc._viewing_mode
                        if tc and hasattr(tc, "_viewing_mode")
                        else None
                    ) or ""
                    for f in flights_snapshot:
                        f["scope_connected"] = scope_connected
                        f["scope_mode"] = scope_mode
                    asyncio.run(
                        save_possible_transits(
                            flights_snapshot,
                            POSSIBLE_TRANSITS_LOGFILENAME.format(date_=date_),
                        )
                    )
                except Exception as e:
                    logger.error(f"Error saving possible transits: {e}")
            if send_notif:
                try:
                    # Only send notifications for transits where the target is trackable
                    notifiable = [
                        f for f in flights_snapshot if not f.get("target_below_min_alt")
                    ]
                    asyncio.run(send_telegram_notification(notifiable, None))
                except Exception as e:
                    logger.error(f"Error sending Telegram notification: {e}")

            # Pre-fetch track data for HIGH-probability transits so that track-derived
            # velocity is available for soft-refresh dead-reckoning without a user click.
            api_key = get_aeroapi_key()
            if api_key:
                now_ts = time.time()
                for f in flights_snapshot:
                    if f.get("possibility_level") != PossibilityLevel.HIGH.value:
                        continue
                    if f.get("target_below_min_alt"):
                        continue
                    fid = f.get("fa_flight_id") or ""
                    if not fid:
                        continue
                    # Skip if already cached
                    cached = _track_response_cache.get(fid)
                    if cached and (now_ts - cached[0]) < ROUTE_TRACK_CACHE_TTL:
                        continue
                    try:
                        import requests as _req

                        url = f"https://aeroapi.flightaware.com/aeroapi/flights/{fid}/track"
                        headers = {
                            "Accept": "application/json; charset=UTF-8",
                            "x-apikey": api_key,
                        }
                        resp = _req.get(url=url, headers=headers, timeout=10)
                        if resp.status_code == 200:
                            track_json = resp.json()
                            from src.position import compute_track_velocity

                            positions = (
                                track_json.get("positions")
                                or track_json.get("track")
                                or []
                            )
                            velocity = compute_track_velocity(positions)
                            if velocity:
                                _track_velocity_cache[fid] = velocity
                                logger.info(
                                    f"[BG] Track velocity prefetched for {fid}: "
                                    f"{velocity[0]:.0f} km/h  hdg {velocity[1]:.1f}°"
                                )
                            _track_response_cache[fid] = (now_ts, track_json)
                            _evict_expired_caches()
                        else:
                            logger.warning(
                                f"[BG] Track prefetch for {fid} returned {resp.status_code}"
                            )
                    except Exception as e:
                        logger.error(f"[BG] Track prefetch failed for {fid}: {e}")

        _bg_executor.submit(
            _background_tasks, list(data["flights"]), has_send_notification
        )

        # Schedule automatic recordings for high-probability transits
        transit_recorder = get_transit_recorder()
        if transit_recorder:
            # Cleanup any stale timers from previous cycles
            transit_recorder.cleanup_stale_timers()

            for flight in data["flights"]:
                # Only record HIGH probability transits (green rows)
                if flight.get("possibility_level") == PossibilityLevel.HIGH.value:
                    eta_seconds = (
                        flight.get("time", 0) * 60
                    )  # time field is minutes from now
                    flight_id = flight.get("ident", flight.get("id", "unknown"))

                    try:
                        transit_recorder.schedule_transit_recording(
                            flight_id=flight_id,
                            eta_seconds=eta_seconds,
                            transit_duration_estimate=2.0,  # Aircraft transits ~0.5-2 seconds
                            sep_deg=flight.get("angular_separation", 0.0),
                        )
                        logger.info(
                            f"📹 Scheduled recording for {flight_id} (ETA: {eta_seconds:.0f}s)"
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to schedule recording for {flight_id}: {e}"
                        )

        return jsonify(data)

    except KeyError as e:
        logger.error(f"Missing required parameter: {e}")
        return jsonify({"error": f"Missing required parameter: {e}"}), 400
    except ValueError as e:
        logger.error(f"Invalid parameter value: {e}", exc_info=True)
        return jsonify({"error": "Invalid parameter value"}), 400
    except Exception as e:
        logger.error(f"Error in /flights endpoint: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@app.route("/flights/<fa_flight_id>/route")
def get_flight_route(fa_flight_id):
    """Get the filed route for a specific flight. Response is cached for 1 hour."""
    now = time.time()
    cached = _route_response_cache.get(fa_flight_id)
    if cached and (now - cached[0]) < ROUTE_TRACK_CACHE_TTL:
        logger.info(f"Route cache HIT for {fa_flight_id}")
        return jsonify(cached[1])

    API_KEY = get_aeroapi_key()
    url = f"https://aeroapi.flightaware.com/aeroapi/flights/{fa_flight_id}/route"
    headers = {"Accept": "application/json; charset=UTF-8", "x-apikey": API_KEY}

    try:
        response = requests.get(url=url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            _route_response_cache[fa_flight_id] = (now, data)
            _evict_expired_caches()
            return jsonify(data)
        else:
            return (
                jsonify({"error": f"API returned status {response.status_code}"}),
                response.status_code,
            )
    except Exception as e:
        logger.error(f"Error fetching route for {fa_flight_id}: {str(e)}")
        return jsonify({"error": "Failed to fetch route data"}), 500


@app.route("/transits/recalculate", methods=["POST"])
def recalculate_transits_endpoint():
    """
    Recalculate transit predictions for flights with updated positions.
    Does NOT call FlightAware API - uses provided flight data from soft refresh.

    Request JSON:
    {
        "flights": [...],  // Array of flight objects with updated positions
        "latitude": 34.0,
        "longitude": -118.0,
        "elevation": 100,
        "target": "moon",  // or "sun"
        "min_altitude": 15.0,  // Optional minimum altitude for target
    }

    Returns:
    {
        "flights": [...],  // Updated flight objects with new transit predictions
        "targetCoordinates": {...}
    }
    """
    try:
        data = request.get_json()

        flights = data.get("flights", [])
        _lat_raw = data.get("latitude")
        _lon_raw = data.get("longitude")
        if _lat_raw is None or _lon_raw is None:
            logger.warning("[Recalculate] Missing or invalid coordinates")
            return jsonify({"flights": [], "targetCoordinates": {}}), 200
        latitude = float(_lat_raw)
        longitude = float(_lon_raw)
        elevation = float(data.get("elevation", 0))
        target = data.get("target", "auto")
        if not flights:
            return jsonify({"flights": [], "targetCoordinates": {}}), 200

        # Respect disabled targets from the UI toggle buttons
        disabled_targets = set(data.get("disabled_targets", []))
        transit_monitor.set_disabled_targets(disabled_targets)

        # Apply track-velocity overrides before recalculating.
        # When a flight's track has been viewed, we have a measured velocity
        # (speed + heading from last two ADS-B fixes) that's more accurate
        # than the reported groundspeed/heading, especially during turns.
        tv_applied = 0
        for flight in flights:
            fid = flight.get("fa_flight_id") or flight.get("id", "")
            if fid and fid in _track_velocity_cache:
                spd, hdg = _track_velocity_cache[fid]
                flight["speed"] = spd
                flight["direction"] = hdg
                flight.setdefault("position_source", "track")
                tv_applied += 1
        if tv_applied:
            logger.info(
                f"Track velocity applied to {tv_applied} flights in recalculate"
            )
        from math import atan2, cos, radians, sin, sqrt
        from zoneinfo import ZoneInfo

        from tzlocal import get_localzone_name

        from src.astro import CelestialObject
        from src.constants import ASTRO_EPHEMERIS
        from src.position import get_my_pos
        from src.transit import recalculate_transits

        EARTH = ASTRO_EPHEMERIS["earth"]
        MY_POSITION = get_my_pos(
            lat=latitude, lon=longitude, elevation=elevation, base_ref=EARTH
        )
        local_timezone = get_localzone_name()
        ref_datetime = datetime.now().replace(tzinfo=ZoneInfo(local_timezone))

        all_flights = []
        target_coordinates = {}
        tracking_targets = []

        # Determine which targets to check
        targets_to_check = []
        if target == "auto":
            targets_to_check = ["sun", "moon"]
        else:
            targets_to_check = [target]

        for target_name in targets_to_check:
            # Skip if user has toggled this target off in the UI
            if target_name in disabled_targets:
                continue

            # Check altitude
            celestial_obj = CelestialObject(
                name=target_name, observer_position=MY_POSITION
            )
            celestial_obj.update_position(ref_datetime=ref_datetime)
            coords = celestial_obj.get_coordinates()
            target_coordinates[target_name] = coords

            # Check altitude against directional threshold
            min_altitude = _resolve_min_altitude(coords.get("azimuthal", 0), data)
            target_above_min = coords["altitude"] >= min_altitude
            target_above_horizon = coords["altitude"] > 0

            if target_above_min:
                tracking_targets.append(target_name)

            # Recalculate for any target above the horizon
            if target_above_horizon:
                logger.info(
                    f"Recalculating transits for {target_name} (altitude: {coords['altitude']:.1f}°, "
                    f"min for az {coords.get('azimuthal',0):.0f}°: {min_altitude}°, above_min={target_above_min})"
                )

                result = recalculate_transits(
                    flights,
                    latitude,
                    longitude,
                    elevation,
                    target_name,
                )

                # Tag each flight with target and add computed fields
                for flight in result["flights"]:
                    flight["target"] = target_name
                    flight["target_below_min_alt"] = not target_above_min
                    if "aircraft_elevation" in flight:
                        flight["aircraft_elevation_feet"] = int(
                            flight["aircraft_elevation"] * 3.28084
                        )
                    # Calculate distance
                    if "latitude" in flight and "longitude" in flight:
                        lat1, lon1 = radians(latitude), radians(longitude)
                        lat2, lon2 = radians(flight["latitude"]), radians(
                            flight["longitude"]
                        )
                        dlat, dlon = lat2 - lat1, lon2 - lon1
                        a = (
                            sin(dlat / 2) ** 2
                            + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
                        )
                        c = 2 * atan2(sqrt(a), sqrt(1 - a))
                        distance_km = 6371 * c
                        flight["distance_nm"] = distance_km * 0.539957

                all_flights.extend(result["flights"])

        return jsonify(
            {
                "flights": all_flights,
                "targetCoordinates": target_coordinates,
                "trackingTargets": tracking_targets,
                "generated_at_ms": int(time.time() * 1000),
            }
        )

    except KeyError as e:
        logger.error(f"Missing required parameter: {e}")
        return jsonify({"error": f"Missing required parameter: {e}"}), 400
    except ValueError as e:
        logger.error(f"Invalid parameter value: {e}")
        return jsonify({"error": "Invalid parameter value"}), 400
    except Exception as e:
        logger.error(f"Error in /transits/recalculate: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@app.route("/flights/<fa_flight_id>/track")
def get_flight_track(fa_flight_id):
    """Get the historical track positions for a specific flight. Response is cached for 1 hour."""
    now = time.time()
    cached = _track_response_cache.get(fa_flight_id)
    if cached and (now - cached[0]) < ROUTE_TRACK_CACHE_TTL:
        logger.info(f"Track cache HIT for {fa_flight_id}")
        return jsonify(cached[1])

    API_KEY = get_aeroapi_key()
    url = f"https://aeroapi.flightaware.com/aeroapi/flights/{fa_flight_id}/track"
    headers = {"Accept": "application/json; charset=UTF-8", "x-apikey": API_KEY}

    try:
        response = requests.get(url=url, headers=headers, timeout=10)
        if response.status_code == 200:
            track_json = response.json()
            # Compute track-based velocity and cache it for soft-refresh accuracy.
            positions = track_json.get("positions") or track_json.get("track") or []
            velocity = compute_track_velocity(positions)
            if velocity:
                _track_velocity_cache[fa_flight_id] = velocity
                logger.info(
                    f"Track velocity cached for {fa_flight_id}: "
                    f"{velocity[0]:.0f} km/h  hdg {velocity[1]:.1f}°"
                )
            _track_response_cache[fa_flight_id] = (now, track_json)
            _evict_expired_caches()
            return jsonify(track_json)
        else:
            return (
                jsonify({"error": f"API returned status {response.status_code}"}),
                response.status_code,
            )
    except Exception as e:
        logger.error(f"Error fetching track for {fa_flight_id}: {str(e)}")
        return jsonify({"error": "Failed to fetch track data"}), 500


@app.route("/telescope")
def telescope_page():
    """Redirect legacy telescope URL to the main SPA."""
    return redirect("/")


@app.route("/api/transit-events")
def api_transit_events():
    """Return detection events from transit_events_*.csv logs (last 7 days).

    D4 — Detection event log UI data source.
    """
    import csv
    import glob

    from src.constants import TRANSIT_EVENTS_LOGFILENAME

    pattern = TRANSIT_EVENTS_LOGFILENAME.replace("{date_}", "*")
    files = sorted(glob.glob(pattern), reverse=True)[:7]

    events = []
    for filepath in files:
        try:
            with open(filepath, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    events.append(
                        {
                            "timestamp": row.get("timestamp", ""),
                            "detected_flight_id": row.get("detected_flight_id", ""),
                            "predicted_flight_id": row.get("predicted_flight_id", ""),
                            "prediction_sep_deg": row.get("prediction_sep_deg", ""),
                            "detection_confirmed": row.get("detection_confirmed", ""),
                            "confidence": row.get("confidence", ""),
                            "confidence_score": row.get("confidence_score", ""),
                            "signal_a": row.get("signal_a", ""),
                            "signal_b": row.get("signal_b", ""),
                            "centre_ratio": row.get("centre_ratio", ""),
                            "notes": row.get("notes", ""),
                        }
                    )
        except OSError:
            continue

    events.sort(key=lambda x: x["timestamp"], reverse=True)
    # Enrich each event with any existing human label from transit_labels.csv
    labels = _load_transit_labels()
    for ev in events:
        ev["label"] = labels.get(ev.get("timestamp", ""), "")
    return jsonify(events[:500])


@app.route("/api/transit-events/label", methods=["POST"])
def api_label_transit_event():
    """T23 — Save a human TP/FP/FN label for a detection event.

    Body: {"timestamp": "...", "label": "tp"|"fp"|"fn"|"tn", "notes": "..."}
    Appends to data/transit_labels.csv (creates if absent).
    """
    import csv as _csv

    body = request.get_json(force=True) or {}
    ts = body.get("timestamp", "").strip()
    label = body.get("label", "").strip().lower()
    notes = body.get("notes", "").strip()

    if not ts or label not in ("tp", "fp", "fn", "tn"):
        return jsonify({"error": "timestamp and label (tp/fp/fn/tn) required"}), 400

    labels_path = "data/transit_labels.csv"
    os.makedirs("data", exist_ok=True)
    first_write = not os.path.exists(labels_path)
    with open(labels_path, "a", newline="", encoding="utf-8") as fh:
        writer = _csv.writer(fh)
        if first_write:
            writer.writerow(["timestamp", "label", "notes", "labeled_at"])
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        writer.writerow([ts, label, notes, _dt.now(_tz.utc).isoformat()])

    # Invalidate cached labels
    _load_transit_labels.cache = None  # type: ignore[attr-defined]

    return jsonify({"ok": True, "timestamp": ts, "label": label})


def _load_transit_labels() -> dict:
    """Return {timestamp → label} from data/transit_labels.csv (last label wins)."""
    import csv as _csv

    cache = getattr(_load_transit_labels, "cache", None)
    if cache is not None:
        return cache
    labels: dict = {}
    path = "data/transit_labels.csv"
    if os.path.exists(path):
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                for row in _csv.DictReader(fh):
                    ts = row.get("timestamp", "").strip()
                    lbl = row.get("label", "").strip()
                    if ts and lbl:
                        labels[ts] = lbl
        except OSError:
            pass
    _load_transit_labels.cache = labels  # type: ignore[attr-defined]
    return labels


@app.route("/transit-log")
def transit_log():
    """Return deduplicated near-miss log: best angular separation per flight per day."""
    import csv
    import glob
    import math

    pattern = POSSIBLE_TRANSITS_LOGFILENAME.replace("{date_}", "*")
    files = sorted(glob.glob(pattern))

    best = {}  # key: (fa_flight_id, target, date) → best row
    for filepath in files:
        date_str = filepath.split("log_")[1].replace(".csv", "")
        # Normalize date_str to ISO for consistent sort: "20260222" → "2026-02-22"
        iso_date = (
            f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            if len(date_str) == 8
            else date_str
        )
        try:
            with open(filepath, newline="") as fh:
                for row in csv.DictReader(fh):
                    try:
                        alt = float(row.get("alt_diff") or 999)
                        az = float(row.get("az_diff") or 999)
                        # Fallback for old misaligned schema (waypoints commas broke CSV columns):
                        # the real alt_diff/az_diff ended up in is_possible_transit/possibility_level
                        if alt > 15 or az > 15:
                            alt_fb = float(row.get("is_possible_transit") or 999)
                            az_fb = float(row.get("possibility_level") or 999)
                            if alt_fb < 15 and az_fb < 15:
                                alt, az = alt_fb, az_fb
                            else:
                                continue  # Can't recover this row
                        sep = math.sqrt(alt**2 + az**2)
                        fid = row.get("fa_flight_id") or row.get("id", "?")
                        target = row.get("target", "?")
                        key = (fid, target, date_str)
                        ts = row.get("timestamp", "") or iso_date
                        if key not in best or sep < best[key]["sep"]:
                            best[key] = {
                                "date": date_str,
                                "timestamp": ts,
                                "flight": row.get("id", "?"),
                                "fa_flight_id": fid,
                                "aircraft_type": row.get("aircraft_type", ""),
                                "origin": row.get("origin", ""),
                                "destination": row.get("destination", ""),
                                "target": target,
                                "alt_diff": round(alt, 3),
                                "az_diff": round(az, 3),
                                "sep": round(sep, 3),
                                "time": row.get("time", ""),
                                "possibility_level": int(
                                    float(row.get("possibility_level") or 0)
                                ),
                                "target_alt": row.get("target_alt", ""),
                                "plane_alt": row.get("plane_alt", ""),
                                "target_az": row.get("target_az", ""),
                                "plane_az": row.get("plane_az", ""),
                                "scope_connected": row.get("scope_connected", ""),
                                "scope_mode": row.get("scope_mode", ""),
                            }
                    except (ValueError, KeyError):
                        continue
        except OSError:
            continue

    # Sort by full timestamp desc; iso_date fallback ensures consistent comparison
    events = sorted(best.values(), key=lambda x: x["timestamp"], reverse=True)
    return jsonify(events)


# Register telescope control routes
telescope_routes.register_routes(app)

# Start transit monitor
from src.transit_monitor import get_monitor

transit_monitor = get_monitor()
transit_monitor.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Flymoon Transit Monitor")
    parser.add_argument(
        "--test", action="store_true", help="Use test data (deprecated, use --demo)"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use mock demonstration data with guaranteed classifications",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open the web browser on startup",
    )
    args = parser.parse_args()

    test_mode = args.test or args.demo

    if test_mode:
        mode = "DEMO" if args.demo else "TEST"
        logger.info(f"🎭 Starting in {mode} mode - using mock data")

    # Use PORT env var if set (e.g. by Electron), otherwise find a free port
    import socket

    def _port_is_free(p: int) -> bool:
        """Return True if *p* can be bound right now."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", p))
            s.close()
            return True
        except OSError:
            return False

    def _kill_stale_on_port(p: int) -> bool:
        """Kill any process holding *p* and wait until the port is actually free.
        Returns True if port is confirmed free within ~3 s."""
        import signal as _sig
        import subprocess as _sp
        import time as _t

        try:
            out = (
                _sp.check_output(
                    ["lsof", "-ti", f"TCP:{p}", "-sTCP:LISTEN"],
                    stderr=_sp.DEVNULL,
                )
                .decode()
                .split()
            )
        except (_sp.CalledProcessError, FileNotFoundError):
            return False  # lsof not available or no process found
        killed = False
        for pid_str in out:
            try:
                pid = int(pid_str)
                if pid == os.getpid():
                    continue
                # SIGKILL for immediate release (no graceful-shutdown delay)
                os.kill(pid, _sig.SIGKILL)
                killed = True
                print(f"🔄  Stopped stale process (PID {pid}) on port {p}")
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        if not killed:
            return False
        # Poll until the OS actually releases the port (up to 3 s)
        for _ in range(12):
            _t.sleep(0.25)
            if _port_is_free(p):
                return True
        return False  # gave up — port still occupied

    port = None
    env_port = os.getenv("PORT")
    if env_port:
        port = int(env_port)
    else:
        preferred = 8000
        if _port_is_free(preferred):
            port = preferred
        elif _kill_stale_on_port(preferred):
            port = preferred  # confirmed free after kill
        else:
            # Owned by something we can't kill (e.g. system service) — bump
            for p in range(preferred + 1, 8101):
                if _port_is_free(p):
                    print(
                        f"⚠️  Port {preferred} is in use by a non-app process — starting on {p}"
                    )
                    port = p
                    break

    if port is None:
        logger.error("❌ No available ports in range 8000-8100")
        print("❌ No available ports in range 8000-8100")
        exit(1)

    print(f"🚀 Starting server on port {port}")
    print(
        f"📌 Code revision: {_flymoon_code_revision()} — "
        "confirm in browser console (config.codeRevision) after refresh; "
        "if stale: pull, restart, or PYTHONDONTWRITEBYTECODE=1 python3 -B app.py"
    )

    # Reduce werkzeug logging noise
    import logging

    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)

    # Allow immediate port reuse and clean shutdown on Ctrl-C / SIGTERM
    import signal
    import threading

    from werkzeug.serving import BaseWSGIServer, WSGIRequestHandler

    class ReusableWSGIServer(BaseWSGIServer):
        """Threaded WSGI server with SO_REUSEADDR/SO_REUSEPORT set before bind."""

        allow_reuse_address = True
        daemon_threads = True
        request_queue_size = 128

        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Note: SO_REUSEPORT intentionally NOT set — it allows multiple
            # processes to bind the same port, so a stale server survives
            # restart and steals requests from the new one.
            super().server_bind()

        def process_request(self, request, client_address):
            """Handle each request in a new daemon thread."""
            t = threading.Thread(
                target=self.process_request_thread,
                args=(request, client_address),
                daemon=True,
            )
            t.start()

        def process_request_thread(self, request, client_address):
            try:
                self.finish_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)

    server = ReusableWSGIServer("0.0.0.0", port, app, handler=WSGIRequestHandler)

    # Capture `os` in the closure now — import inside a signal handler can
    # block on the import lock if another thread is mid-import when Ctrl-C
    # fires.  Similarly, print() acquires the stdout lock, so background
    # threads printing at the same instant cause the handler to deadlock
    # before reaching os._exit().  os.write() to fd 2 (stderr) is
    # async-signal-safe and never blocks.
    import os as _os_for_shutdown

    def _shutdown(sig, frame):
        _os_for_shutdown.write(2, b"\nShutting down...\n")
        _os_for_shutdown._exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Auto-open browser unless suppressed by flag or env
    if not args.no_browser and not os.getenv("FLYMOON_NO_BROWSER"):
        import shutil
        import subprocess
        import sys
        import webbrowser

        url = f"http://localhost:{port}"
        browser_pref = os.getenv("FLYMOON_BROWSER", "default").strip().lower()

        def _open_in_preferred_browser(target_url: str, browser: str):
            if browser in ("", "default", "system"):
                webbrowser.open(target_url)
                return

            if browser == "chrome":
                # Best-effort Chrome launch across platforms; fall back to default browser.
                if sys.platform == "darwin":
                    subprocess.Popen(["open", "-a", "Google Chrome", target_url])
                    return
                if os.name == "nt":
                    try:
                        webbrowser.get("chrome").open(target_url)
                        return
                    except webbrowser.Error:
                        pass
                for cmd in (
                    "google-chrome",
                    "google-chrome-stable",
                    "chromium",
                    "chromium-browser",
                ):
                    browser_path = shutil.which(cmd)
                    if browser_path:
                        subprocess.Popen([browser_path, target_url])
                        return

            logger.warning(
                f"Unknown or unavailable FLYMOON_BROWSER='{browser}', "
                "falling back to system default browser"
            )
            webbrowser.open(target_url)

        def _open_browser():
            time.sleep(1.5)
            print(f"🌐 Opening {url} (browser={browser_pref})")
            _open_in_preferred_browser(url, browser_pref)

        threading.Thread(target=_open_browser, daemon=True).start()

    server.serve_forever()
