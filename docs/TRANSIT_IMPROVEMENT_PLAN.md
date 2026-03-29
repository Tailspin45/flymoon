# Flymoon Transit System: Gap Analysis and Improvement Plan
## A Research-Grounded Evaluation for Future Development

**Date:** March 2026  
**Based on:** `TRANSIT_PREDICTION_AND_DETECTION.md` (revision `dc5722d`)  
**Status:** Plan only — no code changes until approved.

---

## Table of Contents

1. [Summary of Current Methods](#1-summary-of-current-methods)
2. [Literature Review](#2-literature-review)
3. [Gap Analysis](#3-gap-analysis)
4. [Recommended Improvements](#4-recommended-improvements)
5. [Refactor Plan](#5-refactor-plan)
6. [Prioritized Todo List](#6-prioritized-todo-list)
7. [Sense Check](#7-sense-check)

---

## 1. Summary of Current Methods

### 1.1 Prediction Pipeline

| Component | Method | Key Parameters |
|-----------|--------|---------------|
| Flight data | OpenSky Network REST API, 60s cache | 30s staleness gate; up to 40s total position uncertainty |
| Bounding box | Geometric corridor (transit ground point + travel radius) | 500 km ground-dist cap; 600 km total radius cap |
| Position propagation | Constant-velocity great-circle dead-reckoning | 15-minute window, 5s time steps (180 samples/aircraft) |
| Altitude propagation | None — current altitude held constant | Vertical rate ignored |
| Angular separation | Spherical law of cosines on alt-az coords | Numerically stable; corrects near-zenith compression |
| Classification | Hard thresholds on minimum separation | HIGH ≤ 2°, MEDIUM ≤ 4°, LOW ≤ 12° |
| Early exit | Combined alt+az diff monotonicity heuristic | 3-minute non-improvement window |
| Uncertainty | None — purely deterministic point estimate | |

### 1.2 Detection Pipeline

| Component | Method | Key Parameters |
|-----------|--------|---------------|
| Disc finding | Hough circles + contour fallback | Every 30 frames (~2s); 3 mask regions |
| Signal A | Frame-to-frame pixel difference (inner mask) | After per-frame mean subtraction |
| Signal B | EMA reference subtraction (inner mask) | α=0.02; frozen 75 frames post-event |
| Spatial gate | Centre/outer ratio on abs-diff image | Requires ≥ 2.5× inner vs outer signal |
| Temporal gate | N consecutive frames above adaptive threshold | Default 7 frames (0.47s at 15 fps) |
| Motion gate | Centroid displacement dot-product agreement | Default 50% of consecutive frames |
| Threshold | Rolling median ± 3 MAD (20s window) | × noise_factor during turbulence |
| Post-detection | OpenSky query + angular proximity match | 10° gate; suffers from 30s OpenSky lag |

### 1.3 Integration

The prediction and detection pipelines are **almost entirely independent**. The only links are: (a) the prediction pipeline schedules a recording window when a HIGH event is imminent; (b) after a live detection, OpenSky is queried to identify the responsible aircraft. There is no real-time feedback between them.

---

## 2. Literature Review

### [L1] Gaussian Process Regression for Flight Trajectory Prediction

**Reference:** R. Graas, J. Sun, J.M. Hoekstra, *"Quantifying Accuracy and Uncertainty in Data-Driven Flight Trajectory Predictions with Gaussian Process Regression,"* 11th SESAR Innovation Days, 2021. (TU Delft Research Portal)

A two-stage GPR approach combines historical flight-type data with the observed trajectory of the specific flight. Applied to arrivals at Amsterdam Schiphol, the model produces full **predictive distributions** (not just point estimates), allowing uncertainty to shrink as the aircraft approaches. Flight-plan and meteorological information further reduce prediction error. Critically, this approach is evaluated on ADS-B data from OpenSky — the same source Flymoon uses — making it directly applicable.

**Relevance to Flymoon:** Replacing the constant-velocity model with a trained GPR (or lighter-weight Kalman variant) would yield: (a) angular uncertainty bounds alongside the separation estimate; (b) better accuracy for non-cruising aircraft; (c) the ability to discount alerts with high uncertainty.

---

### [L2] RNN-Enhanced IMM-Kalman Filter for ADS-B UAV Tracking

**Reference:** arXiv:2312.15721, *"UAV Trajectory Tracking via RNN-enhanced IMM-KF with ADS-B Data,"* December 2023.

An Interacting Multiple Model Kalman Filter (IMM-KF) augmented with a recurrent neural network that adaptively estimates the process and observation noise matrices from the ADS-B data stream. Compared to a conventional IMM-KF, the RNN-enhanced version achieved a **28.56% reduction in total RMSE** on maneuvering targets. The IMM framework maintains probability-weighted combinations of a constant-velocity (CV) and constant-acceleration (CA) motion model, seamlessly handling the switch between straight cruise and turns.

**Relevance to Flymoon:** The IMM approach is the practical engineering alternative to full GPR. It handles the exact failure mode in Limitation 1 (constant-velocity assumption breaking during turns or climb/descent) with moderate implementation effort. The ADS-B-specific noise adaptation addresses Limitation 2 (OpenSky data latency and quality variance).

---

### [L3] Adaptive IMM-Unscented Kalman Filter for Airborne Tracking

**Reference:** *"Adaptive Interacting Multiple Model Unscented Kalman Filter for Airborne Target Tracking,"* Aerospace 2023, 10, 698. (ADS: 2023Aeros..10..698A)

Extends the standard IMM to an Unscented Kalman Filter (UKF) for nonlinear dynamics, with transition probabilities between motion modes adapted via a distance function. Demonstrates robust tracking during sensor outages (analogous to OpenSky going into 429 backoff) and improved accuracy during combined maneuver + sensor-gap scenarios.

**Relevance to Flymoon:** The sensor-outage robustness is directly applicable to the 300-second OpenSky backoff period, during which Flymoon currently has no position updates for any aircraft. An IMM-UKF could continue propagating plausible trajectory envelopes through the outage.

---

### [L4] Wavelet-Based Detrending for Transit Detection in Noisy Signals

**Reference:** G. Celedon et al., *"Analysis of Kepler light curves using the wavelet transform to discriminate with machine learning the astrophysical nature of the eclipsing object,"* Highlights on Spanish Astrophysics XII, 2023. (ADS: 2023hsa..conf..392G)

Discrete Wavelet Transform (DWT) preprocessing using symlet-5 wavelets followed by LightGBM classification achieved **81% accuracy** in distinguishing true transits from false positives (binary systems, pulsating stars), outperforming non-wavelet pipelines by 5–6 percentage points. The DWT separates the slowly-varying background (analogous to atmospheric shimmer on the solar disc) from the impulsive transit signature.

**Relevance to Flymoon:** The rolling EMA reference (Signal B) is a crude first-order high-pass filter. A wavelet decomposition of the per-frame disc-averaged intensity time series would cleanly separate: (a) DC component (solar limb darkening); (b) low-frequency component (atmospheric seeing, clouds, granulation evolution); (c) high-frequency impulse (aircraft crossing at ~15–30 frames/transit). This would improve the signal-to-noise ratio of Signal B significantly, especially for slow-moving or partial disc crossings.

---

### [L5] Transit Comb Filter vs Box Least Squares for Correlated Noise

**Reference:** arXiv:2308.04282 (2023), comparing TCF+ARIMA detrending vs BLS algorithm.

The Transit Comb Filter (TCF), combined with ARIMA-based detrending of the residual time series, significantly outperforms traditional BLS when **autocorrelated noise** is present. BLS assumes white noise; BLS degrades when the noise has a power spectrum that correlates across adjacent samples — exactly the condition imposed by atmospheric turbulence and granulation jitter on a solar image sequence.

**Relevance to Flymoon:** While BLS is designed for periodic transit surveys, the core insight — that matched-filter approaches tuned to the known transit shape outperform simple threshold detectors in correlated noise — applies directly to Flymoon's Signal A/B detection. A matched filter or template correlator shaped to the expected aircraft silhouette duration (0.3–1.5 s) would improve sensitivity for partial or slow crossings.

---

### [L6] GPU-Accelerated Phase Folding + CNN for Transit Detection (GPFC)

**Reference:** arXiv:2312.02063 (2023), published MNRAS 2024. *"The GPU Phase Folding and Deep Learning Method for Detecting Exoplanet Transits."*

GPFC achieved **97% training accuracy** and detection rates superior to BLS at three orders of magnitude faster runtime on GPU. The CNN operates on phase-folded images of the light curve, classifying transit vs. non-transit with high recall on synthetic + real data.

**Relevance to Flymoon:** The architecture is not directly portable (it requires a periodic signal), but the core approach — a small CNN trained on known transit morphologies — is applicable to Flymoon's post-event classifier. A CNN trained on confirmed transit frame-sequences vs. false-positive frame-sequences (clouds, sunspot edge artefacts, cosmic rays) could replace the current hard-threshold consecutive-frame gate with a learned decision boundary, substantially reducing false positives without sacrificing recall.

---

### [L7] KBMOD: Moving Object Detection with CNN Stamp Filters in Difference Images

**Reference:** *"Sifting through the Static: Moving Object Detection in Difference Images,"* AJ 2021; KBMOD v1.0 applied to DECam data 2023.

KBMOD's "shift-and-stack" method detects moving objects by evaluating candidate linear trajectories across a sequence of difference images. A CNN stamp filter rejects artefacts that survive the linear-motion hypothesis but are not true moving objects. In practice this step removed ~70% of false positives while retaining >98% of true detections.

**Relevance to Flymoon:** TransitAnalyzer already performs a similar shift-and-stack via blob tracking + coherence filtering. Adding a lightweight CNN stamp classifier (trained on positive examples from confirmed transits and negatives from false detections) would strengthen the offline post-capture confirmation step and could be applied to the live stream as a second-stage gate.

---

### Additional Observational Context

**Transit Chaser** (transitchaser.com) is the closest publicly known competitor to Flymoon's predictive function. It uses **four simultaneous flight data sources** (ADSB-One, OpenSky, ADS-B Exchange, RadarBox) with search radii from 10–60 km and prediction windows from 30 s to 3 minutes. This multi-source approach directly addresses Flymoon's single-source dependency on OpenSky and the data latency problem.

---

## 3. Gap Analysis

### 3.1 Prediction Pipeline Gaps

| # | Gap | Current Approach | State of the Art | Impact |
|---|-----|-----------------|-----------------|--------|
| P1 | **No uncertainty quantification** | Point-estimate separation only | GPR / IMM gives full predictive distribution [L1, L2] | HIGH — Users cannot distinguish "reliably HIGH" from "barely HIGH" |
| P2 | **Constant-velocity model** | Fixed speed/heading for full 15-min window | IMM-KF blends CV + CA motion models; adapts to maneuvers [L2, L3] | HIGH — Maneuvering aircraft produce ~35 km error at 15 min |
| P3 | **Constant altitude** | Vertical rate ignored in projection | Monotonic GP emulator for climb [L1]; simple: project alt using vertical_rate × time | MEDIUM — ~1.3° error for climbing aircraft at 200 km range |
| P4 | **Early-exit false negatives** | Monotonicity heuristic on combined diff, not spherical sep | No standard equivalent; fix: use spherical sep as exit criterion | MEDIUM — Grazing-trajectory aircraft may be missed |
| P5 | **Single data source** | OpenSky only; 30s staleness gate; 100 req/day anon | Transit Chaser uses 4 simultaneous ADS-B feeds [competitor] | HIGH — OpenSky API 429 → 300s blackout; zero fallback |
| P6 | **No altitude ADS-B refinement** | Uses single reported geometric/baro altitude | Track history → altitude trend → Kalman smoother [L2] | LOW — Most cruise-phase aircraft are stable |
| P7 | **Bbox low-angle coverage gap** | 500 km TGP cap may exclude low-target aircraft | Increase cap or use angular error budget to warn | LOW — Only affects targets < 10° elevation |
| P8 | **No cross-check with detection** | Prediction and detection have no real-time link | Fuse: detection confirms/updates prediction; prediction primes detector | HIGH — Missed opportunity for reciprocal validation |

### 3.2 Detection Pipeline Gaps

| # | Gap | Current Approach | State of the Art | Impact |
|---|-----|-----------------|-----------------|--------|
| D1 | **Threshold-only classifier** | Adaptive median ± 3 MAD threshold | CNN / lightweight ML classifier trained on event morphology [L6, L7] | HIGH — Hard thresholds are brittle across conditions |
| D2 | **No disc-lost alerting** | Disc failure silently disables detection | Monitor disc fit quality; emit warning when disc absent > N frames | HIGH — Silent failures are the worst kind |
| D3 | **Consecutive-frame gate too strict for slow transits** | 7 consecutive frames required | Template / matched filter covering 0.3–2.0s durations [L5] | MEDIUM — Slow aircraft or partial crossings missed |
| D4 | **EMA reference insufficient for correlated noise** | First-order low-pass filter | Wavelet detrending separates shimmer from transit impulse [L4] | MEDIUM — False positives during atmospheric turbulence |
| D5 | **Multiple RTSP consumers cause telescope interference** | 2+ ffmpeg readers on Seestar | Single RTSP demux → multiple consumers in-process | HIGH — Currently observed to disrupt Seestar solar tracking |
| D6 | **OpenSky identification lag** | Query OpenSky at detection time (30s latency) | Pre-cache OpenSky data; match against prediction's pre-fetched list | MEDIUM — Aircraft may already be out of bbox |
| D7 | **No probabilistic confidence score** | Binary fire/no-fire gate | Multi-signal probability score → configurable confidence floor | LOW — Useful for post-event triage |
| D8 | **No prediction priming** | Detector runs at constant sensitivity all day | Raise sensitivity automatically during predicted HIGH window | MEDIUM — Reduces false positives outside event windows |

### 3.3 Integration Gaps

| # | Gap | Severity |
|---|-----|---------|
| I1 | **No prediction ↔ detection cross-check** | HIGH — Cannot automate confirmation of predicted events |
| I2 | **No event log with outcomes** | MEDIUM — "Did the predicted transit actually happen?" is unanswered |
| I3 | **Local ADS-B stub unimplemented** | MEDIUM — Could eliminate all OpenSky latency for users with RTL-SDR |
| I4 | **No multi-source data fusion** | HIGH — Single-source failure loses all position data |

---

## 4. Recommended Improvements

### 4.1 Prediction — IMM Kalman Filter (addresses P1, P2, P3)

Replace `predict_position()` with a two-model Interacting Multiple Model Kalman filter:

- **Model 1 (CV):** Constant-velocity on a WGS-84 surface — identical to current dead-reckoning but with process noise covariance.  
- **Model 2 (CA):** Constant-acceleration — handles turns and climb/descent.  
- **State vector:** `[lat, lon, alt, v_lat, v_lon, v_alt]` — incorporates vertical rate natively, solving P3 at no extra cost.  
- **Measurement update:** Each new OpenSky state vector is a noisy measurement; the filter smooths and extrapolates.  
- **Output:** Point estimate + 1σ covariance propagated forward → angular uncertainty ellipse → `possibility_level_lower` and `possibility_level_upper` bounds.  

The IMM mode-blending handles the cruising→turning transition that is Flymoon's Limitation 1. Literature evidence [L2] shows 28.56% RMSE reduction vs constant-velocity over ADS-B data. Implementation is self-contained in `src/position.py`; the API to `check_transit()` can remain unchanged.

**Effort:** Medium (2–3 days). Requires `filterpy` or a thin custom Kalman implementation. No external API changes.

---

### 4.2 Multi-Source ADS-B Data (addresses P5, I4)

Add a `DataSourceManager` that queries at least two independent sources in parallel and merges results by ICAO24 address, preferring the most recent position:

- **Source A:** OpenSky (current)  
- **Source B:** ADSB-One free tier (`https://opendata.adsb.one/api/0/flights/all`) or ADS-B Exchange  
- **Source C (optional):** Local RTL-SDR / dump1090 / tar1090 JSON feed — implements the existing `adsb-local` stub  

Merge strategy: for each ICAO24, take the position with the smallest `position_age_s`. Log disagreements > 1 nm as a data quality metric. This eliminates the single-source 300s blackout (Limitation P5) and provides partial coverage during OpenSky 429 backoff.

**Effort:** Low–Medium (1–2 days). REST fetch from second source is a near-copy of `src/opensky.py`. The stub in `transit.py` already has the hook.

---

### 4.3 RTSP Internal Mux (addresses D5)

Replace the current 3+ independent `ffmpeg` RTSP processes with a single RTSP reader that distributes frames to all consumers via an in-process queue:

```
Seestar RTSP
    │
  [Single ffmpeg reader thread]
    │
    ├── Detection queue (90×160, 15fps)
    ├── Hi-res buffer queue (1080p MJPEG)
    └── Timelapse tap (1 frame every N seconds)
```

One RTSP connection instead of three removes the Wi-Fi saturation that disrupts the Seestar's Centre Target / solar tracking firmware function (Limitation D5 / Limitation 8 in §17). This is the highest-ROI change for operational reliability.

**Effort:** Medium (2–3 days). The `ffmpeg` process management in `transit_detector.py` and `solar_timelapse.py` needs to be refactored to share a single process or use `ffmpeg`'s `-f tee` muxer.

---

### 4.4 Disc-Lost Watchdog (addresses D2)

Add a state machine inside `TransitDetector` that tracks disc detection outcomes:

- If disc is not found for `DISC_LOST_THRESHOLD` frames (env, default 60 = 4 s), emit a `disc_lost` status event.  
- The Flask `/telescope/status` endpoint and UI should surface this as a warning banner.  
- The TransitMonitor should be able to fire a Telegram alert: "⚠️ Disc lost — telescope may be mispointed."  

This converts the silent failure of Limitation D2 / Limitation 6 into an actionable alert.

**Effort:** Low (half a day). Purely additive to existing `TransitDetector.get_status()`.

---

### 4.5 Prediction-Detection Cross-Link (addresses D8, I1, I2)

At the moment of a predicted HIGH event, inject the predicted transit time into the `TransitDetector`:

- `detector.prime_for_event(eta_seconds, flight_id, angular_separation)` increases sensitivity (lower threshold, fewer consecutive frames required) for the duration of the event window.  
- On any detection during this window, record in the event log: `predicted_flight_id`, `detected_flight_id`, `match=True/False`.  
- Post-event: compare prediction vs detection records; surface confirmed/missed/spurious counts in the `/transit-log` view.

This closes the critical feedback loop (Limitation 10 / Gap I1) and provides a ground-truth dataset for tuning.

**Effort:** Medium (1–2 days). New `prime_for_event()` method + event log schema change.

---

### 4.6 Vertical Rate Altitude Propagation (addresses P3)

Trivially improve position prediction by incorporating the reported `vertical_rate_ms` into altitude propagation:

```python
future_alt_m = current_alt_m + vertical_rate_ms * minutes * 60
```

With a reasonable cap (e.g., don't project above FL450 or below FL050 for airborne aircraft). This is a 3-line change that eliminates the identified ~1.3° altitude error for climbing/descending aircraft.

**Effort:** Trivial (< 1 hour). Pure internal change to `check_transit()`.

---

### 4.7 Wavelet Detrending of Disc Photometry (addresses D4)

Replace the EMA reference (Signal B) with a wavelet-decomposed background estimate:

- Apply a 2-level discrete wavelet transform (e.g., Daubechies db4 or Symlet sym5) to the rolling per-frame disc intensity time series.  
- The approximation coefficients at level 2 represent the slow background (granulation + atmospheric shimmer + seeing).  
- Signal B becomes: `|current_frame - reconstructed_background|`, where background is the approximation-only reconstruction.

This is the technique validated in [L4] and [L5] for separating correlated low-frequency noise from the impulsive transit signal. It would reduce false positives during periods of rapid atmospheric variation (cloud edges, strong seeing) without increasing the false-negative rate.

**Effort:** Medium (1–2 days). Requires `PyWavelets` (already available in scientific Python). Operates on a 1D per-frame signal; no GPU required.

---

### 4.8 Lightweight CNN Transit Classifier (addresses D1, D3)

Train a small CNN classifier to replace or augment the consecutive-frame hard gate:

- **Input:** A 90×160 × N frame stack (N = 15, 1 second of video) centred on the predicted or detected event time.  
- **Classes:** `{transit, cloud_edge, sunspot_artefact, false_positive}`.  
- **Training data:** Confirmed transit recordings from the existing gallery; synthetic transits (dark ellipses swept across a synthesised solar disc); false positive clips from the existing capture archive.  
- **Architecture:** MobileNetV3-Small (fits comfortably in <20ms inference on a MacBook CPU) or a custom 3D-Conv net with ~50K parameters.

This addresses D1 (threshold brittleness) and D3 (7-frame gate is too strict for slow transits). A CNN operates on the full temporal pattern of a transit, not just per-frame pixel values, and can learn to distinguish the characteristic silhouette darkening from cloud-edge gradients.

**Effort:** High (1–2 weeks including data preparation). Significant effort but highest long-term detection quality improvement. Can be deployed as a second-stage confirmation gate on top of the existing pipeline without risk.

---

### 4.9 Pre-Cache OpenSky for Detection Enrichment (addresses D6)

Change the post-detection aircraft identification from a reactive OpenSky query to a proactive pre-cache:

- The TransitMonitor already runs `get_transits()` every 30 seconds. Attach the most recent full OpenSky state vector snapshot to the monitor's shared state.  
- When `TransitDetector._enrich_event()` fires, use the pre-cached list (age < 60 s) instead of issuing a new OpenSky request with 30s latency.  

This makes identification nearly instantaneous and works even during OpenSky 429 backoff (use last cached snapshot).

**Effort:** Low (half a day). Shared state object between `TransitMonitor` and `TransitDetector`.

---

### 4.10 Early-Exit Criterion Repair (addresses P4)

Change the early-exit heuristic to track `min_sep_seen` (spherical separation) instead of `min_diff_combined` (the sum of component differences). The current heuristic aborts based on a metric that is not monotonically correlated with angular separation for off-axis trajectories:

```python
# Current:
if diff_combined > min_diff_combined for 36 steps: break

# Fixed:
if sep > min_sep_seen + 0.5° for 36 steps: break
```

This is a trivial but correct fix for the false-negative risk on grazing trajectories (Limitation 4).

**Effort:** Trivial (< 30 minutes). One-line change in `check_transit()`, tested by existing synthetic transit test.

---

## 5. Refactor Plan

### Phase A — Quick Wins (1 week, no architectural changes)
*Goal: Fix known reliability holes without refactoring data flows.*

| Deliverable | Addresses | Effort |
|------------|-----------|--------|
| A1. Vertical rate altitude propagation | P3 | < 1 h |
| A2. Early-exit criterion fixed to use spherical sep | P4 | < 1 h |
| A3. Disc-lost watchdog + UI warning | D2 | 4–6 h |
| A4. Pre-cache OpenSky state for detection enrichment | D6 | 4–6 h |
| A5. Prediction-detection event log (schema only, no UI yet) | I2 | 2–4 h |

**Success criteria:** All existing Phase 5 tests still pass; disc-lost warning visible in UI during a test where Seestar is mispointed; event log CSV written on each detection.

---

### Phase B — Reliability & Data Quality (2 weeks)
*Goal: Eliminate the single-source ADS-B dependency and RTSP load problem.*

| Deliverable | Addresses | Effort |
|------------|-----------|--------|
| B1. Second ADS-B data source (ADSB-One or ADS-B Exchange) | P5, I4 | 1–2 d |
| B2. RTSP internal mux (single reader, multiple consumers) | D5 | 2–3 d |
| B3. Local ADS-B receiver support (implement adsb-local stub) | P5, I3 | 1–2 d |
| B4. Prediction-detection cross-link (prime_for_event) | D8, I1 | 1–2 d |

**Success criteria:** No RTSP interference with Seestar solar tracking during a 4-hour soak test with detector active; OpenSky 429 no longer causes a full 300s blackout; a detected transit during a predicted HIGH window generates a `match=True` log entry.

---

### Phase C — Prediction Model Improvement (2–3 weeks)
*Goal: Replace constant-velocity dead-reckoning with a stateful, uncertainty-aware predictor.*

| Deliverable | Addresses | Effort |
|------------|-----------|--------|
| C1. IMM Kalman filter (CV + CA models, 6D state) | P1, P2, P3 | 2–3 d |
| C2. Angular uncertainty bounds output | P1 | 1 d |
| C3. UI: display uncertainty band alongside separation | P1 | 1 d |
| C4. Bbox low-angle coverage fix (raise or warn on cap) | P7 | 0.5 d |

**Success criteria:** IMM RMSE ≤ current dead-reckoning RMSE on logged historical events; uncertainty bounds are non-trivially smaller at t=2 min than at t=10 min; no regression in existing tests.

---

### Phase D — Detection Signal Quality (2–3 weeks)
*Goal: Reduce false positives and improve sensitivity for slow transits without increasing RTSP load.*

| Deliverable | Addresses | Effort |
|------------|-----------|--------|
| D1. Wavelet detrending of Signal B | D4 | 1–2 d |
| D2. Duration-adaptive gate (replace 7-consecutive-frame with matched filter) | D3 | 1–2 d |
| D3. Probabilistic confidence score output | D7 | 0.5 d |
| D4. Detection event log UI in /transit-log | I2 | 1 d |

**Success criteria:** False-positive rate during a cloud-edge test < 1/hour; slow-transit synthetic test (aircraft at 500 km range) detects correctly; confidence score meaningfully correlates with event quality in post-review.

---

### Phase E — ML Classifier (4–6 weeks, optional)
*Goal: Replace hard gates with a trained model for best-in-class recall and precision.*

| Deliverable | Addresses | Effort |
|------------|-----------|--------|
| E1. Training data pipeline (extract clips from gallery + synthetic generator) | D1, D3 | 1 wk |
| E2. MobileNetV3-Small (or custom 3D-Conv) training | D1 | 1 wk |
| E3. Integration as second-stage gate in TransitDetector | D1 | 0.5 d |
| E4. Integration as TransitAnalyzer classifier | D1 | 0.5 d |
| E5. Validation: recall ≥ 90%, FPR ≤ 5% on held-out clips | | 1 wk |

**Success criteria:** ≥ 90% recall on confirmed transit clips; ≤ 5% false positive rate on 1-hour passive observation clip; inference time < 50 ms on host CPU.

---

## 6. Prioritized Todo List

Items are ordered by the product of *impact* (how many transits are saved/verified) × *feasibility* (inverse of effort and risk). Items within a phase are already ordered.

### Tier 1 — Do First (Phase A, trivial or critical safety)

- [ ] **T01** Fix early-exit criterion: use spherical sep monotonicity, not `diff_combined` — eliminates grazing-trajectory false negatives. *(30 min)*
- [ ] **T02** Add vertical rate to altitude projection in `check_transit()`. *(1 hour)*
- [ ] **T03** Add disc-lost watchdog to `TransitDetector`; surface warning in UI and via Telegram. *(4–6 hours)*
- [ ] **T04** Pre-cache OpenSky state snapshot in `TransitMonitor`; use it in `TransitDetector._enrich_event()`. *(4–6 hours)*
- [ ] **T05** Create event log schema: `transit_events.csv` with `[timestamp, predicted_flight_id, detected_flight_id, prediction_sep, detection_confirmed, notes]`. *(2–4 hours)*

### Tier 2 — High Impact (Phase B, architectural reliability)

- [ ] **T06** Implement RTSP internal mux: one `ffmpeg` reader, multiple Python consumers via `queue.Queue`. *(2–3 days)*
- [ ] **T07** Add ADSB-One or ADS-B Exchange as second data source in `src/opensky.py` (or new `src/adsb_one.py`); merge by ICAO24 + recency. *(1–2 days)*
- [ ] **T08** Implement `prime_for_event(eta_s, flight_id)` in `TransitDetector`; lower threshold and frame-gate during predicted HIGH window. *(1 day)*
- [ ] **T09** Wire prediction → detection cross-link: `TransitRecorder.schedule_transit_recording()` calls `detector.prime_for_event()` when scheduling. *(half a day)*
- [ ] **T10** Implement local ADS-B receiver support (`adsb-local` mode using dump1090/tar1090 JSON). *(1–2 days)*

### Tier 3 — Model Improvement (Phase C, prediction accuracy)

- [ ] **T11** Implement IMM Kalman filter with CV and CA motion models in `src/position.py`; maintain per-aircraft filter state across refresh cycles keyed by ICAO24. *(2–3 days)*
- [ ] **T12** Output angular uncertainty bounds from IMM; add `sep_1sigma` field to flight result dict. *(1 day)*
- [ ] **T13** UI: display separation as `2.1° ± 0.4°` when uncertainty is available. *(1 day)*
- [ ] **T14** Fix bbox cap: for targets < 10° elevation, log a warning and optionally use wider radius with a staleness-quality caveat. *(half a day)*

### Tier 4 — Signal Quality (Phase D, detection sensitivity)

- [ ] **T15** Add `PyWavelets` dependency; replace EMA Signal B with `pywt.dwt2`-based background separation on disc photometry time series. *(1–2 days)*
- [ ] **T16** Replace 7-consecutive-frame gate with a matched-filter correlator matched to a trapezoidal transit template of configurable duration (0.3–2.0 s). *(1–2 days)*
- [ ] **T17** Output probabilistic confidence score (sigmoid of SNR relative to threshold) alongside binary fire/no-fire. *(half a day)*
- [ ] **T18** Add detection event outcomes to `/transit-log` UI view: predicted vs detected, match status, confidence. *(1 day)*

### Tier 5 — Long-Term ML (Phase E, best-in-class accuracy)

- [ ] **T19** Write a training-data extractor that clips positive (transit) and negative (cloud, sunspot, false positive) sequences from the existing capture gallery. *(1 week)*
- [ ] **T20** Train MobileNetV3-Small (or custom 3D-Conv) on the clip dataset; validate at ≥ 90% recall, ≤ 5% FPR. *(1 week)*
- [ ] **T21** Integrate CNN as a second-stage gate in `TransitDetector` (fires only if existing gates pass + CNN agrees). *(half a day)*
- [ ] **T22** Integrate CNN into `TransitAnalyzer` offline confirmation step. *(half a day)*
- [ ] **T23** Automate false-positive/false-negative labelling from event log + user feedback UI. *(1 week)*

---

## 7. Sense Check

Each proposed change is tested here against three criteria: (a) it addresses a concrete identified gap; (b) it is supported by cited evidence or first-principles reasoning; (c) it is feasible within Flymoon's project constraints (Python, OpenCV, Skyfield, Flask, no dedicated GPU required except optionally for Phase E).

| Item | Gap(s) Addressed | Evidence Basis | Feasibility | Risk |
|------|-----------------|---------------|-------------|------|
| T01 Early-exit fix | P4 (grazing miss) | First principles: diff_combined is not monotone with sep | Trivial; testable with existing synthetic test | Negligible |
| T02 Vertical rate altitude | P3 | Standard ADS-B physics | Trivial; one formula | Negligible |
| T03 Disc-lost watchdog | D2 | Identified operational failure (§17.6) | Additive to existing status machinery | Negligible |
| T04 Pre-cache OpenSky | D6 | Identified latency gap (§17.9) | Shared state between two existing objects | Low |
| T05 Event log schema | I2 | Without logging we cannot improve | Schema design only; CSV append | Negligible |
| T06 RTSP mux | D5 | Observed hardware interference (§17.8) | Requires restructuring ffmpeg process management | Medium — regression risk in streaming |
| T07 Second ADS-B source | P5, I4 | Transit Chaser uses 4 sources; L2/L3 assume sensor fusion | New HTTP client; merge logic | Low |
| T08 prime_for_event | D8, I1 | First principles: known transit time = prior for detector | Additive API on TransitDetector | Low |
| T09 prediction→detection wire | I1 | Closes identified gap | One call from TransitRecorder → detector | Negligible |
| T10 Local ADS-B | P5, I3 | Eliminates all OpenSky latency | Existing stub; implement JSON parse | Low |
| T11 IMM Kalman filter | P1, P2, P3 | L2: 28.56% RMSE reduction; L3: handles maneuvers | `filterpy` library; self-contained | Medium — per-aircraft state tracking |
| T12/T13 Uncertainty bounds | P1 | L1: GPR gives predictive distribution | Derived from IMM covariance | Low |
| T14 Bbox low-angle fix | P7 | §17.5 documented | Warning log + env-configurable cap | Negligible |
| T15 Wavelet Signal B | D4 | L4: 5–6% improvement over non-wavelet approaches | `pywt` is pure Python | Low — frequency band selection needs tuning |
| T16 Matched filter gate | D3 | L5: matched filter outperforms threshold in correlated noise | Replaces consecutive counter with correlation | Medium — shape parameter tuning |
| T17 Confidence score | D7 | Standard signal processing | Sigmoid of SNR | Negligible |
| T18 Log UI | I2 | Post-event review is currently manual | Flask route + template | Low |
| T19–T23 CNN classifier | D1, D3 | L6: 97% accuracy; L7: 98% retention + 70% FP reduction | Requires capture archive; MobileNetV3 runs on CPU | High — training data curation effort |

### Constraints verification

- **No GPU required** for Phases A–D. Phase E (CNN) runs inference in <50 ms on a modern CPU using ONNX Runtime or PyTorch with `torch.inference_mode()`.
- **No new external APIs** for Phases A–C except one additional ADS-B REST endpoint (T07).
- **Backward compatibility:** All changes in Phases A–C leave the existing API contract (`/flights`, `/transits/recalculate`) unchanged externally.
- **Test coverage:** Existing `tests/diag_phase5_*.py` and `tests/test_integration.py` provide a regression baseline. T01 and T02 are directly verifiable by the synthetic transit test. T11 requires a new unit test for the Kalman state estimator.
- **Hardware dependency:** T06 (RTSP mux) is the highest-risk item because it touches the Seestar's Wi-Fi communication at a low level. It should be preceded by a 4-hour RTSP soak test after implementation (T06 success criterion).

### What this plan does NOT do

- It does not pursue plate-solving or star-alignment-based pointing verification (not applicable during solar/lunar observation — confirmed in project notes).
- It does not propose replacing the Skyfield/DE421 ephemeris, which is already producing correct celestial positions.
- It does not propose changing the Flask architecture or moving to async Python for the main web application — the scope is confined to prediction and detection modules.
- Phase E (CNN) is explicitly optional and sequenced last so that the system continues to produce value at every prior phase.

---

*End of plan. No code changes should be made until this document is reviewed and each phase is approved in sequence.*
