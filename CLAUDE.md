You are Claude Code, operating inside this repository. Follow these instructions exactly for fast, reliable, and **token‑efficient** work.

## 1. Role and Goals

- Primary goal: correctly complete the current coding task with minimal back‑and‑forth.
- Default stance: act autonomously, do not narrate obvious steps, and keep responses concise.
- **Never show code in responses unless the user explicitly asks to see it.**
- Prefer small, incremental changes that compile, pass tests, and are easy to review.

If the task is unclear at a **high** level, ask one short clarifying question before doing substantial work.

## 2. Permissions

You have standing permission to:

- Read, edit, create, move, and delete files in this repository.
- Run any shell command you need (git, python, node, npm, pip, make, curl, docker, etc.).
- Install or upgrade packages, run tests, start/stop servers, and manage local services.
- Kill processes, free ports, and restart dev servers.
- Make commits and push to the current branch when the user asks or when it clearly finishes a task.

Never ask for approval to run a command. All necessary commands are pre‑approved.

## 3. Token‑Efficient Workflow

Optimize both prompt and output length:

1. **Skim, then focus**
   - Quickly scan only the files and directories that are relevant to the task.
   - Avoid reading large files end‑to‑end unless absolutely required; prefer targeted searches.

2. **Minimize chatter**
   - Keep explanations short and to the point.
   - Avoid restating the prompt, repo overview, or long summaries of obvious behavior.
   - Use bullet lists for plans and results instead of long paragraphs.

3. **Targeted plans, not essays**
   - For non‑trivial tasks, first produce a compact plan (3–7 bullets).
   - Execute the plan step‑by‑step; only update the user with what changed and how to run/check it.

4. **Compact code diffs**
   - When showing changes, prefer minimal diffs or edited functions/blocks, not entire large files.
   - If a file is very long, show only the relevant portions and describe other changes in a sentence.

5. **Reuse context**
   - Refer back to existing functions, patterns, and utilities rather than rewriting similar logic.
   - Prefer small helper functions and configuration changes over large refactors unless requested.

6. **Bound output length**
   - Default to concise answers that a human can read quickly.
   - Only provide extended explanations when explicitly requested (e.g., “explain in detail”).

## 4. Project Context (Flymoon)

Flymoon tracks aircraft transiting the Sun and Moon using real‑time flight data, celestial calculations, and a computer‑vision transit detection pipeline. It is a Flask‑based web app with automatic Seestar S50 telescope control for capturing transits.

Deployment modes:

1. Web app server (`python app.py`).
2. Headless monitoring scripts:
   - `transit_capture.py` for Seestar control or Telegram notifications.
3. macOS app bundle (via `./build_mac_app.sh`).
4. Windows app installer (double‑clickable launcher).

When in doubt, default to the Flask web app plus headless monitoring scripts as the main runtime.

## 5. Key Commands (for quick reference)

Setup and dev:

```bash
make setup          # create venv, install deps, create .env from .env.mock
source .venv/bin/activate
make dev-install    # install dev tools (black, isort, autoflake)
Run:

bash
python app.py
python3 transit_capture.py --latitude LAT --longitude LON --target sun
python3 transit_capture.py --test-seestar
Quality and tests:

bash
make lint           # check formatting/lint
make lint-apply     # auto-format
python3 tests/test_integration.py
python3 tests/test_classification_logic.py
python3 tests/transit_validator.py
python3 data/test_data_generator.py --scenario dual_tracking
Config and packaging:

bash
python3 src/config_wizard.py --setup
./build_mac_app.sh
Use these commands directly; do not ask whether you may run them.

6. Architecture Cheatsheet
Core modules:

src/flight_data.py – FlightAware AeroAPI client and parse_fligh_data().

src/position.py – Coordinate transforms and aircraft position prediction (up to ~15 minutes).

src/astro.py – CelestialObject and Skyfield + JPL ephemeris wrapper.

src/transit.py – Angular separation, check_transit(), and get_possibility_level().

src/transit_detector.py / src/transit_analyzer.py – Real‑time and post‑capture detection.

src/flight_cache.py – In‑memory flight cache (TTL ~5 minutes).

src/seestar_client.py / MockSeestarClient – Telescope control via JSON‑RPC 2.0.

src/telescope_routes.py – Flask telescope endpoints.

src/telegram_notify.py – Telegram alerts.

src/transit_monitor.py – Background monitoring logic.

src/constants.py – Enums and global constants.

src/logger_.py – Logger setup.

Flask routes live in app.py and src/telescope_routes.py, templates in templates/, and frontend JS/CSS in static/.

Always modify active code in /Users/Tom/flymoon/ and never touch legacy files under /Users/Tom/flymoon/archive/development/dist/Flymoon-Web/.

7. Important Domain Rules
Units and conversions (critical for correctness):

FlightAware elevation: hundreds of feet → meters via * 0.3048 * 100.

Groundspeed: knots → km/h via * 1.852.

Angles: all celestial calculations in degrees.

Time: local tz for UI, UTC internally for calculations.

Transit assumptions:

Position predictions assume constant velocity/heading and are trusted for ~15 minutes.

Transits are very brief (0.5–2 s); automation and pre‑pointing the telescope at Sun/Moon are required.

Angular separation classification:

HIGH: ≤1.5°

MEDIUM: ≤2.5°

LOW: ≤3.0°

UNLIKELY: >3°

Telescope:

Use Seestar JSON‑RPC on TCP port 4700 with a heartbeat every ~3 s.

Only record in solar/lunar modes, not deep‑sky.

Use MockSeestarClient when hardware is unavailable.

transit_capture.py must fall back gracefully (e.g., to Telegram) if Seestar fails.

8. Configuration and Secrets
Environment variables (in .env):

Required:

AEROAPI_API_KEY

OBSERVER_LATITUDE, OBSERVER_LONGITUDE, OBSERVER_ELEVATION

LAT_LOWER_LEFT, LONG_LOWER_LEFT, LAT_UPPER_RIGHT, LONG_UPPER_RIGHT

Optional (examples):

Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Telescope: ENABLE_SEESTAR, SEESTAR_HOST, SEESTAR_PORT (default 4700)

Recording: SEESTAR_PRE_BUFFER, SEESTAR_POST_BUFFER

Monitoring: MONITOR_INTERVAL

Thresholds: ALT_THRESHOLD, AZ_THRESHOLD

Gallery: GALLERY_AUTH_TOKEN

Never log secrets or commit them to version control. Keep configuration changes minimal and documented.

9. Common Tasks
When asked to:

Add a transit classification level:

Update PossibilityLevel in src/constants.py.

Adjust get_possibility_level() in src/transit.py.

Update frontend colors in static/map.js.

Update any docs (e.g., README).

Add a telescope command:

Add JSON‑RPC handling in SeestarClient._send_command() and public method wrapper.

Expose via Flask in src/telescope_routes.py.

Add UI controls in static/telescope.js.

Extend MockSeestarClient to match.

Change transit thresholds:

Simple: tweak env vars (ALT_THRESHOLD, AZ_THRESHOLD).

Advanced: modify get_possibility_level() logic.

Test without hardware:

Set ENABLE_SEESTAR=false.

Use MockSeestarClient.

Generate synthetic flights via data/test_data_generator.py.

For any change, prefer:

Small, isolated edits.

Tests or at least a quick manual check path.

A short explanation of what you changed and how to verify it.

10. Response Style
Default to short, direct answers.

Show enough code to be unambiguous, but avoid flooding the user with large files.

When you finish a meaningful chunk of work, briefly summarize:

What changed.

How to run or verify.

Any follow‑ups or TODOs you recommend.

If you are unsure between multiple reasonable approaches and the choice impacts API usage, UX, or performance, ask one focused question and then proceed with the chosen approach once answered.