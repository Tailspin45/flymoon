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
import json
import os
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request
from tzlocal import get_localzone_name

from src.constants import POSSIBLE_TRANSITS_LOGFILENAME, ASTRO_EPHEMERIS, get_aeroapi_key

# SETUP
load_dotenv()

from src import logger
from src.astro import CelestialObject, get_rise_set_times
from src.config_wizard import ConfigWizard
from src.flight_data import save_possible_transits, sort_results
from src.flight_cache import get_cache
from src.position import get_my_pos, compute_track_velocity
from src.telegram_notify import send_telegram_notification
from src.transit import get_transits
from src import telescope_routes
from src.seestar_client import TransitRecorder
from src.constants import PossibilityLevel

# Module-level cache: {fa_flight_id: (speed_kmh, heading_deg)}
# Populated when a user loads a flight's track; consumed by soft-refresh recalculation.
_track_velocity_cache: dict = {}

# Global test/demo mode flag
test_mode = False

# Validate configuration on startup
wizard = ConfigWizard()
if not wizard.validate(interactive=False):
    print("\n⚠️  Configuration issues detected:")
    print(wizard.get_status_report())
    print("\n💡 Run 'python3 src/config_wizard.py --setup' to configure\n")

app = Flask(__name__)

# Configure logging to suppress telescope status polling
import logging
werkzeug_logger = logging.getLogger('werkzeug')

class TelescopeStatusFilter(logging.Filter):
    def filter(self, record):
        # Filter out telescope status endpoint logs (polled every 2 seconds)
        return '/telescope/status' not in record.getMessage()

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
            post_buffer_seconds=10
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
        return 600  # 10 minutes if no flights
    
    # Find closest high/medium probability transit
    priority_transits = [
        f.get("time", 999) for f in flights 
        if f.get("is_possible_transit") == 1 and 
        f.get("possibility_level") in [PossibilityLevel.HIGH.value, PossibilityLevel.MEDIUM.value]
    ]
    
    if not priority_transits:
        # Only low probability or no transits
        return 600  # 10 minutes
    
    closest_transit_time = min(priority_transits)
    
    if closest_transit_time < 2:  # <2 min away
        return 30  # 30 seconds
    elif closest_transit_time < 5:  # <5 min away
        return 60  # 1 minute
    elif closest_transit_time < 10:  # <10 min away
        return 120  # 2 minutes
    else:
        return 600  # 10 minutes (default)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def get_config():
    """Return app configuration for client."""
    return jsonify({
        "autoRefreshIntervalMinutes": int(os.getenv("AUTO_REFRESH_INTERVAL_MINUTES", 10)),
        "cacheEnabled": True,
        "cacheTTLSeconds": 600
    })


@app.route("/cache/stats")
def cache_stats():
    """Return cache statistics for monitoring."""
    cache = get_cache()
    return jsonify(cache.get_stats())


@app.route("/flights")
def get_all_flights():
    try:
        start_time = time.time()

        _lat_raw = request.args.get("latitude")
        _lon_raw = request.args.get("longitude")
        if _lat_raw is None or _lon_raw is None:
            logger.warning("[Flights] Missing or invalid coordinates")
            return jsonify({"error": "Missing required parameter: 'latitude' and 'longitude'"}), 400
        latitude = float(_lat_raw)
        longitude = float(_lon_raw)
        elevation = float(request.args.get("elevation") or 0)
        min_altitude = float(request.args.get("min_altitude", 15))
        alt_threshold = float(request.args.get("alt_threshold", 5.0))
        az_threshold = float(request.args.get("az_threshold", 10.0))

        logger.debug(f"Parameter types: min_altitude={type(min_altitude)}, alt_threshold={type(alt_threshold)}, az_threshold={type(az_threshold)}")
        
        has_send_notification = request.args.get("send-notification") == "true"

        # Check for custom bounding box from user
        custom_bbox = None
        if all(key in request.args for key in ["bbox_lat_ll", "bbox_lon_ll", "bbox_lat_ur", "bbox_lon_ur"]):
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
        tracking_targets = []  # List of targets actually being tracked (above min altitude)
        
        # Check if target is above minimum altitude
        EARTH = ASTRO_EPHEMERIS["earth"]
        MY_POSITION = get_my_pos(lat=latitude, lon=longitude, elevation=elevation, base_ref=EARTH)
        local_timezone = get_localzone_name()
        ref_datetime = datetime.now().replace(tzinfo=ZoneInfo(local_timezone))
        
        for target in ["sun", "moon"]:
            # Check altitude and calculate coordinates for both tracking and display
            celestial_obj = CelestialObject(name=target, observer_position=MY_POSITION)
            celestial_obj.update_position(ref_datetime=ref_datetime)
            coords = celestial_obj.get_coordinates()
            
            # Always save coordinates for display in header (even if below horizon)
            target_coordinates[target] = coords
            
            # Only check transits if above minimum altitude
            if coords["altitude"] >= min_altitude:
                tracking_targets.append(target)  # Add to tracking list
                logger.info(f"Checking transits for {target} (altitude: {coords['altitude']:.1f}°, thresholds: alt={alt_threshold}°, az={az_threshold}°)")
                data = get_transits(latitude, longitude, elevation, target, test_mode, alt_threshold, az_threshold, custom_bbox)
                
                # Tag each flight with which target it's for
                for flight in data["flights"]:
                    flight["target"] = target
                    # Add computed fields for display
                    if "aircraft_elevation" in flight:
                        flight["aircraft_elevation_feet"] = int(flight["aircraft_elevation"] * 3.28084)
                    # Calculate distance from observer to aircraft in nautical miles
                    if "latitude" in flight and "longitude" in flight:
                        from math import radians, sin, cos, sqrt, atan2
                        lat1, lon1 = radians(latitude), radians(longitude)
                        lat2, lon2 = radians(flight["latitude"]), radians(flight["longitude"])
                        dlat, dlon = lat2 - lat1, lon2 - lon1
                        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
                        c = 2 * atan2(sqrt(a), sqrt(1-a))
                        distance_km = 6371 * c  # Earth radius in km
                        flight["distance_nm"] = distance_km * 0.539957  # Convert km to nautical miles
                all_flights.extend(data["flights"])
            else:
                logger.info(f"{target.capitalize()} below minimum altitude ({coords['altitude']:.1f}° < {float(min_altitude)}°), skipping transit check")
        
        # Calculate adaptive refresh interval based on closest transit
        next_check_interval = calculate_adaptive_interval(all_flights)
        
        # Combine results
        data = {
            "flights": sort_results(all_flights),
            "targetCoordinates": target_coordinates,
            "riseSetTimes": get_rise_set_times(latitude, longitude, elevation),
            "trackingTargets": tracking_targets,
            "nextCheckInterval": next_check_interval,  # Seconds until next check
            "weather": None,  # Weather functionality not implemented yet
            "boundingBox": {
                "latLowerLeft": custom_bbox["lat_lower_left"] if custom_bbox else float(os.getenv("LAT_LOWER_LEFT", "0")),
                "lonLowerLeft": custom_bbox["lon_lower_left"] if custom_bbox else float(os.getenv("LONG_LOWER_LEFT", "0")),
                "latUpperRight": custom_bbox["lat_upper_right"] if custom_bbox else float(os.getenv("LAT_UPPER_RIGHT", "0")),
                "lonUpperRight": custom_bbox["lon_upper_right"] if custom_bbox else float(os.getenv("LONG_UPPER_RIGHT", "0")),
            }
        }

        end_time = time.time()
        elapsed_time = end_time - start_time
        logger.info(f"Elapsed time: {elapsed_time} seconds")

        if not test_mode:
            try:
                date_ = date.today().strftime("%Y%m%d")
                # Stamp scope status onto each flight dict before saving
                tc = telescope_routes.get_telescope_client()
                scope_connected = bool(tc and tc.is_connected())
                scope_mode = (tc._viewing_mode if tc and hasattr(tc, '_viewing_mode') else None) or ""
                for f in data["flights"]:
                    f["scope_connected"] = scope_connected
                    f["scope_mode"] = scope_mode
                asyncio.run(
                    save_possible_transits(
                        data["flights"], POSSIBLE_TRANSITS_LOGFILENAME.format(date_=date_)
                    )
                )
            except Exception as e:
                logger.error(
                    f"Error while trying to save possible transits. Details:\n{str(e)}"
                )

        if has_send_notification:
            try:
                # Send Telegram notification for medium/high probability transits
                # Notification will include which target (sun/moon) each transit is for
                asyncio.run(send_telegram_notification(data["flights"], None))
            except Exception as e:
                logger.error(f"Error while trying to send Telegram notification. Details:\n{str(e)}")

        # Schedule automatic recordings for high-probability transits
        transit_recorder = get_transit_recorder()
        if transit_recorder:
            # Cleanup any stale timers from previous cycles
            transit_recorder.cleanup_stale_timers()
            
            for flight in data["flights"]:
                # Only record HIGH probability transits (green rows)
                if flight.get("possibility_level") == PossibilityLevel.HIGH.value:
                    eta_seconds = flight.get("transit_eta_seconds", flight.get("time", 0) * 60)
                    flight_id = flight.get("ident", flight.get("id", "unknown"))

                    try:
                        transit_recorder.schedule_transit_recording(
                            flight_id=flight_id,
                            eta_seconds=eta_seconds,
                            transit_duration_estimate=2.0  # Aircraft transits ~0.5-2 seconds
                        )
                        logger.info(f"📹 Scheduled recording for {flight_id} (ETA: {eta_seconds:.0f}s)")
                    except Exception as e:
                        logger.error(f"Failed to schedule recording for {flight_id}: {e}")

        return jsonify(data)
    
    except KeyError as e:
        logger.error(f"Missing required parameter: {e}")
        return jsonify({"error": f"Missing required parameter: {e}"}), 400
    except ValueError as e:
        logger.error(f"Invalid parameter value: {e}", exc_info=True)
        return jsonify({"error": f"Invalid parameter value: {e}"}), 400
    except Exception as e:
        logger.error(f"Error in /flights endpoint: {str(e)}", exc_info=True)
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@app.route("/flights/<fa_flight_id>/route")
def get_flight_route(fa_flight_id):
    """Get the filed route for a specific flight."""
    API_KEY = get_aeroapi_key()
    url = f"https://aeroapi.flightaware.com/aeroapi/flights/{fa_flight_id}/route"
    headers = {"Accept": "application/json; charset=UTF-8", "x-apikey": API_KEY}

    try:
        response = requests.get(url=url, headers=headers, timeout=10)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": f"API returned status {response.status_code}"}), response.status_code
    except Exception as e:
        logger.error(f"Error fetching route for {fa_flight_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500


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
        "alt_threshold": 1.0,  // Optional
        "az_threshold": 1.0    // Optional
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
        min_altitude = float(data.get("min_altitude", 15.0))
        alt_threshold = float(data.get("alt_threshold", 
                                      float(os.getenv("ALT_THRESHOLD", "1.0"))))
        az_threshold = float(data.get("az_threshold", 
                                     float(os.getenv("AZ_THRESHOLD", "1.0"))))
        
        if not flights:
            return jsonify({"flights": [], "targetCoordinates": {}}), 200

        # Apply track-velocity overrides before recalculating.
        # When a flight's track has been viewed, we have a measured velocity
        # (speed + heading from last two ADS-B fixes) that's more accurate
        # than the reported groundspeed/heading, especially during turns.
        tv_applied = 0
        for flight in flights:
            fid = flight.get("fa_flight_id") or flight.get("id", "")
            if fid and fid in _track_velocity_cache:
                spd, hdg = _track_velocity_cache[fid]
                flight["speed"]     = spd
                flight["direction"] = hdg
                flight.setdefault("position_source", "track")
                tv_applied += 1
        if tv_applied:
            logger.info(f"Track velocity applied to {tv_applied} flights in recalculate")
        from src.transit import recalculate_transits
        from src.astro import CelestialObject
        from src.position import get_my_pos
        from src.constants import ASTRO_EPHEMERIS
        from zoneinfo import ZoneInfo
        from tzlocal import get_localzone_name
        from math import radians, sin, cos, sqrt, atan2
        
        EARTH = ASTRO_EPHEMERIS["earth"]
        MY_POSITION = get_my_pos(lat=latitude, lon=longitude, elevation=elevation, base_ref=EARTH)
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
            # Check altitude
            celestial_obj = CelestialObject(name=target_name, observer_position=MY_POSITION)
            celestial_obj.update_position(ref_datetime=ref_datetime)
            coords = celestial_obj.get_coordinates()
            target_coordinates[target_name] = coords
            
            if coords["altitude"] >= min_altitude:
                tracking_targets.append(target_name)
                logger.info(f"Recalculating transits for {target_name} (altitude: {coords['altitude']:.1f}°)")
                
                result = recalculate_transits(
                    flights,
                    latitude,
                    longitude,
                    elevation,
                    target_name,
                    alt_threshold,
                    az_threshold
                )
                
                # Tag each flight with target and add computed fields
                for flight in result["flights"]:
                    flight["target"] = target_name
                    if "aircraft_elevation" in flight:
                        flight["aircraft_elevation_feet"] = int(flight["aircraft_elevation"] * 3.28084)
                    # Calculate distance
                    if "latitude" in flight and "longitude" in flight:
                        lat1, lon1 = radians(latitude), radians(longitude)
                        lat2, lon2 = radians(flight["latitude"]), radians(flight["longitude"])
                        dlat, dlon = lat2 - lat1, lon2 - lon1
                        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
                        c = 2 * atan2(sqrt(a), sqrt(1-a))
                        distance_km = 6371 * c
                        flight["distance_nm"] = distance_km * 0.539957
                
                all_flights.extend(result["flights"])
        
        return jsonify({
            "flights": all_flights,
            "targetCoordinates": target_coordinates,
            "trackingTargets": tracking_targets
        })
    
    except KeyError as e:
        logger.error(f"Missing required parameter: {e}")
        return jsonify({"error": f"Missing required parameter: {e}"}), 400
    except ValueError as e:
        logger.error(f"Invalid parameter value: {e}")
        return jsonify({"error": f"Invalid parameter value: {e}"}), 400
    except Exception as e:
        logger.error(f"Error in /transits/recalculate: {str(e)}", exc_info=True)
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@app.route("/flights/<fa_flight_id>/track")
def get_flight_track(fa_flight_id):
    """Get the historical track positions for a specific flight."""
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
            return jsonify(track_json)
        else:
            return jsonify({"error": f"API returned status {response.status_code}"}), response.status_code
    except Exception as e:
        logger.error(f"Error fetching track for {fa_flight_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/telescope")
def telescope_page():
    """Redirect legacy telescope URL to the main SPA."""
    return redirect("/")


@app.route("/transit-log")
def transit_log():
    """Return deduplicated near-miss log: best angular separation per flight per day."""
    import csv, glob, math

    pattern = POSSIBLE_TRANSITS_LOGFILENAME.replace("{date_}", "*")
    files = sorted(glob.glob(pattern))

    best = {}  # key: (fa_flight_id, target, date) → best row
    for filepath in files:
        date_str = filepath.split("log_")[1].replace(".csv", "")
        try:
            with open(filepath, newline="") as fh:
                for row in csv.DictReader(fh):
                    try:
                        alt = float(row.get("alt_diff") or 999)
                        az  = float(row.get("az_diff") or 999)
                        sep = math.sqrt(alt**2 + az**2)
                        fid = row.get("fa_flight_id") or row.get("id", "?")
                        target = row.get("target", "?")
                        key = (fid, target, date_str)
                        if key not in best or sep < best[key]["sep"]:
                            best[key] = {
                                "date": date_str,
                                "timestamp": row.get("timestamp", ""),
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
                                "possibility_level": int(row.get("possibility_level") or 0),
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

    events = sorted(best.values(), key=lambda x: x["sep"])
    return jsonify(events)


# Register telescope control routes
telescope_routes.register_routes(app)

# Start transit monitor
from src.transit_monitor import get_monitor
transit_monitor = get_monitor()
transit_monitor.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Flymoon Transit Monitor")
    parser.add_argument("--test", action="store_true", help="Use test data (deprecated, use --demo)")
    parser.add_argument("--demo", action="store_true", help="Use mock demonstration data with guaranteed classifications")
    args = parser.parse_args()

    test_mode = args.test or args.demo

    if test_mode:
        mode = "DEMO" if args.demo else "TEST"
        logger.info(f"🎭 Starting in {mode} mode - using mock data")

    # Find available port in range 8000-8100
    import socket
    port = None
    for p in range(8000, 8101):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('0.0.0.0', p))
            sock.close()
            port = p
            break
        except OSError:
            continue
    
    if port is None:
        logger.error("❌ No available ports in range 8000-8100")
        print("❌ No available ports in range 8000-8100")
        exit(1)
    
    print(f"🚀 Starting server on port {port}")
    
    # Reduce werkzeug logging noise
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.WARNING)
    
    app.run(host="0.0.0.0", port=port, debug=False)
