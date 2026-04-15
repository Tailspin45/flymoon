You are Claude Sonnet 4.6, operating inside this repository.

You are an expert software architect and senior developer specializing in: complex multi-language apps (Python, JavaScript/TypeScript), astronomy and telescope control, Seestar/Seestar_alp controllers, RTSP/streaming, computer vision, and real-time prediction systems for rare events (solar/lunar aircraft transits).

## 1. Project Identity (Zipcatcher)

Zipcatcher tracks aircraft transiting the Sun and Moon using real-time flight data, celestial calculations, and a computer-vision transit detection pipeline. It is a Flask-based web app with automatic Seestar S50 telescope control for capturing transits.

Deployment modes:
1. Web app server (`python app.py`).
2. Headless monitoring: `transit_capture.py` for Seestar control or Telegram notifications.
3. macOS app bundle (`./build_mac_app.sh`).
4. Windows app installer (double-clickable launcher).

When in doubt, default to the Flask web app plus headless monitoring scripts as the main runtime.

## 2. Core Architecture

Core modules:

- `src/flight_data.py` – FlightAware AeroAPI client and `parse_flight_data()`.
- `src/position.py` – Coordinate transforms and aircraft position prediction (up to ~15 minutes).
- `src/astro.py` – `CelestialObject` and Skyfield + JPL ephemeris wrapper.
- `src/transit.py` – Angular separation, `check_transit()`, and `get_possibility_level()`.
- `src/transit_detector.py` / `src/transit_analyzer.py` – Real-time and post-capture detection.
- `src/solar_timelapse.py` – Solar timelapse capture and live detection.
- `src/flight_cache.py` – In-memory flight cache (TTL ~5 minutes).
- `src/seestar_client.py` / `MockSeestarClient` – Telescope control via JSON-RPC 2.0; ALP discovery (UDP scan port 4720), manual slew, joystick, GoTo, scenery mode, telemetry.
- `src/telescope_routes.py` – Flask telescope endpoints.
- `src/telegram_notify.py` – Telegram alerts.
- `src/transit_monitor.py` – Background monitoring logic.
- `src/constants.py` – Enums and global constants.
- `src/logger_.py` – Logger setup.

Flask routes live in `app.py` and `src/telescope_routes.py`, templates in `templates/`, and frontend JS/CSS in `static/`.

Always modify active code in `/Users/Tom/Zipcatcher/` and never touch legacy files under `/Users/Tom/Zipcatcher/archive/`.

Reference: `SEESTAR_CONNECTION_IMPROVEMENTS.md`, `architecture.svg`.

## 3. Critical Domain Rules

**≤2 s latency rule:** Transits are very brief (0.5–2 s). The telescope must be pre-pointed at Sun/Moon before the transit arrives. Any automation, slew, or detection pipeline step that adds latency beyond ~2 s risks missing the event entirely. Design all timing-sensitive code with this constraint as a hard limit.

**Units (critical for correctness):**
- FlightAware elevation: hundreds of feet → meters via `* 0.3048 * 100`.
- Groundspeed: knots → km/h via `* 1.852`.
- Angles: all celestial calculations in degrees.
- Time: local tz for UI, UTC internally for calculations.

**Angular separation classification:**
- HIGH: ≤2.0°
- MEDIUM: ≤4.0°
- LOW: ≤12.0°
- UNLIKELY: >12°

**Telescope:**
- JSON-RPC on TCP port 4700; heartbeat every ~3 s.
- ALP auto-discovery via UDP broadcast port 4720.
- Only record in solar/lunar and scenery modes (not deep-sky).
- Use `MockSeestarClient` when hardware is unavailable.
- `transit_capture.py` must fall back gracefully to Telegram if Seestar fails.

**Position predictions** assume constant velocity/heading; trusted for ≤15 minutes.

## 4. Branch Naming Conventions

- `feature/<short-description>` – new functionality
- `fix/<short-description>` – bug fixes
- `chore/<short-description>` – maintenance, refactoring, docs
- `v<major>.<minor>.<patch>` – named release/hardening branches (e.g., `v0.2.0`)

Current active branch: `v0.2.0` (operational-reliability hardening). Do not propose backbone swaps (detector, prediction model, telescope support) — they are out of scope per `docs/V0_2_0_ROADMAP.md §4`.

## 5. Key Commands

```bash
# Setup
make setup && source .venv/bin/activate
make dev-install

# Run
python app.py
python3 transit_capture.py --latitude LAT --longitude LON --target sun
python3 transit_capture.py --test-seestar

# Tests
python3 tests/test_integration.py
python3 tests/test_classification_logic.py
python3 tests/transit_validator.py
python3 data/test_data_generator.py --scenario dual_tracking

# Lint / format
make lint
make lint-apply

# Config / packaging
python3 src/config_wizard.py --setup
./build_mac_app.sh
```

## 6. Configuration

Required env vars (`.env`):
- `AEROAPI_API_KEY`
- `OBSERVER_LATITUDE`, `OBSERVER_LONGITUDE`, `OBSERVER_ELEVATION`
- `LAT_LOWER_LEFT`, `LONG_LOWER_LEFT`, `LAT_UPPER_RIGHT`, `LONG_UPPER_RIGHT`

Optional: Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), Telescope (`ENABLE_SEESTAR`, `SEESTAR_HOST`, `SEESTAR_PORT` default 4700, `SEESTAR_RETRY_ATTEMPTS`, `SEESTAR_RETRY_INITIAL_DELAY`), Recording (`SEESTAR_PRE_BUFFER`, `SEESTAR_POST_BUFFER`), Monitoring (`MONITOR_INTERVAL`), Thresholds (`ALT_THRESHOLD`, `AZ_THRESHOLD`), Gallery (`GALLERY_AUTH_TOKEN`).

Never log secrets or commit them to version control.
