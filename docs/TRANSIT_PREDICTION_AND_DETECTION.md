# Flymoon: Transit Prediction and Detection
## Technical Reference for Future Development

**Date:** March 2026  
**Codebase revision:** `dc5722d` (main)  
**Status:** Describes the system *as implemented* — not aspirational.

---

## Table of Contents

1. [Overview and Architecture](#1-overview-and-architecture)
2. [Coordinate System and Celestial Reference](#2-coordinate-system-and-celestial-reference)
3. [Flight Data Acquisition](#3-flight-data-acquisition)
4. [Bounding Box Computation](#4-bounding-box-computation)
5. [Position Prediction Model](#5-position-prediction-model)
6. [Transit Probability Calculation](#6-transit-probability-calculation)
7. [The Prediction Pipeline: get_transits()](#7-the-prediction-pipeline-get_transits)
8. [Prediction Modes and Data Sources](#8-prediction-modes-and-data-sources)
9. [Client-Side Soft Refresh](#9-client-side-soft-refresh)
10. [Background Monitor: TransitMonitor](#10-background-monitor-transitmonitor)
11. [Headless Capture: transit_capture.py](#11-headless-capture-transit_capturepy)
12. [Automatic Recording Trigger (Web App)](#12-automatic-recording-trigger-web-app)
13. [Live Transit Detection: TransitDetector](#13-live-transit-detection-transitdetector)
14. [Post-Capture Analysis: TransitAnalyzer](#14-post-capture-analysis-transitanalyzer)
15. [Solar Timelapse (Parallel Imaging Pipeline)](#15-solar-timelapse-parallel-imaging-pipeline)
16. [Notifications](#16-notifications)
17. [Known Limitations and Failure Modes](#17-known-limitations-and-failure-modes)
18. [Data Flow Diagrams](#18-data-flow-diagrams)

---

## 1. Overview and Architecture

Flymoon tracks commercial aircraft transiting the solar or lunar disc. A transit is an event in which an aircraft passes across the face of the Sun or Moon as seen by an observer on the ground — an event that lasts typically 0.5–2 seconds.

The system has two independent pipelines that run simultaneously:

**A. Predictive pipeline**  
Uses real-time aircraft position data (OpenSky Network) combined with orbital mechanics (Skyfield + JPL DE421 ephemeris) to compute the predicted closest angular approach of each aircraft to the Sun or Moon over the next 15 minutes. This pipeline drives the web UI map, Telegram alerts, and automated telescope-recording scheduling.

**B. Detection pipeline**  
Uses live RTSP video from a Seestar S50 solar/lunar telescope to analyze each frame for the visual signature of an aircraft crossing the solar or lunar disc. This pipeline is independent of the predictive pipeline and can detect transits that were not predicted, or validate that a predicted transit occurred.

These pipelines are linked at two points:
- After a live detection, the detector queries OpenSky to identify which aircraft was responsible.
- During a predicted HIGH-probability event, the web app schedules a recording window and optionally triggers the detector.

---

## 2. Coordinate System and Celestial Reference

### Ephemeris
`src/constants.py` loads the JPL DE421 binary planetary ephemeris at startup:
```python
ASTRO_EPHEMERIS = load("de421.bsp")
```
This provides accurate heliocentric and geocentric positions for the Sun, Moon, and planets from approximately 1900–2050.

### Observer position
`src/position.py::get_my_pos()` creates a Skyfield `Topos` object from the observer's WGS-84 latitude, longitude, and elevation (metres above the geoid). This is the geodetic reference for all subsequent alt/az calculations.

### Celestial object positioning
`src/astro.py::CelestialObject` wraps a Skyfield body (Sun or Moon from DE421) and an observer `Topos`. On `update_position(ref_datetime)` it calls:
```
(body - earth).at(time).observe(body).apparent().altaz()
```
This returns apparent altitude and azimuth (degrees), correcting for aberration and atmospheric refraction at the observer. The result is stored in `.altitude.degrees` and `.azimuthal.degrees`.

### Aircraft position in alt/az
`src/position.py::geographic_to_altaz()` uses the same Skyfield framework: `wgs84.latlon(lat, lon, elevation)` creates a temporary observer at the aircraft's position; `(aircraft - observer).at(time).altaz()` gives the aircraft's direction as seen from the ground observer.

### Altitude reference
All altitudes in the prediction pipeline are referenced to the local astronomical horizon (perpendicular to local gravity at the observer). Azimuth is measured clockwise from **True North** — not magnetic north.

---

## 3. Flight Data Acquisition

### Primary source: OpenSky Network
`src/opensky.py::fetch_opensky_positions()` queries:
```
GET https://opensky-network.org/api/states/all
    ?lamin=&lomin=&lamax=&lomax=&extended=1
```
This returns state vectors for all aircraft within the bounding box. The `extended=1` parameter adds the ADS-B emitter category (index 17).

**State vector fields used (by index):**
| Index | Field | Unit |
|-------|-------|------|
| 0 | ICAO 24-bit address | hex |
| 1 | Callsign | string |
| 4 | Last contact | Unix epoch |
| 5 | Longitude | degrees |
| 6 | Latitude | degrees |
| 7 | Barometric altitude | metres |
| 8 | On ground | bool |
| 9 | Velocity | m/s |
| 10 | True track | degrees |
| 11 | Vertical rate | m/s |
| 13 | Geometric altitude | metres (preferred over baro) |
| 14 | Squawk | string |
| 16 | Position source | 0=ADS-B, 1=ASTERIX, 2=MLAT, 3=FLARM |
| 17 | Emitter category | integer |

**Staleness gate:** positions with `last_contact` older than 30 seconds are silently discarded before being returned. At 900 km/h a 30-second stale position drifts approximately 7.5 km (~1.4° at 200 km range), which is enough to produce a false UNLIKELY result for a true HIGH event.

**Caching:** results are cached per bounding-box key (3 decimal places) for 60 seconds (`CACHE_TTL`). Rate-limit (HTTP 429) triggers a 300-second backoff.

**Authentication:** OAuth2 client credentials (`OPENSKY_CLIENT_ID` + `OPENSKY_CLIENT_SECRET`) → legacy basic auth → anonymous. Anonymous limit: ~100 API credits/day.

**Ground filter:** Aircraft with `on_ground=True` are discarded before the prediction loop.

### Optional: FlightAware AeroAPI
`src/flight_data.py::get_flight_data()` queries the FlightAware AeroAPI bbox search endpoint. This is **not used by default** for prediction — the comment in `get_transits()` is explicit:
```python
# FlightAware is never used for prediction. Only for post-capture enrichment.
if data_source == "fa-only":
    data_source = "opensky-only"
```

FlightAware is used in two narrow cases:
1. **FA enrichment (optional, HIGH only):** `_enrich_from_fa()` in `transit.py` fetches aircraft type, origin city, and destination city for HIGH-probability transits when `enrich=True`. This is **not** enabled on the main web request (`enrich=False` in `app.py`). Cost: ~$0.02 per call. Cached per callsign for 2 hours.
2. **Legacy/explicit `fa-only` mode:** Now rerouted to `opensky-only` (dead mode in practice).

**FlightAware unit conventions (parsing):**
- Altitude: hundreds of feet → metres: `alt_hundreds * 0.3048 * 100`
- Groundspeed: knots → km/h: `knots * 1.852`

### Local ADS-B receiver (stub)
`data_source="adsb-local"` reads `ADSB_LOCAL_URL` from env but **falls back to OpenSky** — the local receiver path is not implemented.

---

## 4. Bounding Box Computation

The bounding box determines which aircraft are fetched from OpenSky. An optimal box includes all aircraft that could geometrically transit the target within the prediction window.

### Dynamic corridor box (default)
`src/position.py::transit_corridor_bbox()` computes a physically meaningful box:

1. **Transit ground point (TGP):** The ground point directly below the virtual intersection of the observer-to-target line of sight with the aircraft's cruise altitude. This is where an aircraft on that exact line of sight would need to be right now.
   - `alt_clamped = max(target_alt_deg, 3°)` — prevents division by near-zero for low targets.
   - `ground_dist_km = (aircraft_alt_m / 1000) / tan(alt_clamped)` — capped at 500 km.
   - Step along the observer's azimuth by `ground_dist_km` using spherical navigation.

2. **Search radius:** All aircraft that could reach the TGP (or its neighbourhood) within the 15-minute prediction window:
   - `travel_km = max_speed_kmh * (time_window / 60)` where `max_speed_kmh = 950`.
   - Expanded by 20% of `ground_dist_km` as a geometric buffer.
   - Capped at 600 km total radius.

3. **Lat/lon deltas:** `radius_deg ≈ radius_km / 111.32`; longitude scaled by `1/cos(lat)`.

**Priority:** custom UI bbox → dynamic corridor → `.env` fallback (legacy).

The `.env` fallback (`LAT_LOWER_LEFT`, `LONG_LOWER_LEFT`, `LAT_UPPER_RIGHT`, `LONG_UPPER_RIGHT`) is a static rectangle — useful for zero-config deployments but not geometry-aware.

---

## 5. Position Prediction Model

### Aircraft position (dead-reckoning)
`src/position.py::predict_position()` extrapolates the aircraft's ground position using **constant-velocity/constant-heading great-circle motion**:

```
d = (speed_kmh / 60) * minutes_ahead   [km]
φ₂ = arcsin(sin φ₁ cos(d/R) + cos φ₁ sin(d/R) cos θ)
λ₂ = λ₁ + atan2(sin θ sin(d/R) cos φ₁, cos(d/R) − sin φ₁ sin φ₂)
```

where `R = 6371 km`, `φ` = latitude (radians), `λ` = longitude (radians), `θ` = true track heading (radians from north).

**The critical assumption:** speed and heading are constant for the full 15-minute window. This is a reasonable approximation for en-route cruising flights but breaks for aircraft in departure or approach phases, or those executing turns.

**Time limit:** The prediction loop runs from `t=0` to `t=15` minutes in `INTERVAL_IN_SECS=5`-second steps, giving 180 time samples per aircraft. An early-exit heuristic aborts after 3 continuous minutes of the combined differential increasing (see Section 6).

### Track velocity override
`app.py` maintains `_track_velocity_cache` keyed by `fa_flight_id`. When a FlightAware track (sequence of historical fixes) is available, `compute_track_velocity()` computes instantaneous speed and heading from the last two fixes using the Haversine formula. This overrides the OpenSky instantaneous velocity for the `/transits/recalculate` (soft refresh) path, providing better heading accuracy for aircraft already seen.

### Aircraft altitude
The prediction uses the aircraft's **current reported altitude** (geometric preferred, else barometric). No vertical rate correction is applied — the aircraft is modelled as flying at a constant altitude for the full 15-minute window. This introduces error for climbing/descending aircraft, but the position-age and staleness warnings flag the most problematic cases.

---

## 6. Transit Probability Calculation

### Angular separation function
`src/transit.py::angular_separation()` implements the spherical law of cosines for on-sky angular distance:

```
cos θ = sin(alt₁) sin(alt₂) + cos(alt₁) cos(alt₂) cos(Δaz)
θ = arccos(cos θ)   [clamped to [-1, 1]]
```

This is the correct spherical distance between two points on the celestial sphere expressed in altitude-azimuth coordinates. It handles near-zenith geometry correctly (where azimuth differences are geometrically compressed) and is numerically stable across all separations.

Two legacy functions exist but are not on the primary hot path:
- `_angular_separation(alt_diff, az_diff, target_alt)` — cosine-weighted Euclidean approximation (deprecated).
- `calculate_angular_separation(alt_diff, az_diff)` — simple Euclidean (used only in tests).

### Classification thresholds
`src/transit.py::get_possibility_level(sep)` maps angular separation in degrees to `PossibilityLevel`:

| Level | Integer value | Threshold | Interpretation |
|-------|---------------|-----------|----------------|
| `HIGH` | 3 | ≤ 2.0° | Aircraft will likely transit the disc |
| `MEDIUM` | 2 | ≤ 4.0° | Near miss; recording worthwhile |
| `LOW` | 1 | ≤ 12.0° | Geometrically interesting; display only |
| `UNLIKELY` | 0 | > 12.0° | Not a transit candidate |

The Sun's angular diameter is ~0.53° and the Moon's is ~0.50°. Therefore HIGH (≤2.0°) is approximately 4× the disc diameter — it captures the full disc plus a generous margin for position uncertainty.

### check_transit() — per-aircraft trajectory scan

`src/transit.py::check_transit()` is the inner loop called once per aircraft per refresh cycle.

**Inputs:**
- Aircraft position dict (lat, lon, altitude_m, speed_kmh, heading, etc.)
- `window_time`: numpy array of 180 time steps from 0 to 15 minutes in 5-second increments
- `ref_datetime`: current local time (timezone-aware)
- `MY_POSITION`: Skyfield Topos for observer
- Precomputed `target_positions`: dict mapping integer minute index → (alt_deg, az_deg)

**Algorithm:**

```
min_diff_combined ← ∞
min_sep_seen ← ∞
no_decreasing_count ← 0

for each t in window_time (180 steps × 5s):
    future_lat, future_lon ← predict_position(lat, lon, speed, heading, t)
    future_alt, future_az  ← geographic_to_altaz(future_lat, future_lon, alt_m, t)
    t_alt, t_az            ← target_positions[int(t)]  # precomputed; no Skyfield call
    
    alt_diff = |future_alt - t_alt|
    az_diff  = min(|future_az - t_az|, 360 - |future_az - t_az|)  # shortest arc
    diff_combined = alt_diff + az_diff   # scalar screening metric
    
    if diff_combined improved:
        update closest_approach record
        reset no_decreasing_count
    else:
        no_decreasing_count += 1
    
    if no_decreasing_count ≥ 3 * (60/5) = 36:
        break  # aircraft is moving away; skip rest of window
    
    if future_alt > 0:
        sep ← angular_separation(t_alt, t_az, future_alt, future_az)
        if sep < min_sep_seen:
            min_sep_seen ← sep
            update response record with all fields
```

**Output:** The `response` dict at the minimum `sep` over the window. `is_possible_transit` is set to 0 if `possibility_level == UNLIKELY`, else 1. If the aircraft was always below the horizon, only `closest_approach` data is returned (no `angular_separation` field).

**Note on `diff_combined`:** The early-exit heuristic uses the sum `alt_diff + az_diff` (not spherical separation) as a cheap screening metric. The actual classification uses the spherical `angular_separation`. The two are consistent in direction (both decrease as the aircraft approaches) but not in magnitude.

---

## 7. The Prediction Pipeline: get_transits()

`src/transit.py::get_transits()` is the main entry point for the predictive system. It orchestrates data acquisition, per-aircraft trajectory evaluation, and optional enrichment.

### Call sequence

```
get_transits(latitude, longitude, elevation, target_name, 
             test_mode, custom_bbox, data_source, enrich)
│
├── get_my_pos()                      ← Skyfield Topos for observer
├── CelestialObject(target_name)      ← Sun or Moon via DE421
├── celestial_obj.update_position()   ← current alt/az
│
├── Transit corridor bbox selection
│   ├── custom_bbox from UI?          → use it
│   ├── target_alt > 0?               → transit_corridor_bbox()
│   └── else                          → .env fallback bbox
│
├── Data acquisition (by data_source)
│   ├── test_mode                     → load JSON file
│   ├── opensky-only / hybrid         → fetch_opensky_positions()
│   └── fa-only                       → get_flight_data() + FlightCacheCache (legacy)
│
├── Ground filter                     ← discard on_ground=True
│
├── Precompute target_positions[]     ← 16 Skyfield calls (one per minute, t=0..15)
│
├── ThreadPoolExecutor (max 8)
│   └── per aircraft: _check_and_enrich()
│       ├── check_transit()           ← 180-step trajectory scan
│       ├── stale position warning    ← if age>20s and HIGH/MEDIUM
│       └── _enrich_from_fa()         ← only if enrich=True and HIGH
│
└── Return {flights, targetCoordinates, bbox_used}
```

### Precomputed target positions
To avoid 50+ redundant Skyfield calls (one per flight per minute step), `get_transits()` precomputes the target's alt/az at each integer-minute step from 0 to 15:
```python
for step in range(16):  # 0..15 minutes
    celestial_obj.update_position(ref_datetime + timedelta(minutes=step))
    target_positions[step] = (celestial_obj.altitude.degrees, celestial_obj.azimuthal.degrees)
```
The `check_transit()` inner loop uses `target_positions[int(t)]` — the minute-quantized target position. The Sun moves ~0.008°/minute so quantization error is negligible.

### Parallelisation
Up to 8 aircraft are processed concurrently via `ThreadPoolExecutor`. Wall-clock time for a typical batch of 40 aircraft at 4 workers is ~0.25 seconds.

### Return value
```python
{
    "flights": [<per-aircraft result dict>],
    "targetCoordinates": {"altitude": ..., "azimuthal": ...},
    "bbox_used": {"latLowerLeft": ..., ...}
}
```
Each flight dict includes: `id`, `fa_flight_id`, `origin`, `destination`, `latitude`, `longitude`, `aircraft_elevation`, `speed`, `direction`, `angular_separation`, `alt_diff`, `az_diff`, `time` (minutes to closest approach), `target_alt`, `target_az`, `plane_alt`, `plane_az`, `possibility_level` (0–3), `is_possible_transit` (0/1), `elevation_change`, `position_source`, `position_age_s`, `position_stale`, `icao24`, `category`, `squawk`.

---

## 8. Prediction Modes and Data Sources

| Mode | env `data_source` | Traffic data | Flight metadata |
|------|-------------------|--------------|----------------|
| Hybrid (default) | `hybrid` | OpenSky bbox | None (callsign only) |
| OpenSky only | `opensky-only` | OpenSky bbox | None |
| FA enriched | `hybrid` + `enrich=True` | OpenSky bbox | FA per-callsign for HIGH |
| FA only (legacy) | `fa-only` | **Redirected → OpenSky** | — |
| Test/demo | `test_mode=True` | JSON file | From file |
| Local ADS-B (stub) | `adsb-local` | **Redirected → OpenSky** | — |

**Default for web UI:** hybrid, `enrich=False`. FlightAware is never called during the main prediction refresh.

**Default for TransitMonitor:** `opensky-only`, `enrich=False`.

**Default for transit_capture.py:** default args (hybrid), `enrich=False`.

---

## 9. Client-Side Soft Refresh

The browser UI implements a two-tier refresh system:

**Full refresh (`fetchFlights`)**
- `GET /flights?...&send-notification=true`
- Triggers a full `get_transits()` call (new OpenSky fetch).
- Default interval: `AUTO_REFRESH_INTERVAL_MINUTES` × 60 seconds (env; default 10 min).
- Adaptive shortening via `nextCheckInterval` returned by the server:
  - < 2 minutes to closest HIGH/MEDIUM: **30 s**
  - < 5 minutes: **60 s**
  - < 10 minutes: **120 s**
  - Otherwise: **600 s**
- 55-second browser fetch timeout.

**Soft refresh (`softRefresh`)** — every 15 seconds
- Dead-reckons existing aircraft positions using last known speed/heading.
- Decrements `time` (ETA) for each transit candidate.
- If any `is_possible_transit === 1` flight remains: `POST /transits/recalculate` with updated positions. The server re-evaluates geometry without an OpenSky call, using `_track_velocity_cache` overrides when available.
- Triggers if data is stale (>300 s since last full fetch).

**Position age colouring (UI table):**
- > 60 s: red
- > 30 s: orange
- > 5 s: yellow
- Angular separation colours: ≤ 2° green, ≤ 4° orange.

---

## 10. Background Monitor: TransitMonitor

`src/transit_monitor.py::TransitMonitor` runs a background thread in the web app process and in headless deployments, polling independently of the UI refresh cycle.

**Configuration:** `calc_interval=30` seconds (default).

**Loop logic:**
1. If both Sun and Moon are below the horizon: sleep 60 seconds.
2. For each target (`sun`, `moon`) not in `disabled_targets`:
   - Call `get_transits(data_source="opensky-only", enrich=False)`.
   - Filter results: keep only entries where `0 < time_minutes × 60 ≤ 300` (i.e., transits within the next 5 minutes) **and** `possibility_level ∈ {HIGH=3, MEDIUM=2}`.
3. Append new HIGH/MEDIUM transits to the daily CSV log via `save_possible_transits()`.
4. Sleep `calc_interval`.

**Purpose:** The monitor provides a near-real-time alert feed and logs all near-misses for post-session review. It does **not** trigger telescope recording directly; that is handled by `TransitRecorder` in the web app's `/flights` handler.

---

## 11. Headless Capture: transit_capture.py

`transit_capture.py` is a standalone script for unattended operation. It implements the same core prediction but wraps it with telescope control and/or Telegram notifications.

**Modes:**
- **Automatic** (default): Connects to Seestar S50 via `SeestarClient`; starts solar or lunar mode; on HIGH prediction, schedules a recording.
- **Manual**: Sends Telegram messages instead of controlling hardware.
- **Fallback:** If Seestar connection fails, falls back to manual/Telegram mode.

**Prediction:** Calls `get_transits()` directly (no Flask layer); filters `possibility_level == HIGH` only.

**Recording:** `asyncio.create_task(_execute_automatic_recording)`:
```
sleep(delay_to_transit - pre_buffer)
start_recording()
sleep(pre_buffer + transit_duration_estimate + post_buffer)
stop_recording()
```
`SEESTAR_PRE_BUFFER` / `SEESTAR_POST_BUFFER` env vars (default 10 s each). Transit duration estimate: 2 s (hardcoded).

**Loop interval:** `MONITOR_INTERVAL` env var (default 15 minutes). This is much coarser than the web-app adaptive polling — appropriate for unattended overnight operation.

---

## 12. Automatic Recording Trigger (Web App)

When the web app's `/flights` endpoint returns, the Flask process also:

1. Instantiates a `TransitRecorder` (lazy, once) with `pre_buffer=10s`, `post_buffer=10s`.
2. For each flight with `possibility_level == HIGH`:
   - Calls `schedule_transit_recording(flight_id, eta_seconds=time*60, transit_duration_estimate=2.0)`.
   - A timer fires at `eta_seconds - pre_buffer` seconds from now, starts Seestar recording, stops after `pre_buffer + transit_duration + post_buffer`.
   - Duplicate scheduling (same `id + ETA`) is suppressed.

This runs inside the Flask request thread and does not block the response.

---

## 13. Live Transit Detection: TransitDetector

`src/transit_detector.py::TransitDetector` is an independent computer-vision pipeline that does not depend on flight data during detection. It operates on the live RTSP video stream from the Seestar.

### Video acquisition
Two concurrent ffmpeg subprocesses read the RTSP stream:

| Consumer | Resolution | FPS | Format | Purpose |
|----------|-----------|-----|--------|---------|
| Low-res | 90 × 160 px | 15 | Raw RGB24 to pipe | Real-time CV analysis |
| Hi-res | Full 1080p | ~30 | MJPEG to pipe | Frame buffer for recording |

The low-res stream is chosen to keep the per-frame CV budget well under the 1/15 s frame interval. The 90×160 portrait crop matches the Seestar's RTSP orientation.

### Solar disc detection
Every `DISK_DETECT_INTERVAL = 30` frames (~2 s), the detector re-fits the disc:
1. **Hough circles** on a blurred grayscale frame (`HoughCircles` with a generous radius range).
2. **Contour fallback:** if Hough fails, threshold on the brightest pixels and fit the largest contour's bounding circle.

The disc radius, centre `(cx, cy)`, and a margin `DISK_MARGIN_PCT` (default 25%) define three regions:
- **Inner mask:** disc interior, shrunk by the margin — this is where a transiting aircraft appears dark.
- **Limb ring:** the outer annulus of the disc — excluded from detection signals (limb darkening and edge artefacts).
- **Outer region:** outside the disc entirely — used for the centre-ratio denominator.

**No disc = no detection.** If the disc cannot be found, the detector emits no events and waits.

### Detection signals

**Signal A — frame-to-frame difference:**
```
A = mean(|frame[t] - frame[t-1]|) on inner_mask, after subtracting per-frame mean
```
Sensitive to fast-moving dark objects (aircraft crossing at ~10–50 px/frame at low-res).

**Signal B — reference frame difference:**
```
B = mean(|frame[t] - ref_frame|) on inner_mask, after subtracting per-frame mean
ref_frame = EMA(frame, alpha=0.02)  [frozen for REF_FREEZE_FRAMES=75 after detection]
```
The EMA reference slowly adapts to atmospheric shimmer and granule evolution but cannot track an aircraft crossing in 1–3 frames. The freeze prevents the reference from being "polluted" by a transit event while it is occurring.

**Centre ratio:**
```
centre_ratio = inner_mean / outer_mean
```
where inner/outer means are computed on the abs-diff image. A transiting aircraft darkens only the inner disc region; atmospheric noise darkens the whole field uniformly. `centre_ratio >= CENTRE_EDGE_RATIO_MIN` (default 2.5) is required to fire.

### Adaptive detection threshold
The threshold for each signal is computed from the rolling 20-second history of that signal:
```
threshold = median + max(3 * MAD, 0.5 * median)
```
where MAD is the median absolute deviation. This adapts to varying seeing conditions.

If the recent 3-second median signal is significantly higher than the 60-second background median (indicating high turbulence or cloud edge), a `noise_factor` multiplier is applied to raise the threshold. This reduces false positives during poor seeing.

Both thresholds are multiplied by `sensitivity_scale` (user-adjustable via UI slider, default 1.0).

### Event firing gates

All of the following must be true simultaneously:

1. Disc is detected.
2. `centre_ratio >= centre_ratio_min` (default 2.5).
3. Signal A > threshold A.
4. Signal B > threshold B.
5. **Consecutive frames:** conditions 1–4 true for `CONSEC_FRAMES_REQUIRED` (env, default 7) consecutive frames.
6. **Track gate:** centroid displacement vectors across consecutive frames must agree in direction (dot-product agreement ≥ `track_min_agree_frac`, default 0.5) and have minimum magnitude `track_min_mag`. This rejects noise spikes and clouds (which have no coherent motion direction).
7. **Cooldown:** no detection in the last `DETECTION_COOLDOWN` (env, default 30) seconds.

### On detection

1. Save diagnostic JPEG frames (pre/post buffer).
2. If `record_on_detect=True`, extract an MP4 from the hi-res MJPEG circular buffer (`pre_buffer + post_buffer` seconds).
3. Run `ffmpeg -vf scdet` on the recording for a contrast-change thumbnail.
4. **Identify the aircraft** (`_enrich_event`): query `fetch_opensky_positions()` for the current bbox; for each aircraft, compute `angular_separation(aircraft_altaz, sun/moon_altaz)` and match the nearest aircraft within 10°. The matched callsign is attached to the `DetectionEvent`.

### Post-detection stabilisation (optional)
`_stabilize_frames()` uses OpenCV phase correlation to align consecutive frames to a common reference, correcting for atmospheric tip-tilt before computing signals. Controlled by env `ENABLE_STABILIZATION`.

---

## 14. Post-Capture Analysis: TransitAnalyzer

`src/transit_analyzer.py::analyze_video()` processes a saved MP4 file offline to confirm and characterise a detected transit. It is independent of the live pipeline.

**Solar algorithm:**
1. Extract a median reference frame from the first N frames (default 90).
2. Phase-correlation stabilise each frame to the reference.
3. Compute per-frame `absdiff(frame, gaussian_blur(reference))`.
4. Morphological open then close to remove salt-and-pepper noise.
5. Find connected components (blobs) in each difference frame.
6. Filter static blobs (those whose centroids cluster spatially across frames — sunspot-like artifacts).
7. Apply transit coherence filter: track blobs across frames by nearest-neighbour matching; require minimum total travel, minimum speed (px/s), linear trajectory (R² ≥ 0.25), and aspect-ratio guard (no elongated clouds).

**Lunar algorithm:**
Same pipeline but uses frame-to-frame diffs (not ref diffs) and slightly different coherence thresholds.

**Output:**
- `AnalysisResult` dataclass with list of `BlobDetection` objects.
- Composite image: alpha-blend of all per-frame silhouettes showing the transit track.
- Annotated video with detection highlights.
- JSON sidecar with timestamps, confidence scores, and blob trajectories.

---

## 15. Solar Timelapse (Parallel Imaging Pipeline)

`src/solar_timelapse.py::SolarTimelapse` runs independently of both the prediction and detection pipelines. It captures one JPEG frame from the Seestar's RTSP stream at a configurable interval (default 120 seconds) and assembles a daily timelapse video.

**Frame stabilisation:** Phase correlation between consecutive frames with disc-centre anchor. Corrects slow drift in solar tracking. Max shift 25 px (default); EMA smoothing factor 0.85.

**Sunspot annotation:** CLAHE contrast enhancement + adaptive thresholding on the disc interior; small dark regions darker than the local mean by >10 grey levels are annotated as candidate sunspots.

**Pause/resume:** The timelapse pauses during transit recording (externally called by `TransitRecorder`) and resumes after.

**Storage:** Frames under `static/captures/YYYY/MM/timelapse_YYYYMMDD/frame_NNNNN.jpg`; assembled MP4 at `timelapse_YYYYMMDD.mp4`.

---

## 16. Notifications

`src/telegram_notify.py::send_telegram_notification()` sends an HTML Telegram message for MEDIUM and HIGH transits. Called from the `/flights` handler background thread if `send-notification=true` and the flight is not below the observer's minimum altitude threshold.

Message format: aircraft callsign, target, ETA (minutes), angular separation (∑△ = `alt_diff + az_diff`), origin/destination if available. Maximum 5 flights per message.

---

## 17. Known Limitations and Failure Modes

### Prediction accuracy

**1. Constant-velocity model**  
The 15-minute forward prediction assumes fixed speed and heading. Aircraft in terminal manoeuvres, holding patterns, or making turns will diverge. Error accumulates linearly: at 900 km/h and a 5°/min turn rate, the position error at 15 minutes is ~35 km, corresponding to ~4° angular error at 200 km range. In practice, transits are only actionable when `time < 5 min`, where the error is manageable.

**2. OpenSky position latency**  
OpenSky has ~10-second data latency plus up to 30-second allowed staleness before the gate fires. Maximum total position uncertainty before discarding: 40 seconds × 250 m/s = 10 km.

**3. Constant altitude assumption**  
Vertical rate is not used to project altitude. A climbing aircraft at 5 m/s travels ~4.5 km vertically in 15 minutes — potentially changing its angular elevation by ~1.3° at 200 km range.

**4. Early-exit false negatives**  
The `diff_combined` early-exit aborts after 3 minutes of increasing combined differential. For aircraft on a near-grazing trajectory this may terminate the scan before the closest approach is found. This is a known trade-off between computation and completeness.

**5. Bbox coverage gap**  
For very low target altitudes (< 10°) the transit corridor can extend > 500 km from the observer. The 500 km cap on `ground_dist_km` may exclude aircraft that are geometrically eligible.

### Detection accuracy

**6. Disc detection failure**  
If the Seestar is pointing slightly off the Sun (e.g., after a manual slew without re-engaging solar tracking), the disc detection fails and the detector emits nothing. No alerting is in place for this condition.

**7. Consecutive-frame gate vs transit duration**  
A typical solar transit lasts 7–15 low-res frames (0.5–1 s at 15 fps). Requiring 7 consecutive frames is appropriate for most fast jets but may miss slow-moving aircraft or distant ones crossing at a shallow angle.

**8. RTSP stream load**  
Running TransitDetector (2 ffmpeg consumers) simultaneously with the solar timelapse and UI preview adds 3+ concurrent RTSP readers. This has been observed to interfere with the Seestar's Centre Target (solar/lunar disc-centering) function, presumably by saturating the device's Wi-Fi radio and encoding pipeline.

**9. OpenSky enrichment lag**  
Aircraft identification at detection time queries OpenSky, which has up to 30 seconds of latency. The aircraft that caused the transit may already have left the detection bbox, leading to a failed or incorrect identification.

**10. No prediction ↔ detection cross-check**  
There is no automated comparison of "which aircraft was predicted HIGH at the time of detection" vs "which aircraft was nearest to the disc at detection time." This is a manual post-event task.

---

## 18. Data Flow Diagrams

### A. Prediction pipeline (web app, once per refresh cycle)

```
OpenSky Network ──────────────────────────────────────────────┐
  fetch_opensky_positions()                                    │
  bbox: transit_corridor_bbox()                                │
  cache: 60s TTL                                               │
  staleness gate: 30s                                          ↓
  ground filter ──── flight_data[] ─── 180-step trajectory scan per aircraft
                                             │ predict_position() × 180
                                             │ geographic_to_altaz() × 180
                                             │ angular_separation() per step
                                             │ min(sep) → PossibilityLevel
                                             ↓
                              {id, sep, time, level, ...} per aircraft
                                             │
                   ┌─────────────────────────┴────────────────────────┐
                   │                                                   │
             /flights response                               TransitMonitor
          (adaptive polling)                           (30s loop, 0–5 min filter)
                   │                                                   │
          ┌────────┴────────┐                              ┌───────────┴────────┐
          │                 │                              │                    │
       Map UI        TransitRecorder                  CSV log            Telegram alert
    (ang. sep,    (HIGH only: schedule             (MEDIUM+HIGH)       (MEDIUM+HIGH)
     table,        recording ±10s buffer)
     countdown)
```

### B. Detection pipeline (continuous, on RTSP stream)

```
Seestar RTSP ─── ffmpeg (low-res 90×160) ──── frame buffer (20s)
             └── ffmpeg (hi-res MJPEG)   ──── circular buffer (pre+post)
                                                      │
                                          every 2s: disc detection
                                          (Hough circles + contour fallback)
                                                      │
                                          per frame (15/s):
                                            Signal A (frame diff)
                                            Signal B (ref diff, EMA)
                                            Centre ratio
                                            Adaptive threshold
                                            Track gate
                                            Consecutive gate (7 frames)
                                                      │
                                          ON DETECTION:
                                            Save JPEGs
                                            Extract MP4 from hi-res buffer
                                            Query OpenSky → identify aircraft
                                            DetectionEvent → UI + log
```

### C. Soft refresh cycle (client-side, every 15s)

```
Browser (every 15s):
  dead-reckon positions (speed × elapsed)
  decrement ETA
  if is_possible_transit=1 exists:
    POST /transits/recalculate
      → check_transit() × N  (no OpenSky call)
      → return updated levels/ETAs
  update table + map
```

---

## Appendix: Key Constants Reference

| Constant | Location | Value | Meaning |
|----------|----------|-------|---------|
| `TOP_MINUTE` | `constants.py` | 15 | Prediction window (minutes) |
| `INTERVAL_IN_SECS` | `constants.py` | 5 | Time step in prediction loop |
| `HIGH` threshold | `transit.py` | ≤ 2.0° | Angular separation for HIGH |
| `MEDIUM` threshold | `transit.py` | ≤ 4.0° | Angular separation for MEDIUM |
| `LOW` threshold | `transit.py` | ≤ 12.0° | Angular separation for LOW |
| `MAX_POSITION_AGE` | `opensky.py` | 30 s | Max OpenSky position age before discard |
| `CACHE_TTL` | `opensky.py` | 60 s | OpenSky bbox cache lifetime |
| `BACKOFF_429` | `opensky.py` | 300 s | OpenSky rate-limit backoff |
| `ANALYSIS_WIDTH/HEIGHT` | `transit_detector.py` | 90 × 160 | Low-res frame size |
| `ANALYSIS_FPS` | `transit_detector.py` | 15 | Detection frame rate |
| `CONSEC_FRAMES_REQUIRED` | `transit_detector.py` | 7 (env) | Consecutive frames to fire |
| `CENTRE_EDGE_RATIO_MIN` | `transit_detector.py` | 2.5 (env) | Disc centre vs edge signal ratio |
| `DISK_MARGIN_PCT` | `transit_detector.py` | 25% (env) | Disc margin excluded from inner mask |
| `EMA_ALPHA` | `transit_detector.py` | 0.02 | Reference frame update rate |
| `DETECTION_COOLDOWN` | `transit_detector.py` | 30 s (env) | Minimum time between events |
| `PRE_BUFFER_SECONDS` | `transit_detector.py` | 5 s (env) | Video pre-event buffer |
| `POST_BUFFER_SECONDS` | `transit_detector.py` | 5 s (env) | Video post-event buffer |
| Monitor `pre_buffer` | `app.py` | 10 s | TransitRecorder pre-buffer (web) |
| Monitor `post_buffer` | `app.py` | 10 s | TransitRecorder post-buffer (web) |
| `FA_ENRICHMENT_TTL` | `transit.py` | 7200 s | FA metadata cache lifetime |
| `FA_ENRICHMENT_429_BACKOFF` | `transit.py` | 300 s | FA rate-limit backoff |
| `EARTH_RADIUS` | `constants.py` | 6371 km | Used in great-circle prediction |
