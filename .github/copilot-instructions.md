# Zipcatcher Copilot Instructions (Current)

## What this repository is now

Zipcatcher is a Flask app plus headless scripts for predicting, detecting, and recording aircraft transits across the Sun/Moon. It includes:

- Real-time transit prediction (`src/transit.py`)
- Live CV detection + post-analysis (`src/transit_detector.py`, `src/transit_analyzer.py`)
- Seestar control via JSON-RPC and ALPACA (`src/seestar_client.py`, `src/alpaca_client.py`, `src/telescope_routes.py`)
- Solar timelapse support (`src/solar_timelapse.py`)
- Multi-source flight data (FlightAware + optional OpenSky/ADS-B sources)

## Development commands

```bash
make setup          # create .venv, install requirements, create .env from .env.mock
make dev-install    # install dev tools
make lint           # black/isort/autoflake checks
make lint-apply     # auto-format
python app.py       # run web app on localhost:8000
```

Useful scripts:

```bash
python3 transit_capture.py --latitude LAT --longitude LON --target sun
python3 src/config_wizard.py --setup
./build_mac_app.sh
```

## Testing reality

There is an automated Python test suite under `tests/` (pytest-style tests and diagnostics). Do not assume “manual-only testing.”

Common tests used in this repo include:

- `python3 tests/test_integration.py`
- `python3 tests/test_classification_logic.py`
- `python3 tests/test_transit_detection.py`
- `python3 tests/test_flask_routes.py`

## Core behavior to preserve

- Prediction horizon: **15 minutes** (`TOP_MINUTE = 15`).
- Celestial calculations are in **degrees**.
- FlightAware conversions:
  - elevation hundreds-of-feet -> meters: `* 0.3048 * 100`
  - groundspeed knots -> km/h: `* 1.852`
- `EARTH_TIMESCALE.from_datetime()` inputs must be timezone-aware datetimes.

### Transit classification (current)

In `src/transit.py`, `get_possibility_level(sep)` uses angular separation thresholds:

- HIGH: `<= 2.0°`
- MEDIUM: `<= 4.0°`
- LOW: `<= 12.0°`
- UNLIKELY: `> 12.0°`

Do not revert these to old 1/2/3-degree thresholds.

## Key routes and surfaces

Main Flask routes in `app.py` include:

- `/`, `/config`, `/flights`, `/flights/<id>/route`, `/flights/<id>/track`
- `/telescope` (page shell), `/transit-log`
- `/api/transit-events`, `/api/transit-events/label`
- `/api/cnn/retrain`, `/api/cnn/retrain/status`

Telescope API routes are registered from `src/telescope_routes.py` and include:

- Connection/discovery: `/telescope/discover`, `/telescope/connect`, `/telescope/status`
- Motion/control: `/telescope/goto`, `/telescope/nudge`, `/telescope/alpaca/*`
- Capture/detection: `/telescope/recording/*`, `/telescope/detect/*`
- Timelapse: `/telescope/timelapse/*`
- Files/analysis: `/telescope/files/*`, `/telescope/composite`
- Preview stream: `/telescope/preview/stream.mjpg`

## Configuration reality (`.env.mock`)

Required baseline:

- `AEROAPI_API_KEY`
- `OBSERVER_LATITUDE`, `OBSERVER_LONGITUDE`, `OBSERVER_ELEVATION`

Important optional groups now in active use:

- Security: `GALLERY_AUTH_TOKEN`
- Seestar JSON-RPC: `ENABLE_SEESTAR`, `SEESTAR_HOST`, `SEESTAR_PORT`, `SEESTAR_TIMEOUT`
- Seestar retry: `SEESTAR_RETRY_ATTEMPTS`, `SEESTAR_RETRY_INITIAL_DELAY`
- Seestar ALPACA: `SEESTAR_ALPACA_PORT`, `SEESTAR_ALPACA_TIMEOUT`
- RTSP/buffers: `SEESTAR_RTSP_PORT`, `SEESTAR_PRE_BUFFER`, `SEESTAR_POST_BUFFER`
- Solar timelapse: `SOLAR_TIMELAPSE_*`
- Flight sources: `OPENSKY_*`, `ADSB_*`, `OPENAIP_API_KEY`

Bounding-box env vars (`LAT/ LONG _LOWER_LEFT/UPPER_RIGHT`) still exist as fallback, but dynamic transit-corridor logic is now first-class.

## Editing guidance

- Keep changes small and behavior-safe.
- Reuse existing helpers and module patterns.
- Maintain Flask route contracts used by `static/` frontend code.
- When changing telescope behavior, keep `MockSeestarClient` and real client behavior aligned.
- Update docs when behavior/config changes.
