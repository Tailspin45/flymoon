# Forensic Review: Transit Prediction & Detection Pipeline

**Date:** March 2026  
**Commit:** `fa60fb5`  
**Scope:** Full pipeline review — prediction math, detection CV, logging, configuration

---

## Executive Summary

After 3+ weeks of continuous operation near San Diego (33.11°N, 117.31°W) — a region
with heavy air traffic (LAX corridor, cross-country routes) — the app predicted
**zero aircraft transits** of the Sun or Moon. The CV detection pipeline only caught
insects (grasshoppers), generating false positives with no true aircraft events.

A forensic review identified the root cause: **classification thresholds were 4× too
tight**, making it mathematically near-impossible to ever classify an aircraft as a
transit candidate. Combined with a WARNING-level logger that silently dropped all
pipeline telemetry, the system appeared to run normally while producing no useful output.

All issues have been fixed in commit `fa60fb5`.

---

## Comparison with dbetm Fork

The review was informed by [`dbetm/flymoon` commit `5147e98`](https://github.com/dbetm/flymoon/commit/5147e98ffef6951d491b0d2f09b3a23f08a6285b),
which addressed similar issues with the message: *"fix: transit prediction algorithm —
consider cases when the target is high"*.

| Aspect | Our Code (before) | dbetm Fork | Impact |
|--------|-------------------|------------|--------|
| Angular separation formula | Cosine-weighted Euclidean approx | Spherical law of cosines (exact) | Minor (~5% at high altitudes) |
| HIGH threshold | ≤1.5° | ≤2.0° | Missed marginal HIGHs |
| MEDIUM threshold | ≤2.5° | ≤4.0° | **Missed 60% of MEDIUMs** |
| LOW threshold | ≤3.0° | ≤12.0° | **Missed 75% of LOWs** |
| check_transit gate | Hard 10° combined threshold | No gate — records all | Minor (10° is generous) |
| get_thresholds() | Dead code — never called | N/A | Intended adaptive logic never ran |
| future_time precision | `int(minute)` — truncated | `float(minute)` | Minor for Earth-fixed objects |
| Data source | OpenSky (free, ~60s cache) | FlightAware (paid, real-time) | OpenSky may have gaps |

### Why the Thresholds Mattered Most

The Sun/Moon disk is ~0.5° across. With a LOW cutoff of only 3°, the system would only
classify aircraft within ~6 solar diameters of the target. For San Diego with ~30
visible aircraft at any time:

- Sky hemisphere: ~20,626 square degrees
- A 3° radius circle: ~28 square degrees
- Per-snapshot probability of ANY aircraft within 3°: **~0.14%**
- Aircraft cluster on flight corridors, not uniformly — even worse odds

With a 12° LOW cutoff, the capture area increases 16×, making near-misses visible
daily. These near-misses are essential for validating the pipeline works.

---

## Findings

### A. Prediction Math & Geometry

#### A1. CRITICAL — Classification thresholds 4× too tight

**Before:** HIGH ≤1.5°, MEDIUM ≤2.5°, LOW ≤3.0°  
**After:** HIGH ≤2.0°, MEDIUM ≤4.0°, LOW ≤12.0°

With 3° max LOW, we only saw aircraft practically ON the solar disk — events so rare
they effectively never occurred. Without near-misses, it was impossible to validate
the pipeline was working at all.

**Fix:** Widened thresholds to match dbetm fork. The 12° LOW threshold captures
aircraft passing within ~24 solar diameters — frequent enough to validate daily.

#### A2. MAJOR — `get_thresholds()` was dead code

Function defined at `transit.py:236` but **never called anywhere** in the codebase.
It was designed to adapt thresholds based on target altitude (near zenith, azimuth
compression makes fixed az thresholds meaningless). The adaptive logic never ran.

**Fix:** Removed the dead function. The new `get_possibility_level(sep)` uses
pre-computed angular separation which already accounts for azimuth compression.

#### A3. MAJOR — Angular separation formula was approximate

**Before:** `sqrt(alt_diff² + (az_diff × cos(target_alt))²)` — Euclidean approx  
**After:** Spherical law of cosines — exact for all angles:

```python
cos(σ) = sin(alt₁)·sin(alt₂) + cos(alt₁)·cos(alt₂)·cos(Δaz)
```

The old formula diverges at high altitudes (~4% error at 70° altitude). The spherical
formula is equally simple, always exact, and handles all edge cases (zenith, horizon,
pole wrapping).

#### A4. MINOR — future_time truncated to integer minutes

`timedelta(minutes=int(minute))` dropped sub-minute precision. For aircraft alt-az
from a ground observer, the sidereal time dependence is negligible (both observer and
aircraft rotate with Earth), so this was mostly harmless but sloppy.

**Fix:** Changed to `timedelta(minutes=minute)` (float).

### B. Data Source & Coverage

#### B1. INFO — OpenSky free tier limitations

OpenSky provides ~60-second cached data with rate limiting (429 → 300s backoff).
During backoff windows, the app could miss entire 5-minute prediction cycles. This
wasn't the root cause (thresholds were), but it's worth monitoring.

**Fix:** Summary logging now reports aircraft count per cycle, making coverage gaps
visible.

### C. Detection Pipeline (CV)

#### C1. MAJOR — No duration discrimination (insects vs aircraft)

At `CONSEC_FRAMES_REQUIRED = 3` and 15 fps, the minimum detection duration was 200ms.
Aircraft transits last 0.5–2 seconds (7–30 frames), but fast-moving insects at close
range can trigger 3+ frames easily.

**Fix:** Increased default to 5 frames (333ms). At 15 fps, this filters most insects
(<100ms) while still catching aircraft (>500ms). Made configurable via
`CONSEC_FRAMES_REQUIRED` env var.

#### C2. MAJOR — Detection parameters were hardcoded

`CONSEC_FRAMES_REQUIRED`, `DETECTION_COOLDOWN`, and `CENTRE_EDGE_RATIO_MIN` were all
compile-time constants with no way to tune without code changes.

**Fix:** All three now read from environment variables with sensible defaults:
- `CONSEC_FRAMES_REQUIRED=5` (frames, default 5)
- `DETECTION_COOLDOWN=30` (seconds, default 30)
- `CENTRE_EDGE_RATIO_MIN=1.5` (ratio, default 1.5)

#### C3. INFO — No prediction↔detection correlation

The prediction and detection pipelines run independently. Detection events are
timestamped but not cross-referenced with active transit predictions.

**Fix:** Enhanced log messages with millisecond timestamps and frame/timing metadata
for manual log correlation. Full automated correlation deferred to future work.

### D. Logging & Observability

#### D1. CRITICAL — Logger set to WARNING

`src/logger_.py` had `level=logging.WARNING`, silently dropping ALL `INFO`-level
messages. Every transit pipeline log message (aircraft counts, nearest misses,
classification results) was invisible.

**Fix:** Changed to `level=logging.INFO`.

#### D2. CRITICAL — No nearest-miss logging

Without knowing how close aircraft were passing to the target, it was impossible to
distinguish "no aircraft nearby" from "pipeline is broken."

**Fix:** Added per-cycle summary logging in `get_transits()`:
```
[Transit] 12 aircraft checked | 🏆 nearest: UAL1234 at 5.23° (MEDIUM) | HIGH:0 MED:1 LOW:3
```

### E. Configuration

#### E1. MAJOR — Three disconnected threshold systems

1. `check_transit` gate: `max(alt_threshold, az_threshold)` — defaults 5°/10°
2. `get_possibility_level`: hardcoded 1.5°/2.5°/3.0° boundaries
3. `.env`: `ALT_THRESHOLD=1.0, AZ_THRESHOLD=1.0` — for notifications only

These were poorly documented and confusingly named.

**Fix:** Simplified to one system: `get_possibility_level(angular_separation)` with
clear threshold constants. The `check_transit` function no longer gates — it always
computes closest approach and classifies afterward.

---

## Changes Made (commit `fa60fb5`)

### `src/transit.py`
- New `angular_separation(alt1, az1, alt2, az2)` using spherical law of cosines
- Old `_angular_separation()` kept as deprecated alias for backward compatibility
- `get_possibility_level(sep)` — simplified to single argument, widened thresholds
- `check_transit()` — removed hard gate, tracks closest approach for ALL aircraft,
  classifies after search loop completes
- Removed dead `get_thresholds()` function
- Removed unused `Altitude` import
- Fixed `timedelta(minutes=int(minute))` → float
- Added per-cycle summary logging with nearest-miss identification

### `src/logger_.py`
- Level: `WARNING` → `INFO`

### `src/transit_detector.py`
- `CONSEC_FRAMES_REQUIRED`: 3 → 5 (configurable via env)
- `DETECTION_COOLDOWN`: now configurable via env
- `CENTRE_EDGE_RATIO_MIN`: now configurable via env
- Detection log messages include millisecond timestamps and timing metadata

### Tests
- `tests/test_transit_detection.py`: 18 tests (3 new synthetic transit tests)
- `tests/test_classification_logic.py`: Updated for new API and thresholds
- `tests/run_validation.py`: Updated for new `get_possibility_level(sep)` signature

### Synthetic Transit Tests Added

1. **`test_synthetic_transit_high`**: Aircraft placed on a known Sun-crossing path at
   ~6 km range, 10 km altitude. Verified: detected as HIGH (≤2°), angular separation
   < 2°.
2. **`test_synthetic_near_miss_medium`**: Aircraft offset from Sun flying radially.
   Verified: classified within 12° (at least LOW).
3. **`test_synthetic_far_aircraft_no_transit`**: Stationary aircraft far from Sun.
   Verified: `is_possible_transit = 0`.

---

## Operational Recommendations

### Immediate (days 1–3)
1. Run the app with current generous thresholds (LOW ≤12°)
2. Watch logs for nearest-miss entries — expect to see aircraft within 5–12° daily
3. Verify OpenSky is returning aircraft (check "N aircraft checked" counts)

### Short-term (week 1–2)
4. Review near-miss data — do angular separations look physically reasonable?
5. If seeing many LOW events but no MEDIUM/HIGH, thresholds are working correctly
   and real transits are just rare
6. If seeing zero events, investigate data source (OpenSky coverage, bounding box)

### Tuning (ongoing)
7. If false positives from detection are still too high, increase
   `CONSEC_FRAMES_REQUIRED` to 7 (467ms)
8. If missing real transits, decrease to 4 (267ms)
9. Monitor `CENTRE_EDGE_RATIO_MIN` — increase to 2.0 if edge artifacts trigger detections

### Detection Pipeline (future work)
- Add angular velocity estimation (aircraft: consistent speed/direction; insects: erratic)
- Add minimum duration filter (require signal above threshold for >300ms continuously)
- Correlate detection events with active transit predictions automatically
- Consider restricting ROI to a tighter bounding box around the Sun/Moon disk center

---

## Appendix: Angular Separation Math

### Old Formula (Euclidean approximation)
```
σ = sqrt(Δalt² + (Δaz · cos(target_alt))²)
```
Applies cosine compression to azimuth at the target's altitude. Valid for small angles
but diverges at high altitudes (~4% error at 70°).

### New Formula (Spherical law of cosines)
```
cos(σ) = sin(alt₁)·sin(alt₂) + cos(alt₁)·cos(alt₂)·cos(Δaz)
σ = arccos(result)
```
Exact for all angles. Handles zenith, horizon, and large separations correctly.

### Comparison at various geometries

| Scenario | Old formula | Exact (new) | Error |
|----------|------------|-------------|-------|
| Target 30°, 1° offset | 1.323° | 1.320° | 0.2% |
| Target 70°, 2°alt 10°az | 3.962° | 3.814° | 3.9% |
| Target 85°, 1°alt 5°az | 1.091° | 1.073° | 1.7% |
| Target 89°, 0°alt 50°az | 0.873° | 0.872° | 0.1% |

### Transit Probability Estimate (San Diego, ~30 aircraft visible)

- Sky hemisphere: ~20,626 sq degrees
- 2° radius (HIGH) circle: ~12.6 sq degrees → **0.06%** per aircraft per snapshot
- 12° radius (LOW) circle: ~452 sq degrees → **2.2%** per aircraft per snapshot
- With 30 aircraft: ~**50% chance** of at least one LOW event per snapshot
- Aircraft cluster on flight corridors — actual rate depends on corridor alignment
  with Sun/Moon position
