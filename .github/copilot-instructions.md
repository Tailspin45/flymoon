# Flymoon Development Guide

## Overview

Flymoon tracks aircraft transiting the Sun and Moon using real-time flight data and celestial calculations. It provides a Flask-based web interface with automatic telescope control for capturing transits.

**Deployment Modes:**
- **Web Application**: Flask server with interactive map (default, `app.py`)
- **Headless Scripts**: Background monitoring with push notifications
  - `monitor_transits.py` - Pushbullet notifications only
  - `transit_capture.py` - Automated telescope control or Telegram notifications
- **macOS App**: Double-clickable application bundle for `transit_capture.py`

## Build & Development Commands

```bash
# Setup (creates venv, installs deps, creates .env from .env.mock)
make setup

# Install dev dependencies (black, isort, autoflake)
make dev-install

# Lint (check only)
make lint

# Auto-format code
make lint-apply

# Run application
python app.py  # Access at http://localhost:8000
```

**Testing:** No automated test suite exists. Manual testing via web interface.

**Code Quality:**
- Uses black (line length 88), isort (profile=black), and autoflake
- Python 3.9+ required

## Architecture

### Core Calculation Flow

1. **Flight Data** (`src/flight_data.py`) - Fetches aircraft from FlightAware AeroAPI within bounding box
2. **Position Prediction** (`src/position.py`) - Predicts aircraft position up to 15 minutes ahead (constant velocity/heading assumption)
3. **Celestial Tracking** (`src/astro.py`) - Calculates Sun/Moon altitude/azimuth using Skyfield + JPL ephemeris (de421.bsp)
4. **Transit Detection** (`src/transit.py`) - Uses numerical optimization to find minimum angular separation between aircraft and target
5. **Probability Classification** - Simple thresholds assuming 1° target size (0.5° sun/moon + 0.5° margin):
   - **High** (🟢): alt_diff ≤ 1° AND az_diff ≤ 1° (direct transit very likely)
   - **Medium** (🟠): alt_diff ≤ 2° AND az_diff ≤ 2° (near miss, worth recording)
   - **Low** (⚪): alt_diff ≤ 3° AND az_diff ≤ 3° (possible distant transit)

### Key Components

**Flask Routes (`app.py`):**
- `/` - Main web interface
- `/flights` - Query flights in bounding box with transit predictions
- `/flights/<id>/route` - Flight route data (forward path)
- `/flights/<id>/track` - Historical track data
- `/telescope/*` - Telescope control endpoints (see `src/telescope_routes.py`)
- `/gallery` - Transit image gallery

**Telescope Integration (`src/seestar_client.py`):**
- Direct JSON-RPC 2.0 over TCP (no bridge apps needed)
- Default port: 4700 (configurable via SEESTAR_PORT)
- Heartbeat every 3 seconds to prevent timeout
- TransitRecorder schedules pre/post-buffered video capture (default: 10s before/after)

**Notifications (`src/telegram_notify.py`):**
- Sends alerts for medium/high probability transits
- Uses python-telegram-bot library

### Data Flow

```
FlightAware API → parse_fligh_data() → predict_position() → geographic_to_altaz()
                                                              ↓
CelestialObject.update_position() ← Skyfield + de421.bsp ←   ↓
                                                              ↓
                                    get_transits() ← minimize angular separation
                                                              ↓
                                    PossibilityLevel classification
                                                              ↓
                                    Frontend map + Telegram + Telescope
```

## Key Conventions

### Units & Conversions

- **Elevation:** FlightAware returns hundreds of feet → convert to meters (`* 0.3048 * 100`)
- **Speed:** FlightAware groundspeed in knots → convert to km/h (`* 1.852`)
- **Angles:** All celestial calculations use degrees
- **Time:** Uses local timezone via tzlocal, UTC internally for calculations

### Environment Variables

Required in `.env`:
- `AEROAPI_API_KEY` - FlightAware API key (preferred; legacy aliases `AEROAPI_KEY` and `FLIGHTAWARE_API_KEY` are also supported)
- Observer position: `OBSERVER_LATITUDE`, `OBSERVER_LONGITUDE`, `OBSERVER_ELEVATION`
- Bounding box: `LAT_LOWER_LEFT`, `LONG_LOWER_LEFT`, `LAT_UPPER_RIGHT`, `LONG_UPPER_RIGHT`

Optional:
- Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Telescope: `ENABLE_SEESTAR`, `SEESTAR_HOST`, `SEESTAR_PORT`, `SEESTAR_TIMEOUT`
- Recording buffers: `SEESTAR_PRE_BUFFER`, `SEESTAR_POST_BUFFER`
- Headless monitoring: `MONITOR_INTERVAL`, `PUSH_BULLET_API_KEY`
- Transit detection: `ALT_THRESHOLD`, `AZ_THRESHOLD` (default: 1.0°)
- Transit detection: `ALT_THRESHOLD` (default: 1.0°), `AZ_THRESHOLD` (default: 1.0°)

### Constants (`src/constants.py`)

- `ASTRO_EPHEMERIS` - Loaded Skyfield ephemeris (sun, moon, earth)
- `EARTH_TIMESCALE` - Skyfield timescale for time conversions
- `INTERVAL_IN_SECS` - Prediction sampling interval
- `TOP_MINUTE` - Maximum prediction window (15 minutes)
- `Altitude` - Enum-like classification: LOW (<10,000m), MEDIUM (10k-20k), MEDIUM_HIGH (20k-30k), HIGH (>30k)
- `PossibilityLevel` - UNLIKELY, LOW, MEDIUM, HIGH

### Logging

Uses custom logger (`src/logger_.py`) - imported as `from src import logger`

### Flight Data Structure

Parsed flights (`parse_fligh_data()`) return:
```python
{
    "name": str,           # Flight ident
    "aircraft_type": str,
    "fa_flight_id": str,
    "origin": str,         # City name
    "destination": str,    # City name or "N/D"
    "latitude": float,
    "longitude": float,
    "direction": float,    # Heading in degrees
    "speed": float,        # km/h (converted from knots)
    "elevation": float,    # Meters (for calculations)
    "elevation_feet": int, # Feet (for display)
    "elevation_change": str  # "C" (climbing), "D" (descending), "-" (level)
}
```

### Telescope Control

**JSON-RPC Methods:**
- `start_record_avi` - Start MP4 recording (params: {"raw": false})
- `stop_record_avi` - Stop recording
- `scope_get_equ_coord` - Heartbeat check (gets telescope coordinates)

**Recording Workflow:**
1. Check if Seestar is in Solar/Lunar mode (not deep sky)
2. Calculate transit time with buffers
3. Schedule recording start (default 10s before)
4. Schedule recording stop (default 10s after)
5. Actual transit lasts 0.5-2 seconds

### Transit Detection Thresholds

Simple angular separation thresholds assuming 1° target size (0.5° sun/moon + 0.5° margin for near misses):
- **HIGH (🟢)**: ≤1° in both altitude and azimuth - Direct transit very likely
- **MEDIUM (🟠)**: ≤2° in both altitude and azimuth - Near miss, worth recording  
- **LOW (⚪)**: ≤3° in both altitude and azimuth - Possible distant transit
- **UNLIKELY**: >3° separation

Thresholds are configurable via `.env` variables `ALT_THRESHOLD` and `AZ_THRESHOLD` (default: 1.0°).

## Important Notes

- **No automated tests** - Changes require manual verification via web UI
- **15-minute prediction window** - Assumes constant aircraft velocity/heading (acceptable for short timeframes)
- **FlightAware rate limits** - Personal tier: 10 queries/minute
- **Transit brevity** - Aircraft transits last <2 seconds, automation is critical
- **Telescope must be pre-pointed** - Seestar should already be tracking Sun/Moon before transit
- **JPL ephemeris file** - `de421.bsp` must exist in root directory (downloaded on first Skyfield use)
- **Configuration wizard** - Run `python3 src/config_wizard.py --setup` for interactive config validation

## File Organization

```
/
├── app.py                 # Flask application entry point
├── monitor_transits.py    # Standalone monitoring script (Pushbullet)
├── transit_capture.py     # Transit capture with notifications (Telegram/Seestar)
├── build_mac_app.sh       # macOS .app builder script
├── Transit Monitor.app    # macOS application bundle (generated)
├── src/                   # Core modules
│   ├── astro.py          # Celestial calculations (CelestialObject)
│   ├── transit.py        # Transit detection & optimization
│   ├── flight_data.py    # FlightAware API interface
│   ├── position.py       # Coordinate transforms & prediction
│   ├── seestar_client.py # Direct telescope control (JSON-RPC)
│   ├── telescope_routes.py # Flask routes for telescope
│   ├── telegram_notify.py  # Telegram alerts
│   ├── config_wizard.py    # Interactive configuration tool
│   ├── constants.py        # Global constants & enums
│   └── logger_.py          # Logging setup
├── static/               # Frontend assets (JS, CSS)
├── templates/            # Jinja2 HTML templates
├── data/                 # Flight logs, gallery images
└── de421.bsp            # JPL planetary ephemeris (generated)
```

## External Dependencies

- **Skyfield** - High-precision celestial calculations
- **Flask** - Web framework
- **FlightAware AeroAPI** - Real-time flight data
- **python-telegram-bot** - Telegram notifications
- **Leaflet.js** (frontend) - Interactive map visualization
