# Zipcatcher — Seestar Aircraft Transit Tracker

**Predict, detect, and photograph aircraft crossing the Sun or Moon in real time.**

<p align="center">
  <img src="static/images/flymoon-hero.jpg" alt="Zipcatcher — Aircraft Transit Tracker" width="100%">
</p>

---

## What Zipcatcher Does

Capturing an aircraft silhouette against the solar or lunar disc is a rare and technically demanding shot. The geometry has to be nearly perfect, the timing is measured in fractions of a second, and the Seestar has to be tracking the Sun or Moon before the aircraft arrives. Zipcatcher automates every part of that problem.

It continuously monitors live flight traffic, projects each aircraft's path against the solar or lunar disc using high-precision ephemeris data, ranks candidates by how close they will come, and — when a high-probability transit is imminent — commands a Seestar telescope to start recording automatically. After the session it analyses the footage and produces an annotated composite image showing the aircraft's full track across the disc.

**Key capabilities:**

- Predicts transits up to **15 minutes ahead** using real-time flight data from six concurrent sources
- Displays flight paths, altitudes, and probability on a live **interactive map**
- Controls a **Seestar** via direct TCP — no bridge app required
- Detects aircraft in the live RTSP stream using a **frame-coherence computer-vision pipeline**
- Runs a **Convolutional Neural Network (CNN) transit classifier** trained on real detection clips to separate genuine transits from false positives
- Produces **annotated composite images** from recorded video
- Sends **Telegram alerts** with flight details and predicted transit time
- Runs **headlessly overnight** on Mac, Linux, or Windows

---

## Quick Start

### Prerequisites

- Python 3.9 +

**Flight data sources** — Zipcatcher queries up to six sources concurrently and merges the results. Most work with no account or API key:

| Source | Key required? | Notes |
|--------|--------------|-------|
| [OpenSky Network](https://opensky-network.org) | No | Free, community ADS-B network |
| [ADSB-One](https://api.adsb.one) | No | Free, no authentication |
| [adsb.lol](https://api.adsb.lol) | No | Free, no authentication |
| [adsb.fi](https://opendata.adsb.fi) | No | Free open-data API |
| [FlightAware AeroAPI](https://www.flightaware.com/aeroapi/signup/personal) | Yes — free personal tier | Adds airline/route metadata |
| [ADS-B Exchange](https://www.adsbexchange.com/data/) | Yes — `ADSBX_API_KEY` in `.env` | Optional; skip if you don't have one |
| Local receiver (dump1090 / tar1090) | No | Point `ADSB_LOCAL_URL` at your own RTL-SDR receiver — optional |

You can run Zipcatcher with zero API keys and still get solid coverage from the four free sources. Adding a FlightAware key enriches results with callsign, route, and aircraft-type data.

### Install and run

Full setup instructions → **[SETUP.md](SETUP.md)**

```bash
make setup                          # create venv, install deps, create .env from .env.mock
source .venv/bin/activate
python app.py                       # open http://localhost:8000
```

For headless operation with telescope control:

```bash
python3 transit_capture.py --latitude 51.5 --longitude -0.12 --target sun
```

---

## Transit Capture

<p align="center">
  <img width="1890" height="1048" alt="Screenshot 2026-04-06 at 11 59 35 AM" src="https://github.com/user-attachments/assets/23a9245b-4b15-4505-a19e-1d3ad287d5b3" />
</p>

### Prediction Pipeline


1. **Flight acquisition** — queries APIs for all aircraft inside the configured bounding box
2. **Position projection** — extrapolates constant-velocity/heading tracks up to 15 minutes ahead
3. **Celestial tracking** — computes Sun and Moon position with Skyfield + JPL DE421 ephemeris, including atmospheric refraction
4. **Angular separation** — numerical optimisation finds the moment of closest approach on-sky
5. **Probability classification** — ranks candidates using true angular separation, with azimuth differences cosine-weighted by target altitude to correct for geometric compression near the zenith

| Level | Separation | Meaning |
|-------|-----------|---------|
| 🟢 High | ≤ 2.0° | Direct transit very likely |
| 🟠 Medium | ≤ 4.0° | Near miss — worth recording |
| ⚪ Low | ≤ 12.0° | Possible distant transit |

### Live Video Detection

<p align="center">
<img width="369" height="753" alt="Screenshot 2026-04-07 at 8 05 40 AM" src="https://github.com/user-attachments/assets/adc376d0-8a51-4e52-94f4-85bcbf06b8b4" />
</p>

When the telescope is connected, **TransitDetector** monitors the live RTSP stream continuously. The detector uses a multi-stage coherence pipeline:

- **Score A** — spike gate: detects a large, sudden per-frame anomaly consistent with a fast-moving silhouette
- **Score B** — consecutive gate: confirms the anomaly persists across multiple frames in a straight line
- **Score B (MF)** — matched-filter gate: cross-correlates the signal against a bank of transit templates covering different speeds and sizes

All three gates require `score_a ≥ thresh_a` before they can accumulate, preventing background noise from triggering a false detection. A hard centre-ratio gate suppresses detections where the brightness anomaly is not centred in the disc. After a confirmed detection the detector enforces a 6-second cooldown; suppressed triggers during cooldown are logged once (not once per frame).

### Post-Capture Analysis

**TransitAnalyzer** processes saved video to produce:

- A **composite image** blending every frame where the aircraft was on the disc over a clean reference background
- A **sidecar JSON** with frame-level signal data (scores, thresholds, triggered frames, peak time) used to annotate the scrubber in the gallery viewer

### CNN Transit Classifier

A lightweight CNN runs over detection clips to score each event as a genuine transit versus a false positive. Training data is extracted automatically from confirmed captures and stored in `data/training/`. The classifier can be retrained from the telescope panel when new labelled clips are available.

### Detection Tester

In the telescope sidebar under **Live Detection**, the Detection Tester card provides rapid pipeline feedback without waiting for a real transit:

- **Inject** — inserts a synthetic transit and verifies the pipeline catches it
- **Sweep** — runs a size × speed matrix and reports which combinations are detected; highlights gaps in coverage
- **Validate** — runs the analyzer over all saved MP4s and reports events found per file

Two modes:

- **Default** — production thresholds; fewer false positives
- **Sensitive** — relaxed speed/travel gates, static filter disabled; better for slow or small objects

---

## Map Interface



- **Per-quadrant minimum altitude** — set independent minimum angles for North, East, South, and West to mask out obstructions; flights are only ranked when the target is above your local horizon. Click the centre to reset all quadrants to zero
- **Altitude bars** — thin bars on each flight indicator show cruising altitude at a glance
- **Route and track overlay** — click any indicator for planned route ahead and historical track behind
- **Azimuth arrows** — on-map arrows point toward the Sun and Moon from your observer position
- **Traffic density heatmap** — toggle 🔥 to reveal accumulated flight corridors built up across polling cycles (persists in browser storage, capped at 2,000 points)
- **Adjustable bounding box** — drag corners to resize the flight-search area

---

## Telescope Panel

<p align="center">
  <img width="1916" height="1047" alt="Screenshot 2026-04-06 at 11 55 42 AM" src="https://github.com/user-attachments/assets/df7e751e-72f0-49a5-b65b-c392bfd820ef" />
</p>

Zipcatcher connects directly to the Seestar over TCP on port 4700.

### Connection

- **Auto-discovery** — UDP broadcast scan on port 4720 finds the scope's IP automatically
- **Smart reconnect** — if the scope connection drops overnight, Zipcatcher waits until the target rises above the configured minimum altitude before attempting to reconnect, avoiding noisy retries in the middle of the night

### Imaging

- **Solar and lunar modes** — switches the scope to the correct imaging mode for the selected target
- **Scenery mode** — for manual positioning independent of the automated tracking
- **Automatic recording** — starts a configurable video pre-buffer before the predicted transit and stops after a post-buffer (defaults: 10 s each)
- **GoTo** — slew to any named location or entered alt/az coordinates
- **Continuous nudge** — fine-position the scope with hold-to-repeat joystick controls
- **Autofocus** — trigger a focus run from the panel
- **Live preview** — MJPEG stream from the scope displayed directly in the browser

### Focus Odometer

A per-session focus-step counter in the sidebar tracks how many focuser steps have been applied since the session started, helping you return to a known focus position after experimenting. ZWO has encrypted the position readout.

### ALPACA / seestar_alp

If you run a `seestar_alp` sidecar, set `SEESTAR_ALPACA_URL` in `.env` to expose the stable `/v1/seestar/*` ALPACA API. Zipcatcher will prefer ALPACA endpoints when available and fall back to direct RPC otherwise.

<p align="center">
<img width="391" height="673" alt="Alpaca panel" src="https://github.com/user-attachments/assets/02823808-ac62-4c69-9d1a-58679770f4bb" />

</p>

---

## Capture Gallery

The gallery (📁 **Captured Files** strip at the bottom of the scope panel) shows thumbnails of all recorded clips, detection frames, diff heatmaps, and analysed composites.

### File Viewer

 Click any thumbnail to open the file viewer:

<p align="center">
  <img width="1197" height="873" alt="Screenshot 2026-04-07 at 8 08 53 AM" src="https://github.com/user-attachments/assets/e54943d2-cd93-4637-9200-e71e4b95a36d" />

</p>

- **Five-panel frame display** — shows the current frame flanked by two frames on each side for context
- **Frame scrubber** — drag to seek; ◀◀ and ▶▶ buttons on either side of the frame counter play the clip in reverse or forward at native FPS; click again to stop
  
<p align="center">
  <img width="1827" height="387" alt="Screenshot 2026-04-07 at 8 13 09 AM" src="https://github.com/user-attachments/assets/efdd0277-0501-48a9-927d-9a8c14c52dd6" />
</p>

- **📌 Mark** — first tap sets the In point, second tap sets the Out point; re-tapping replaces whichever endpoint is nearest to the current frame. The trim row above the scrubber shows the current In and Out times live
- **✂️ Trim** — writes a new `trim_<filename>.mp4` alongside the original (non-destructive; the original is never modified). After trimming a **Replace Original** button appears if you want to discard the source
- **Transit analysis** — ☀️ Solar Transit / 🌙 Lunar Transit buttons run the post-capture analyzer and overlay signal data on the scrubber bar
- **Composite** — 🖼 Build Composite assembles marked frames into an annotated stack image
- **Filmstrip shift-select** — hold Shift to range-select multiple files for batch delete

### Data Sources Activity Panel

A collapsible **Data Sources** panel in the sidebar shows per-source activity odometers (FlightAware, OpenSky, OpenAIP) with the last-updated timestamp and request count for the current session.

<p align="center">
  <img width="386" height="141" alt="Screenshot 2026-04-07 at 8 26 02 AM" src="https://github.com/user-attachments/assets/26bd6099-8fc0-4754-bab2-ba34f2cf229b" />
</p>

---

## Solar Eclipse Timelapse

During a solar eclipse, Zipcatcher switches to timelapse mode: it captures frames at a configurable interval throughout the event and assembles them into a timelapse video. Aircraft transits detected during the eclipse are bookmarked as timestamped events within the recording.

<p align="center">
<img width="1922" height="961" alt="Screenshot 2026-04-07 at 8 28 53 AM" src="https://github.com/user-attachments/assets/c35a82dc-d8c7-4076-b0d5-d72679d09275" />

</p>

Stabilisation (`SOLAR_TIMELAPSE_STABILIZE=true`) compensates for atmospheric jitter between frames.

Auto-resume (`SOLAR_TIMELAPSE_AUTO_RESUME=true`) restarts today's timelapse automatically after a reconnect or restart without requiring manual intervention.

---

## Notifications

**Telegram** alerts fire for medium and high-probability transits, including predicted transit time, flight callsign, altitude, aircraft type, and angular separation. Alerts can be muted per-session from the panel without restarting the server.

---

## Headless / Background Mode

### `transit_capture.py`

```bash
# Telescope + Telegram
python3 transit_capture.py --latitude 51.5 --longitude -0.12 --target sun

# Telegram only (no scope)
python3 transit_capture.py --latitude 51.5 --longitude -0.12 --target sun --manual
```

### macOS App Bundle

Build the signed DMG installer from the repo root — the build script handles everything:

```bash
cd electron && npx electron-builder --mac
```

The output DMG is written to `../dist-electron/`. Open it and drag Zipcatcher to Applications.

### Windows System Tray

```cmd
pip install -r requirements-windows.txt
python windows_monitor.py
```

Tray icon: **gray** = idle · **green** = monitoring · **orange** = transit detected · **red** = error.

---

## Configuration

Copy `.env.mock` to `.env` and fill in the values relevant to your setup. Run `python3 src/config_wizard.py --setup` for interactive validation.

| Variable | Purpose |
|----------|---------|
| `AEROAPI_API_KEY` | FlightAware AeroAPI key (required if used) |
| `OBSERVER_LATITUDE / LONGITUDE / ELEVATION` | Your location |
| `LAT/LONG_LOWER_LEFT / UPPER_RIGHT` | Flight search bounding box |
| `TELEGRAM_BOT_TOKEN / CHAT_ID` | Telegram alerts (optional) |
| `ENABLE_SEESTAR / SEESTAR_HOST` | Telescope control (optional) |
| `SEESTAR_ALPACA_URL` | seestar_alp ALPACA sidecar URL (optional) |
| `SEESTAR_PRE_BUFFER / POST_BUFFER` | Recording window in seconds (default: 10) |
| `SOLAR_TIMELAPSE_AUTO_RESUME` | Auto-resume today's timelapse after reconnect (`true`/`false`) |
| `SOLAR_TIMELAPSE_INTERVAL` | Seconds between timelapse frames (default: 120) |
| `SOLAR_TIMELAPSE_STABILIZE` | Stabilize timelapse frames (`true`/`false`) |
| `MIN_TARGET_ALTITUDE` | Minimum target altitude for reconnect logic (default: 10°) |
| `GALLERY_AUTH_TOKEN` | Token required for gallery write operations |

---

## Documentation

| File | Contents |
|------|---------|
| [QUICKSTART.md](QUICKSTART.md) | Fastest path to first detection |
| [SETUP.md](SETUP.md) | Full setup — Telegram, Telescope, Windows |
| [SECURITY.md](SECURITY.md) | Securing the server on a LAN |
| [ATTRIBUTION.md](ATTRIBUTION.md) | Open-source library credits |

---

## Security

Zipcatcher binds to `0.0.0.0:8000` by default (LAN-accessible). Gallery write operations require a `GALLERY_AUTH_TOKEN`. See [SECURITY.md](SECURITY.md) before exposing the server beyond your local network.

---

## Contributing

Issues and pull requests welcome — especially transit photographs.

---

## Credits

| Component | Project | Licence |
|-----------|---------|---------|
| Original idea and foundation code: [David Bettancort Montebello](https://github.com/dbetm/flymoon) | Public Domain
| Interactive map | [Leaflet 1.9.4](https://leafletjs.com) © Vladimir Agafonkin | BSD 2-Clause |
| Bounding-box drawing | [Leaflet.Editable](https://github.com/Leaflet/Leaflet.editable) © Yoann Aubineau | MIT |
| Traffic heatmap | [Leaflet.heat](https://github.com/Leaflet/Leaflet.heat) © Vladimir Agafonkin | MIT |
| Celestial calculations | [Skyfield 1.49](https://rhodesmill.org/skyfield/) © Brandon Rhodes | MIT |
| Web framework | [Flask 3.0.3](https://flask.palletsprojects.com) © Pallets | BSD 3-Clause |
| Telegram alerts | [python-telegram-bot 21.0](https://python-telegram-bot.org) | LGPLv3 |
| JPL Ephemeris | [DE421](https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/) — NASA/JPL | Public Domain |
| Free flight positions | [OpenSky Network](https://opensky-network.org) | [Terms](https://opensky-network.org/about/terms-of-use) |
| Aviation chart overlay | [OpenAIP](https://www.openaip.net) | CC BY-NC-SA 4.0 |

See [ATTRIBUTION.md](ATTRIBUTION.md) for full licence texts.

---

## Licence

MIT — see [LICENSE](LICENSE)

---

*Pro tip: keep Flightradar24 open alongside Zipcatcher for extra situational awareness when a high-probability transit is approaching.*
