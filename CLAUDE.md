# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Flymoon tracks aircraft transiting the Sun and Moon using real-time flight data and celestial calculations. It's a Flask-based web application with automatic telescope control (Seestar S50) for capturing transits.

**Three Deployment Modes:**
1. **Web Application** - Flask server with interactive map (`python app.py`)
2. **Headless Monitoring** - Background scripts for automated monitoring:
   - `monitor_transits.py` - Pushbullet notifications only
   - `transit_capture.py` - Seestar control or Telegram notifications
3. **macOS App** - Double-clickable application bundle (built via `./build_mac_app.sh`)

## Common Commands

### Setup & Installation
```bash
# Initial setup (creates venv, installs deps, creates .env from .env.mock)
make setup
source .venv/bin/activate

# Install dev tools (black, isort, autoflake)
make dev-install
```

### Running the Application
```bash
# Web interface (access at http://localhost:8000)
python app.py

# Headless monitoring with Pushbullet notifications
python3 monitor_transits.py --latitude LAT --longitude LON --target sun

# Automated telescope capture or Telegram fallback
python3 transit_capture.py --latitude LAT --longitude LON --target sun

# Test Seestar telescope connection
python3 transit_capture.py --test-seestar
```

### Code Quality
```bash
# Check formatting/linting (no changes)
make lint

# Auto-format all code
make lint-apply
```

### Testing
```bash
# Integration test with synthetic data
python3 tests/test_integration.py

# Classification logic tests
python3 tests/test_classification_logic.py

# Validate transit detection
python3 tests/transit_validator.py

# Generate test data
python3 data/test_data_generator.py --scenario dual_tracking
```

**Note:** No automated test suite exists. Most testing is done manually via the web interface.

### Configuration
```bash
# Interactive configuration wizard
python3 src/config_wizard.py --setup

# Build macOS app bundle
./build_mac_app.sh
```

## Architecture

### Core Transit Detection Pipeline

1. **Flight Data** (`src/flight_data.py`)
   - Fetches aircraft from FlightAware AeroAPI within bounding box
   - Returns: position, speed, heading, elevation, origin/destination

2. **Position Prediction** (`src/position.py`)
   - Projects aircraft position up to 15 minutes ahead
   - Assumes constant velocity/heading (acceptable for short timeframes)
   - Function: `predict_position()`

3. **Celestial Tracking** (`src/astro.py`)
   - Calculates Sun/Moon altitude/azimuth using Skyfield + JPL ephemeris (de421.bsp)
   - Class: `CelestialObject` with `update_position()`

4. **Angular Separation** (`src/transit.py`)
   - Uses numerical optimization to find minimum angular distance between aircraft path and target
   - Function: `check_transit()` → returns min separation time and angles

5. **Probability Classification** (`src/transit.py:get_possibility_level()`)
   - **HIGH** (🟢): ≤1° separation in both alt/az (direct transit very likely)
   - **MEDIUM** (🟠): ≤2° separation (near miss, worth recording)
   - **LOW** (⚪): ≤3° separation (possible distant transit)
   - **UNLIKELY**: >3° separation

### Flask Application Structure

**Main Routes** (`app.py`):
- `/` - Main web interface
- `/flights` - Query flights in bounding box with transit predictions
- `/flights/<id>/route` - Forward flight route data
- `/flights/<id>/track` - Historical track data
- `/telescope/*` - Telescope control (see `src/telescope_routes.py`)
- `/gallery` - Transit image gallery (write operations require auth token)

**Telescope Integration** (`src/seestar_client.py`):
- Direct JSON-RPC 2.0 over TCP (port 4700, no bridge apps needed)
- Heartbeat every 3 seconds to prevent timeout
- `TransitRecorder` class schedules pre/post-buffered video (default: 10s before/after transit)
- Mock client available for testing: `MockSeestarClient`

**Key JSON-RPC Methods:**
- `iscope_start_view` - Start solar/lunar mode (params: {"mode": 1} for sun, 2 for moon)
- `iscope_stop_view` - Stop viewing mode
- `start_record_avi` - Start MP4 recording (params: {"raw": false})
- `stop_record_avi` - Stop recording
- `scope_get_equ_coord` - Heartbeat check (gets telescope coordinates)

### Data Flow

```
FlightAware API → parse_fligh_data() → predict_position() → geographic_to_altaz()
                                                             ↓
CelestialObject.update_position() ← Skyfield + de421.bsp ←  ↓
                                                             ↓
                                   get_transits() ← minimize angular separation
                                                             ↓
                                   PossibilityLevel classification
                                                             ↓
                                   Frontend + Telegram + TransitRecorder
```

### Flight Cache System

- `src/flight_cache.py` - In-memory cache to avoid redundant API calls
- Uses `fa_flight_id` as cache key
- Stores: flight data, route data, track data
- Default TTL: 5 minutes

## Key Conventions

### Units & Conversions

**CRITICAL:** FlightAware uses different units than calculations require:
- **Elevation:** FlightAware returns hundreds of feet → convert to meters via `* 0.3048 * 100`
- **Speed:** FlightAware groundspeed in knots → convert to km/h via `* 1.852`
- **Angles:** All celestial calculations use degrees
- **Time:** Uses local timezone (tzlocal), UTC internally for calculations

### Environment Variables

**Required** (in `.env`):
- `AEROAPI_API_KEY` - FlightAware API key
- `OBSERVER_LATITUDE`, `OBSERVER_LONGITUDE`, `OBSERVER_ELEVATION` - Observer position
- `LAT_LOWER_LEFT`, `LONG_LOWER_LEFT`, `LAT_UPPER_RIGHT`, `LONG_UPPER_RIGHT` - Bounding box

**Optional:**
- Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Telescope: `ENABLE_SEESTAR` (true/false), `SEESTAR_HOST`, `SEESTAR_PORT` (default: 4700)
- Recording: `SEESTAR_PRE_BUFFER` (default: 10s), `SEESTAR_POST_BUFFER` (default: 10s)
- Monitoring: `MONITOR_INTERVAL` (minutes), `PUSH_BULLET_API_KEY`
- Thresholds: `ALT_THRESHOLD` (default: 1.0°), `AZ_THRESHOLD` (default: 1.0°)
- Gallery: `GALLERY_AUTH_TOKEN` - Required for write operations on /gallery endpoints

### Parsed Flight Data Structure

`parse_fligh_data()` returns:
```python
{
    "name": str,              # Flight ident (e.g., "UAL1234")
    "aircraft_type": str,     # Aircraft model (e.g., "B738")
    "fa_flight_id": str,      # FlightAware unique ID
    "origin": str,            # Origin city
    "destination": str,       # Destination city or "N/D"
    "latitude": float,
    "longitude": float,
    "direction": float,       # Heading in degrees (0-360)
    "speed": float,           # km/h (converted from knots)
    "elevation": float,       # Meters (for calculations)
    "elevation_feet": int,    # Feet (for display)
    "elevation_change": str   # "C" (climbing), "D" (descending), "-" (level)
}
```

### Constants (`src/constants.py`)

- `ASTRO_EPHEMERIS` - Loaded Skyfield ephemeris (sun, moon, earth)
- `EARTH_TIMESCALE` - Skyfield timescale for time conversions
- `INTERVAL_IN_SECS` - Prediction sampling interval
- `TOP_MINUTE` - Maximum prediction window (15 minutes)
- `Altitude` - Enum-like: LOW, MEDIUM, MEDIUM_HIGH, HIGH
- `PossibilityLevel` - UNLIKELY, LOW, MEDIUM, HIGH

### Logging

Uses custom logger from `src/logger_.py`:
```python
from src import logger
logger.info("message")
logger.warning("message")
logger.error("message")
```

## Important Development Notes

### Project Directory Structure

**CRITICAL:** This project has a legacy archive directory. Always edit files in the main directory:
- **EDIT HERE:** `/Users/Tom/flymoon/` - Active source code
- **DO NOT EDIT:** `/Users/Tom/flymoon/archive/development/dist/Flymoon-Web/` - Outdated distribution copy

### Transit Detection Assumptions

- **15-minute window** - Aircraft maintain constant velocity/heading (acceptable for short timeframes, accuracy degrades beyond 10 minutes)
- **Transit brevity** - Aircraft transits last 0.5-2 seconds, automation is critical
- **Pre-pointing required** - Telescope must already be tracking Sun/Moon before transit occurs
- **1° target size** - Classification thresholds assume 0.5° for Sun/Moon + 0.5° margin

### API Rate Limits

- FlightAware Personal tier: 10 queries/minute
- Use flight cache (`src/flight_cache.py`) to minimize API calls
- Cache expires after 5 minutes

### Telescope Control Considerations

- **Firmware compatibility** - JSON-RPC timeouts reported on firmware 6.70
- **Mock mode available** - Use `MockSeestarClient` for testing without hardware
- **Graceful fallback** - `transit_capture.py` falls back to Telegram notifications if Seestar connection fails
- **Heartbeat critical** - Socket times out without periodic keepalive (every 3s)
- **Mode-specific recording** - Only record in solar/lunar modes, not deep sky

### Security

- Gallery write operations require `GALLERY_AUTH_TOKEN` in `.env`
- Server binds to `0.0.0.0:8000` (accessible on LAN)
- See `SECURITY.md` for production deployment guidance

### External Dependencies

- **Skyfield** - Celestial calculations (downloads `de421.bsp` on first run)
- **FlightAware AeroAPI** - Real-time flight data
- **python-telegram-bot** - Notifications
- **Leaflet.js** - Frontend map (self-hosted in `static/`, not CDN)

### File Organization

```
/
├── app.py                    # Flask application entry
├── monitor_transits.py       # Standalone monitoring (Pushbullet)
├── transit_capture.py        # Transit capture (Telegram/Seestar)
├── build_mac_app.sh          # macOS app builder
├── Makefile                  # Build commands
├── src/                      # Core modules
│   ├── astro.py             # CelestialObject, Skyfield wrapper
│   ├── transit.py           # Transit detection & classification
│   ├── flight_data.py       # FlightAware API client
│   ├── flight_cache.py      # In-memory flight data cache
│   ├── position.py          # Coordinate transforms & prediction
│   ├── seestar_client.py    # Direct telescope control (JSON-RPC)
│   ├── telescope_routes.py  # Flask telescope endpoints
│   ├── telegram_notify.py   # Telegram alerts
│   ├── transit_monitor.py   # Background monitoring logic
│   ├── config_wizard.py     # Interactive config tool
│   ├── constants.py         # Global constants & enums
│   └── logger_.py           # Logging setup
├── static/                   # Frontend JS/CSS
│   ├── app.js               # Main application logic
│   ├── map.js               # Leaflet map & visualization
│   ├── telescope.js         # Telescope control UI
│   └── leaflet*.js/css      # Self-hosted Leaflet library
├── templates/                # Jinja2 HTML
│   ├── index.html           # Main interface
│   └── telescope.html       # Telescope control page
├── data/                     # Flight logs, gallery images
├── tests/                    # Manual tests
└── de421.bsp                 # JPL ephemeris (auto-downloaded)
```

## Common Development Tasks

### Adding a New Transit Classification Level

1. Add enum value to `PossibilityLevel` in `src/constants.py`
2. Update `get_possibility_level()` in `src/transit.py`
3. Update frontend color coding in `static/map.js`
4. Update documentation in README.md

### Adding a New Telescope Command

1. Add JSON-RPC method to `SeestarClient._send_command()` in `src/seestar_client.py`
2. Add public method to `SeestarClient` class
3. Add Flask endpoint to `src/telescope_routes.py`
4. Add UI controls to `templates/telescope.html` and `static/telescope.js`
5. Add mock implementation to `MockSeestarClient` for testing

### Modifying Transit Detection Thresholds

- **Simple:** Change `ALT_THRESHOLD` and `AZ_THRESHOLD` in `.env`
- **Advanced:** Modify `get_possibility_level()` in `src/transit.py:60-87`

### Testing Without Hardware

1. Set `ENABLE_SEESTAR=false` in `.env` (or omit)
2. For code testing, use `MockSeestarClient` from `src/telescope_routes.py`
3. Generate test flight data: `python3 data/test_data_generator.py`
