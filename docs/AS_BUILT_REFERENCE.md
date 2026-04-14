# Zipcatcher As-Built Reference

**Date:** 2026-04-14
**Branch:** `v0.2.0`
**Supersedes:** `TRANSIT_PREDICTION_AND_DETECTION.md` (rev `dc5722d`, March 2026) — now stale
**Status:** Canonical. Describes the system as-implemented, not aspirational.

This document exists because the March-2026 technical reference is ~8 months behind the code and, read as ground truth, leads new contributors to form incorrect mental models of every subsystem. Treat this file as the authoritative description of what runs in production on `v0.2.0`. When the code changes in a way that invalidates a section here, update this file in the same commit.

---

## 1. Architecture at 30,000 feet

Flask web app + static JS frontend, packaged as a PyInstaller/Electron wrapper for distribution. Not an Electron-native app. Two long-lived pipelines run in the server process:

1. **Prediction pipeline** — polls multi-source ADS-B, projects each aircraft forward 15 minutes, classifies the closest-approach angular separation against Sun/Moon ephemeris.
2. **Detection pipeline** — reads RTSP video from the telescope, runs per-frame CV analysis against the live Sun/Moon disc, fires events into a recording buffer and an enrichment/CNN/log chain.

The two pipelines were independent in the original design. They are now linked at the moment of a predicted HIGH event: `TransitRecorder.schedule_transit_recording()` calls `TransitDetector.prime_for_event()`, which lowers the detector's gates for the duration of the predicted window.

Core modules, with approximate responsibility and current size:

| Module | LOC | Role |
|---|---|---|
| `app.py` | 1698 | Flask app, route registration, `TransitRecorder` singleton, `/flights` handler |
| `src/telescope_routes.py` | 5398 | Telescope REST glue, RTSP lifecycle, detection tuning, motor state machine |
| `src/transit_detector.py` | 2447 | Live CV detection pipeline — the core of the detection side |
| `src/seestar_client.py` | 2333 | Seestar JSON-RPC client, heartbeat, TransitRecorder, recording plumbing |
| `src/transit_analyzer.py` | 1857 | Offline post-capture analysis (blob tracking, trajectory confirmation) |
| `src/alpaca_client.py` | 1826 | ALPACA/ASCOM client for Seestar-as-mount control |
| `src/solar_timelapse.py` | 1118 | Daily solar timelapse capture pipeline |
| `src/transit.py` | 1032 | `check_transit`, `get_transits`, IMM-integrated per-aircraft scan, FA enrichment cache |
| `src/flight_sources.py` | 681 | Multi-source ADS-B aggregator (5+ upstreams, per-source backoff) |
| `src/config_wizard.py` | 594 | First-run configuration wizard |
| `src/imm_kalman.py` | 515 | CV+CA Interacting Multiple Model Kalman filter, per-ICAO24 state |
| `src/eclipse_monitor.py` | 500 | Eclipse-aware scheduling |
| `src/position.py` | 290 | Coordinate transforms, bbox computation, constant-velocity fallback predictor |
| `src/opensky.py` | 279 | OpenSky REST client (one of the ADS-B upstreams) |
| `src/transit_monitor.py` | 263 | Background 30s poll loop for headless/notification mode |
| `src/opensky_client.py` | 234 | Secondary OpenSky API wrapper |
| `src/transit_classifier.py` | 189 | ONNX wrapper for the TransitCNN model (advisory) |
| `src/flight_data.py` | 188 | FlightAware AeroAPI client (enrichment only, not prediction) |
| `src/astro.py` | 161 | Skyfield wrapper for Sun/Moon position |
| `src/flight_cache.py` | 137 | In-memory per-callsign cache |
| `src/constants.py` | 119 | Enums, global constants, ffmpeg path resolution |
| `src/telegram_notify.py` | 102 | Telegram HTML alert |
| `src/site_context.py` | 78 | Observer location override plumbing |

Anything not in this list (`archive/`, `electron/`, `Flymoon.app/`) is legacy or distribution-only and must not be imported from live code.

---

## 2. Coordinate system and ephemeris

Unchanged from the March-2026 reference. Skyfield + JPL DE421 loaded once in `constants.py`. Observer is a `Topos` from WGS-84 lat/lon/elevation. Celestial object positions via `(body - earth).at(time).observe(body).apparent().altaz()` — apparent alt/az corrected for aberration and refraction. Azimuth is clockwise from true north.

Aircraft alt/az via `(aircraft_topos - observer_topos).at(time).altaz()`. Per-minute target positions are precomputed at `get_transits()` entry and indexed by integer minute — the Sun's motion over 5 s is negligible vs. other error sources.

---

## 3. Flight data acquisition

The `opensky.py`-only fetch described in the old reference is gone from the hot path. The live source is `src/flight_sources.py::get_multi_source_positions()`, which queries the following in parallel under a 12 s wall-clock timeout:

1. **OpenSky Network** — OAuth2 client credentials → legacy basic auth → anonymous (100/day).
2. **ADSB-One / airplanes.live** — free public API, no key.
3. **adsb.lol** — free public API, no key.
4. **adsb.fi opendata** — free, non-commercial terms, must cite source.
5. **ADS-B Exchange** — optional, requires `ADSBX_API_KEY`.
6. **Local dump1090 / tar1090** — optional, uses `ADSB_LOCAL_URL` pointing at a local `aircraft.json` endpoint.

Each source has its own `_SourceBackoff` with exponential timeout backoff (60 s → 3600 s cap) and a separate rate-limit override. Results are merged by callsign with most-recent-position-wins. `_record_http_call` tracks per-source usage for observability.

Stale-position gate is still **30 s** (`MAX_POSITION_AGE` in `opensky.py`) for the OpenSky path; other sources apply their own freshness semantics before the merge. Ground filter (`on_ground=True` discarded) is applied after the merge.

FlightAware (`flight_data.py`) is **never** used for prediction. It is used only for optional HIGH-event enrichment (`_enrich_from_fa`) with a 2 h per-callsign cache and a 300 s 429-backoff. FA call counts are persisted to `data/fa_counts.json` for cost tracking.

---

## 4. Bounding box computation

Unchanged from the old reference. `position.py::transit_corridor_bbox()` computes a physically meaningful corridor (transit ground point + 15-min travel radius, capped at 600 km). Priority: UI custom bbox → dynamic corridor → `.env` fallback rectangle.

---

## 5. Position prediction — IMM Kalman with constant-velocity fallback

This is the section most changed from the March-2026 reference.

`check_transit()` (at `transit.py:343`) now runs a two-stage prediction:

1. **IMM Kalman first.** If an `icao24` is present, `src.imm_kalman.update_filter(icao24, flight, obs_lat, obs_lon)` is called. This maintains a per-ICAO24 filter state with two motion models (constant velocity, constant acceleration) blended by mode probabilities. On every step of the 180-sample trajectory scan, `advance_state(state, 5 s)` propagates the state and `extract_position(state, obs_lat, obs_lon)` returns `(lat, lon, sigma_m)` — the 1σ position uncertainty at that time step.
2. **Constant-velocity fallback.** If the IMM initialisation raises (logged at DEBUG), the loop falls through to `position.predict_position()` — the original great-circle dead-reckoning on the reported speed/heading.

Altitude propagation: vertical rate is applied to the base altitude per minute and clamped to the `[300 m, 15000 m]` cruise envelope. Previously the altitude was held constant for the full 15 minutes.

Early exit: the loop breaks when the **spherical angular separation** (not the old `diff_combined` heuristic) has been non-decreasing for ~3 minutes, with a fall-back to `diff_combined` for below-horizon steps where `sep` is geometrically meaningless.

Uncertainty output: the minimum-separation `_step_sigma_m` is converted to an angular 1σ via `imm_kalman.angular_sigma(sigma_m, slant_distance_m)` and written to `response["sep_1sigma"]`. **See OPERATIONAL_RISK_AUDIT.md §1 for a known defect in this conversion block.**

Classification thresholds (`get_possibility_level` in `transit.py`) are unchanged:

| Level | Threshold |
|---|---|
| HIGH | ≤ 2.0° |
| MEDIUM | ≤ 4.0° |
| LOW | ≤ 12.0° |
| UNLIKELY | > 12.0° |

---

## 6. Detection pipeline — TransitDetector

Resolution and frame rate have been upgraded since the old reference:

| Parameter | Old reference | Current code |
|---|---|---|
| Analysis canvas | 160 × 90 | **180 × 320** (portrait) |
| Analysis FPS | 15 | **30** |
| Rolling window | 20 s (300 frames) | 20 s (~600 frames) |
| Background window | 60 s (900 frames) | 60 s (~1800 frames) |
| `DETECTION_COOLDOWN` | 30 s | **6 s** |
| Pre/post buffer | 5 s / 5 s | 3 s / 6 s |

Per-frame processing in `_process_frame`:

1. **Disc fit.** Hough circles every ~30 frames, contour fallback. `_build_disk_masks` returns `disk_bool` (inner), `limb_bool` (excluded ring), and a smooth float `disk_weight` for weighted diffs. If the disc is lost for `DISC_LOST_THRESHOLD=120` frames (~4 s at 30 fps), `_disc_lost_warning` is set, `_emit_status("disc_lost")` fires, and a Telegram alert is sent via `_send_disc_lost_alert`. Detection is disabled while the disc is missing.
2. **Signal A.** Mean-subtracted frame-to-frame absolute difference on the inner disc.
3. **Signal B.** Mean-subtracted EMA-reference diff, then pushed through a rolling wavelet detrender.
4. **Wavelet detrend.** `_wavelet_detrend` applies a level-3 sym4 DWT to the last ~128 samples of raw Signal B, zeros the approximation coefficients (slow cloud/background trend), reconstructs the detail-only signal, and returns `abs(detail[-1])`. Falls back to raw `abs(last)` if PyWavelets is unavailable or the buffer is too short (< 16 samples). At 30 fps, level-3 separates the ~0.13–2 s transit impulse from drift slower than 2 s.
5. **Centre ratio.** Inner-disc mean vs. limb-ring mean on the abs-diff image. A genuine transit produces `inner >> limb`; whole-field scintillation produces `inner ≈ limb`. The ratio must exceed `centre_ratio_min` (default 2.5).
6. **Adaptive threshold.** `median(history) + max(3 × MAD, 0.5 × median)` over the 20 s rolling window, multiplied by `sensitivity_scale`.
7. **Noise density guard.** If the 3 s recent median is > 2× the 60 s background median, `noise_factor = max(1, 0.5 × ratio)` is applied to both thresholds. Scene dominated by sunspot-like activity raises the bar.
8. **Gates (all must pass):**
   - disc detected
   - `centre_ratio ≥ centre_ratio_min`
   - Signal A and B above their adaptive thresholds
   - *either* the consecutive-frame gate (default 7 frames) *or* the **matched-filter gate** (see below)
   - centroid track-consistency gate: `track_min_agree_frac` (default 0.5) of the streak frames must agree on direction, with minimum per-frame displacement `track_min_mag`
   - cooldown since last event (`DETECTION_COOLDOWN=6 s`)
   - **limb-scintillation ratio check:** sustained `inner ≤ limb` spatial pattern for >N frames is rejected as "likely limb scintillation" with a WARN log line
9. **Matched-filter gate.** Templates `(6, 10, 15, 24, 40, 60, 90, 120)` frames at 30 fps cover 0.2 s → 4 s. Graduated hit-rate thresholds — 70% for n ≤ 15, 60% for n ≤ 40, 50% for n ≤ 60, 45% for n > 60 — so long slow transits through atmospheric seeing (which drop out of triggering for half the frames) still fire, while short templates stay noise-resistant.

On detection, `_handle_detection_fire` runs the post-event chain:

- **CNN advisory** (`src.transit_classifier.get_classifier`). 15-frame clip, ONNX-Runtime CPU inference. Returns `(is_transit, confidence)`. **Never blocks recording** — low CNN confidence applies a `-0.25` penalty to the confidence logit but the event still fires, still records, and still logs. This is deliberate: a missed transit is irrecoverable; a false positive is recoverable via review.
- **Confidence score.** Logit combining SNR (0.5 weight), centre-ratio (0.3), track agreement (0.2), minus a 1.2 bias; soft penalties for matched-filter-only firing (−0.15), track fail (−0.3), low CNN (−0.25), low centre ratio (−0.2); soft bonus for spike gate (+0.4). Sigmoid to [0,1]. Label: `strong` if raw SNR > 2, else `weak` if score ≥ 0.4 or spike, else `speculative`.
- **Diagnostic frames + signal trace snapshot** saved next to the event.
- **Recording extraction** from the hi-res circular buffer via `_start_detection_recording` → separate thread for ffmpeg encode.
- **Enrichment → log** (`_enrich_then_log_event`) uses the pre-cached ADS-B snapshot from `opensky.get_latest_snapshot()` instead of issuing a fresh fetch. If no snapshot is cached (detector running standalone), a fresh multi-source fetch is issued under a 10 s timeout.
- **Training clip extraction** (`_save_training_clip`) writes an `.npz` into `data/training/unlabeled/` labelled by confidence tier (`strong`/`weak`/`speculative`). This is how the CNN training set grows.

### 6.1 RTSP topology

Previously: two independent ffmpeg subprocesses (low-res analysis + hi-res MJPEG buffer) per detector instance, plus one per solar timelapse, plus the preview. Observed to saturate the Seestar Wi-Fi and disrupt solar tracking.

Now: **single unified reader** in `TransitDetector._reader_loop` feeds both the analysis canvas and the hi-res MJPEG buffer. See `transit_detector.py:978` comment: *"Hi-res reader removed: unified reader in _reader_loop feeds both."* The solar timelapse still runs its own ffmpeg but is paused during detection recording by `TransitRecorder`.

### 6.2 prime_for_event

`TransitDetector.prime_for_event(eta_seconds, flight_id, sep_deg=None)` is called from `TransitRecorder.schedule_transit_recording` (at `seestar_client.py:2188`) and — via the `/flights` route — from both `app.py:838` and `telescope_routes.py:5359`. It lowers the detector's consec-frame requirement and/or threshold scale for a window centred on `eta_seconds`, attaching `predicted_flight_id` so a post-detection match can be asserted against the log.

---

## 7. Post-capture analysis

`transit_analyzer.py::analyze_video()` is unchanged in architecture from the old reference — median reference, phase-correlation stabilisation, connected-component blob detection, transit-coherence filter with R² ≥ 0.25 linear-trajectory check, composite alpha-blended track image. Lunar path uses frame-to-frame diffs; solar path uses ref diffs.

The CNN classifier is *not* invoked on the offline path as of v0.2.0 opening — it is only used live, advisory. See `V0_2_0_ROADMAP.md` for whether that should change.

---

## 8. Soft refresh and monitor

`TransitMonitor` (30 s default poll, `opensky-only` legacy name but actually uses multi-source) writes daily CSV logs for MEDIUM+HIGH events and pushes Telegram alerts. The web UI soft-refresh (`POST /transits/recalculate`) still dead-reckons positions client-side between full refreshes and re-evaluates `check_transit` without a new upstream fetch.

---

## 9. Configuration surface

Parameters that can be changed at runtime via `PATCH /telescope/detect/settings` and the Detection Tuning sidebar:

| Name | Default | Range | Effect |
|---|---|---|---|
| `consec_frames_required` | 7 | 2–20 | Frames above threshold to fire the consec gate |
| `centre_ratio_min` | 2.5 | 0.5–6.0 | Inner/limb ratio required to pass the spatial gate |
| `disk_margin_pct` | 25% | 5–50% | Limb exclusion ring |
| `sensitivity_scale` | 1.0 | 0.2–3.0 | Threshold multiplier |
| `track_min_mag` | 2 px | 0–10 px | Min centroid displacement counted as directional |
| `track_min_agree_frac` | 0.6 | 0–1 | Fraction of streak frames needing direction agreement |

Environment variables (incomplete list — the full set is in `.env.mock`):

Required: `OBSERVER_LATITUDE`, `OBSERVER_LONGITUDE`, `OBSERVER_ELEVATION`, bbox corners for fallback. Optional: telescope (`SEESTAR_HOST`, `SEESTAR_PORT`, `ENABLE_SEESTAR`), Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), OpenSky credentials, ADS-B Exchange key, `ADSB_LOCAL_URL`, `DETECTION_COOLDOWN`, `DISC_LOST_THRESHOLD`, `DETECTION_PRE_BUFFER`, `DETECTION_POST_BUFFER`, `CNN_GATE_THRESHOLD`, `FLYMOON_AGENT_DEBUG_LOG`.

---

## 10. What the old reference still gets right

The coordinate-system sections, FA unit conventions, spherical-separation math, and the high-level data-flow shape are still accurate. Section numbers 1-4, 7-12, 14-16, and 18 of the March-2026 doc remain a useful narrative read — but any parameter table, algorithm description, or limitation list in that doc should be cross-checked against this file before being trusted.

## 11. What the old reference gets wrong

- Detection resolution and frame rate (16×90 @ 15 → 180×320 @ 30).
- Signal B description (missing wavelet detrend).
- Consecutive-frame gate description (missing matched-filter alternative and limb-scintillation ratio suppression).
- Claims no CNN classifier exists (it does — advisory).
- Claims no multi-source ADS-B (there are 5+).
- Claims no IMM Kalman (one is wired into `check_transit`).
- Claims no disc-lost alerting (there is).
- Claims no prediction↔detection cross-link (`prime_for_event` exists and is wired).
- `DETECTION_COOLDOWN` stated as 30 s; actually 6 s.
- Known limitation #8 (RTSP stream load) is obsolete — the unified reader fixed it.

Anyone using the March-2026 reference as the basis for planning will duplicate already-shipped work.
