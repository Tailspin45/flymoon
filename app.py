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
- /gallery - Transit image gallery

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

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from src.constants import POSSIBLE_TRANSITS_LOGFILENAME, get_aeroapi_key

# SETUP
load_dotenv()

from src import logger
from src.config_wizard import ConfigWizard
from src.flight_data import save_possible_transits, sort_results
from src.flight_cache import get_cache
from src.telegram_notify import send_telegram_notification
from src.transit import get_transits
from src import telescope_routes
from src.seestar_client import TransitRecorder
from src.constants import PossibilityLevel

# Global test/demo mode flag
test_mode = False

# Validate configuration on startup
wizard = ConfigWizard()
if not wizard.validate(interactive=False):
    print("\n⚠️  Configuration issues detected:")
    print(wizard.get_status_report())
    print("\n💡 Run 'python3 src/config_wizard.py --setup' to configure\n")

app = Flask(__name__)

# Gallery configuration
UPLOAD_FOLDER = 'static/gallery'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB limit

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
    - 480s (8 min) otherwise
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
        return 480  # 8 minutes
    
    closest_transit_time = min(priority_transits)
    
    if closest_transit_time < 2:  # <2 min away
        return 30  # 30 seconds
    elif closest_transit_time < 5:  # <5 min away
        return 60  # 1 minute
    elif closest_transit_time < 10:  # <10 min away
        return 120  # 2 minutes
    else:
        return 480  # 8 minutes (default)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def get_config():
    """Return app configuration for client."""
    return jsonify({
        "autoRefreshIntervalMinutes": int(os.getenv("AUTO_REFRESH_INTERVAL_MINUTES", 8)),
        "cacheEnabled": True,
        "cacheTTLSeconds": 120
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

        latitude = float(request.args["latitude"])
        longitude = float(request.args["longitude"])
        elevation = float(request.args["elevation"])
        min_altitude = float(request.args.get("min_altitude", 15))
        alt_threshold = float(request.args.get("alt_threshold", 5.0))
        az_threshold = float(request.args.get("az_threshold", 10.0))
        has_send_notification = request.args["send-notification"] == "true"

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
        
        # Helper to check if target is above minimum altitude
        from src.astro import CelestialObject
        from src.position import get_my_pos
        from src.constants import ASTRO_EPHEMERIS
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from tzlocal import get_localzone_name
        
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
                data = get_transits(latitude, longitude, elevation, target, test_mode, alt_threshold, az_threshold)
                
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
                logger.info(f"{target.capitalize()} below minimum altitude ({coords['altitude']:.1f}° < {min_altitude}°), skipping transit check")
        
        # Calculate adaptive refresh interval based on closest transit
        next_check_interval = calculate_adaptive_interval(all_flights)
        
        # Combine results
        data = {
            "flights": sort_results(all_flights),
            "targetCoordinates": target_coordinates,
            "trackingTargets": tracking_targets,
            "nextCheckInterval": next_check_interval,  # Seconds until next check
            "weather": None,  # Weather functionality not implemented yet
            "boundingBox": {
                "latLowerLeft": custom_bbox["lat_lower_left"] if custom_bbox else float(os.getenv("LAT_LOWER_LEFT", 0)),
                "lonLowerLeft": custom_bbox["lon_lower_left"] if custom_bbox else float(os.getenv("LONG_LOWER_LEFT", 0)),
                "latUpperRight": custom_bbox["lat_upper_right"] if custom_bbox else float(os.getenv("LAT_UPPER_RIGHT", 0)),
                "lonUpperRight": custom_bbox["lon_upper_right"] if custom_bbox else float(os.getenv("LONG_UPPER_RIGHT", 0)),
            }
        }

        end_time = time.time()
        elapsed_time = end_time - start_time
        logger.info(f"Elapsed time: {elapsed_time} seconds")

        if not test_mode:
            try:
                date_ = date.today().strftime("%Y%m%d")
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
                asyncio.run(send_telegram_notification(data["flights"], "sun/moon"))
            except Exception as e:
                logger.error(f"Error while trying to send Telegram notification. Details:\n{str(e)}")

        # Schedule automatic recordings for high-probability transits
        transit_recorder = get_transit_recorder()
        if transit_recorder:
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
        logger.error(f"Invalid parameter value: {e}")
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


@app.route("/flights/<fa_flight_id>/track")
def get_flight_track(fa_flight_id):
    """Get the historical track positions for a specific flight."""
    API_KEY = get_aeroapi_key()
    url = f"https://aeroapi.flightaware.com/aeroapi/flights/{fa_flight_id}/track"
    headers = {"Accept": "application/json; charset=UTF-8", "x-apikey": API_KEY}

    try:
        response = requests.get(url=url, headers=headers, timeout=10)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": f"API returned status {response.status_code}"}), response.status_code
    except Exception as e:
        logger.error(f"Error fetching track for {fa_flight_id}: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/gallery")
def gallery():
    """Display the transit image gallery page."""
    return render_template("gallery.html")


@app.route("/telescope")
def telescope():
    """Display the telescope control page."""
    return render_template("telescope.html")


@app.route("/gallery/upload", methods=['POST'])
def upload_transit_image():
    """Upload a transit image with metadata."""
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    if file and allowed_file(file.filename):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        flight_id = request.form.get('flight_id', 'UNKNOWN').replace('/', '_')
        ext = file.filename.rsplit('.', 1)[1].lower()

        # Create year/month directories
        now = datetime.now()
        year_month_path = os.path.join(app.config['UPLOAD_FOLDER'], str(now.year), f"{now.month:02d}")
        os.makedirs(year_month_path, exist_ok=True)

        # Save image
        filename = secure_filename(f"{timestamp}_{flight_id}.{ext}")
        filepath = os.path.join(year_month_path, filename)
        file.save(filepath)

        # Save metadata
        metadata = {
            "flight_id": request.form.get('flight_id', ''),
            "aircraft_type": request.form.get('aircraft_type', ''),
            "timestamp": datetime.now().isoformat(),
            "target": request.form.get('target', ''),
            "caption": request.form.get('caption', ''),
            "equipment": request.form.get('equipment', ''),
            "observer_lat": request.form.get('observer_lat', ''),
            "observer_lon": request.form.get('observer_lon', ''),
        }

        metadata_path = filepath.rsplit('.', 1)[0] + '.json'
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)

        logger.info(f"Uploaded transit image: {filename}")
        return jsonify({"success": True, "filename": filename}), 200

    return jsonify({"error": "Invalid file type. Allowed: png, jpg, jpeg, gif"}), 400


@app.route("/gallery/list")
def list_gallery():
    """List all gallery images with metadata."""
    gallery_path = app.config['UPLOAD_FOLDER']
    images = []

    # Create gallery directory if it doesn't exist
    os.makedirs(gallery_path, exist_ok=True)

    # Walk directory structure
    for root, dirs, files in os.walk(gallery_path):
        for file in files:
            if file.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, 'static')
                # Use forward slashes for web paths
                rel_path = rel_path.replace('\\', '/')
                metadata_path = full_path.rsplit('.', 1)[0] + '.json'

                metadata = {}
                if os.path.exists(metadata_path):
                    try:
                        with open(metadata_path, 'r') as f:
                            metadata = json.load(f)
                    except Exception as e:
                        logger.error(f"Error reading metadata for {file}: {str(e)}")

                images.append({
                    "path": rel_path,
                    "filename": file,
                    "full_path": full_path,  # For delete operations
                    "metadata": metadata
                })

    # Sort by timestamp (most recent first)
    images.sort(key=lambda x: x['metadata'].get('timestamp', ''), reverse=True)
    return jsonify(images)


@app.route("/gallery/delete/<path:filepath>", methods=['DELETE'])
def delete_gallery_image(filepath):
    """Delete a gallery image and its metadata."""
    try:
        # Security check - ensure filepath is within gallery directory
        full_path = os.path.join('static', filepath)
        abs_path = os.path.abspath(full_path)
        gallery_abs = os.path.abspath(app.config['UPLOAD_FOLDER'])

        if not abs_path.startswith(gallery_abs):
            return jsonify({"error": "Invalid file path"}), 403

        # Delete image file
        if os.path.exists(abs_path):
            os.remove(abs_path)
            logger.info(f"Deleted image: {filepath}")

        # Delete metadata file
        metadata_path = abs_path.rsplit('.', 1)[0] + '.json'
        if os.path.exists(metadata_path):
            os.remove(metadata_path)
            logger.info(f"Deleted metadata: {metadata_path}")

        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error(f"Error deleting image {filepath}: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/gallery/update/<path:filepath>", methods=['POST'])
def update_gallery_metadata(filepath):
    """Update metadata for a gallery image."""
    try:
        # Security check - ensure filepath is within gallery directory
        full_path = os.path.join('static', filepath)
        abs_path = os.path.abspath(full_path)
        gallery_abs = os.path.abspath(app.config['UPLOAD_FOLDER'])

        if not abs_path.startswith(gallery_abs):
            return jsonify({"error": "Invalid file path"}), 403

        # Get metadata file path
        metadata_path = abs_path.rsplit('.', 1)[0] + '.json'

        # Read existing metadata
        metadata = {}
        if os.path.exists(metadata_path):
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)

        # Update with new values from request
        metadata.update({
            "flight_id": request.form.get('flight_id', metadata.get('flight_id', '')),
            "aircraft_type": request.form.get('aircraft_type', metadata.get('aircraft_type', '')),
            "target": request.form.get('target', metadata.get('target', '')),
            "caption": request.form.get('caption', metadata.get('caption', '')),
            "equipment": request.form.get('equipment', metadata.get('equipment', '')),
            "observer_lat": request.form.get('observer_lat', metadata.get('observer_lat', '')),
            "observer_lon": request.form.get('observer_lon', metadata.get('observer_lon', '')),
        })

        # Save updated metadata
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)

        logger.info(f"Updated metadata for: {filepath}")
        return jsonify({"success": True, "metadata": metadata}), 200
    except Exception as e:
        logger.error(f"Error updating metadata for {filepath}: {str(e)}")
        return jsonify({"error": str(e)}), 500


# Register telescope control routes
telescope_routes.register_routes(app)


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
        exit(1)
    
    logger.info(f"🚀 Starting server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
