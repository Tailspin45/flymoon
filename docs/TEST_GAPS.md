# Test Suite Gap Analysis

**Branch:** `v0.2.0`
**Date:** 2026-04-14
**Status:** Reality-check of `tests/` against the as-built system described in [AS_BUILT_REFERENCE.md](AS_BUILT_REFERENCE.md).
**Companion to:** [OPERATIONAL_RISK_AUDIT.md](OPERATIONAL_RISK_AUDIT.md) — see findings 4 and 5.

---

## 1. Headline

The repository contains **41 files under `tests/`** (124 test functions, roughly half of them diagnostic scripts rather than unit tests). Despite that surface area, **four of the most consequential modules added since v0.1 have zero test coverage**:

| Module | LOC | Test coverage | Risk class |
|---|---:|---|---|
| `src/imm_kalman.py` | ~515 | **0 references in tests/** | Critical — drives sep_1sigma gating |
| `src/transit_detector.py` wavelet path (`_wavelet_detrend`, sym4 L3) | embedded | **0 unit tests** | Critical — shapes SNR input |
| `src/transit_detector.py` matched-filter (`_MF_TEMPLATES`, graduated thresholds) | embedded | **0 unit tests** | Critical — final gate on recording |
| `src/transit_classifier.py` (ONNX `TransitCNN` advisory) | 189 | **0 references in tests/** | High — advisory but feeds confidence logit |
| `src/flight_sources.py` multi-source ADS-B with `_SourceBackoff` | ~400 | **0 references in tests/** | High — silent failure mode |

A bare `grep -rl` across the `tests/` tree for `imm_kalman|wavelet|matched_filter|transit_classifier|flight_sources` returns **no files**. Every new capability shipped since March 2025 is guarded only by integration-level smoke and live RTSP replays.

---

## 2. What the existing tests *do* cover

### 2.1 Unit tests (pytest-style, fast)

| File | Subject | Depth |
|---|---|---|
| [test_astro.py](../tests/test_astro.py) | `CelestialObject` Skyfield wrapper | Solid. Sun/Moon alt-az at known epoch. |
| [test_classification_logic.py](../tests/test_classification_logic.py) | `get_possibility_level()` thresholds | Solid. Boundary cases for 2°/4°/12°. |
| [test_parse_flight_data.py](../tests/test_parse_flight_data.py) | `parse_flight_data()` FlightAware shape | Shallow — one happy path, no schema drift tests. |
| [test_position.py](../tests/test_position.py) | Linear extrapolation in `position.py` | Shallow — tests unit conversions but not 15-minute horizon error. |
| [test_geographic_to_altaz.py](../tests/test_geographic_to_altaz.py) | Observer-frame transform | Solid. |
| [test_fa_cache.py](../tests/test_fa_cache.py) | Flight TTL cache | Shallow — no eviction race tests. |
| [test_flask_routes.py](../tests/test_flask_routes.py) | HTTP route wiring | Smoke only. Does not exercise telescope routes. |
| [test_seestar_altaz_roundtrip.py](../tests/test_seestar_altaz_roundtrip.py) | Alt-Az encode/decode for JSON-RPC | Solid. |

### 2.2 Hardware / socket diagnostics

`test_raw_socket.py` through `test_raw_socket4.py`, `test_alpaca_discovery.py`, `test_alpaca_motor.py`, `test_auto_detect_arming.py` — these are ad-hoc harnesses used during Seestar debugging. They require a live telescope and are not part of any automated run. Useful as kept-around repro scripts; not a test suite.

### 2.3 Phased diagnostic scripts (`diag_phase*.py`)

Twenty files named `diag_phaseN_*.py` implement the original on-hardware validation ladder from the V0.1 position paper. They are numbered by subsystem:

- `phase0_*` — RTSP connect, Seestar TCP session
- `phase1_*` — RTSP stability soak, monitor loop
- `phase2_*` — detection validation, false-positive harness, live detector test
- `phase3_*` — bbox coverage, OpenSky freshness, prediction validation
- `phase4_*` — GoTo, mode transitions, nudge directions, position feedback, time sync
- `phase5_*` — failure injection, soak, synthetic transit

Only `phase2_false_positive_test.py` and `phase5_synthetic_transit.py` run without hardware. None of them exercise the IMM Kalman, wavelet, matched-filter, or CNN paths directly — they predate those subsystems and were never updated.

### 2.4 Higher-level integration

- [test_integration.py](../tests/test_integration.py) — end-to-end shape check; uses mocks.
- [test_transit_detection.py](../tests/test_transit_detection.py) — calls `transit_detector.analyze_frame()` against stubbed frames. Exercises the EMA path; does **not** exercise wavelet or matched-filter (they sit behind the `prime_for_event` + buffer + template machinery).
- [test_live_detector_replay.py](../tests/test_live_detector_replay.py) — replays a recorded clip through the detector. Useful manual regression tool; not asserted.
- [test_detection_harness.py](../tests/test_detection_harness.py) — synthetic transit injector. Again, no wavelet/MF assertions.
- [test_transit_recorder.py](../tests/test_transit_recorder.py) — pre/post buffer timing against a mock writer.
- [transit_validator.py](../tests/transit_validator.py) — CLI grader for historical recordings.

### 2.5 Aggregate numbers

- Test files: 41 (16 `test_*.py` + 20 `diag_phase*.py` + 5 helpers)
- Test functions: 124 (per `grep -c '^def test_'` across `tests/`)
- Tests that run without hardware or network: ~40
- Tests wired into any CI: **unknown / none visible in repo** — no `.github/workflows/*test*`, no `pytest.ini`, no `make test` target.

---

## 3. Critical gaps (must fix in v0.2.0)

### 3.1 IMM Kalman (`src/imm_kalman.py`) — **zero coverage**

Public surface:
- `update_filter(icao24, lat, lon, alt_m, vx, vy, vz, ts)` — main entry
- `advance_state(state, dt)` — forward propagation
- `extract_position(state)` → (lat, lon, alt_m)
- `state_position(state)` → tuple
- `angular_sigma(sigma_m, dist_m)` — meters → radians conversion used by `sep_1sigma`
- `cleanup_stale_filters(ttl)` — TTL janitor

Internals that need coverage:
- `_to_enu` / `_from_enu` — ENU ↔ geodetic roundtrip
- `_F_cv`, `_Q_cv_mat`, `_F_ca`, `_Q_ca_mat` — constant-velocity vs constant-acceleration transition and process-noise matrices
- `_kalman_update` — innovation / gain / covariance update
- `_imm_step` — two-mode mixing, likelihood blend

**Required tests:**

1. `test_imm_enu_roundtrip` — `_from_enu(_to_enu(lat,lon,alt)) ≈ (lat,lon,alt)` to 1e-6 over a grid of observer positions.
2. `test_imm_cv_straight_line` — feed 30 noisy samples of a straight trajectory at 250 m/s; assert terminal state within 50 m of truth and `angular_sigma` decreases monotonically.
3. `test_imm_ca_turn` — feed a 2g banked turn; assert CA mode weight rises above 0.5 within 10 updates.
4. `test_imm_stale_cleanup` — insert filter at t=0, call `cleanup_stale_filters(ttl=60)` at t=120, assert removal.
5. `test_angular_sigma_monotonic` — `angular_sigma(sigma_m, d)` increases with `sigma_m`, decreases with `d`, returns sane values for d→0 (finding 1 in the audit depends on this).
6. `test_update_filter_handles_missing_vz` — OpenSky often returns `None` for vertical rate; assert no exception and filter still advances.

Effort: ~1 day to write, half a day to fix anything that breaks.

### 3.2 Wavelet detrend (`_wavelet_detrend` in `transit_detector.py`)

Sym4 level-3 DWT. Removes slow atmospheric drift before the matched filter sees the signal. If this silently returns a bad array on edge lengths, every downstream detection is corrupted.

**Required tests:**

1. `test_wavelet_detrend_preserves_length` — for N in {30, 60, 120, 240, 480}, output length == input length.
2. `test_wavelet_detrend_removes_dc` — constant input → output near zero.
3. `test_wavelet_detrend_removes_ramp` — linear ramp → output near zero (DWT should kill low frequencies).
4. `test_wavelet_detrend_preserves_transient` — step of width 6 frames survives with ≥80% of its peak amplitude.
5. `test_wavelet_detrend_short_window` — input shorter than 2^3 samples: assert graceful fallback (pass-through or explicit error), never a crash or NaN leak.

Effort: half a day. These are pure numpy tests, no fixtures.

### 3.3 Matched-filter gate (`_MF_TEMPLATES`, `_mf_hit_required`)

Templates: `(6, 10, 15, 24, 40, 60, 90, 120)` frames at 30 fps. Graduated thresholds 70%/60%/50%/45%. This is the final gate on whether a recording happens.

**Required tests:**

1. `test_mf_templates_normalized` — each template has unit L2 norm (prerequisite for threshold comparability).
2. `test_mf_detects_matching_width` — inject a 24-frame triangular transit; assert the 24-frame template fires at ≥0.95 correlation and all others are below their threshold.
3. `test_mf_rejects_noise` — inject Gaussian noise of matching RMS; assert no template exceeds threshold over 10k trials at p < 1e-4.
4. `test_mf_rejects_edge_gradient` — simulate a sun-limb brightening (slow ramp over 60 frames); assert no template fires (the wavelet step should have killed this, so this is a joint test).
5. `test_mf_graduated_threshold_schedule` — verify `_mf_hit_required` returns 0.70 / 0.60 / 0.50 / 0.45 in the right priming windows relative to `eta_seconds`.

Effort: 1 day. Needs synthetic signal helpers; none currently exist.

### 3.4 ONNX CNN classifier (`src/transit_classifier.py`)

The CNN is advisory — it never blocks recording — but it feeds the confidence logit (0.2 × track_factor etc.). If it returns garbage, false-positive rates still climb and post-capture grading misfires.

**Required tests:**

1. `test_classifier_singleton` — two calls to the accessor return the same object (current implementation is module-level).
2. `test_classifier_input_shape` — asserts `(CLIP_T=15, CLIP_H=160, CLIP_W=90)` is what the underlying session accepts. Schema drift on the ONNX file would otherwise only surface in production.
3. `test_classifier_zscore_stable` — constant-frame input z-scores to a finite array (no `/0`), softmax sums to 1.
4. `test_classifier_synthetic_positive` — a synthetic clip with a clear disc-transiting blob returns positive-class logit > neutral.
5. `test_classifier_synthetic_negative` — pure noise returns negative-class logit > positive.

Effort: half a day if the ONNX file is checked in and loads clean; longer if we need to bundle a small fixture model.

### 3.5 Multi-source ADS-B (`src/flight_sources.py`, `_SourceBackoff`)

Five+ sources in parallel with exponential backoff (60s → 3600s). When all sources are in backoff, the predictor silently produces an empty bbox. No test currently simulates this.

**Required tests:**

1. `test_source_backoff_schedule` — record timestamps across 10 forced failures, assert 60/120/240/480/960/1920/3600/3600/3600/3600 (capped).
2. `test_source_backoff_reset_on_success` — one success after 5 failures resets to 60s base.
3. `test_all_sources_down_returns_empty` — monkeypatch every source to raise; assert `fetch_flights_in_bbox()` returns empty list **and** emits a WARN-level log the operator can alert on (finding 7).
4. `test_source_mix_dedup` — two sources return the same ICAO24 with slightly different timestamps; assert the newer record wins.
5. `test_source_timeout_enforced` — mock a source that sleeps 30s; assert the 12s wall-clock cap is honoured.

Effort: 1 day. Requires a small fake HTTP harness, which [test_fa_cache.py](../tests/test_fa_cache.py) has a pattern for.

---

## 4. Gaps worth fixing (should-have, v0.2.0 if time)

### 4.1 `sep_1sigma` regression test

Finding 1 in [OPERATIONAL_RISK_AUDIT.md](OPERATIONAL_RISK_AUDIT.md): the `sep_1sigma` block in `transit.py:580-603` is currently a syntactic mess (`try: pass`, discarded float, inline `__import__('math')`, bare-except → `None`). Before *any* fix ships, a test must pin what the correct output should be:

- `test_sep_1sigma_populated_when_filter_exists` — state + snapshot → numeric sep_1sigma in degrees, not `None`.
- `test_sep_1sigma_falls_back_when_no_filter` — no state → `None` (not a crash, not a silent 0).
- `test_sep_1sigma_units_degrees` — assert the returned number is in degrees (current code mixes radians in at least one branch).

### 4.2 `get_latest_snapshot` bbox matching

Finding 6: `opensky.py:get_latest_snapshot` returns `max(_cache.values(), key=ts)` without checking whether that cache entry covers the current event position. Tests:

- `test_snapshot_matches_requested_bbox` — two cached snapshots for disjoint bboxes; asking for one must not return the other.
- `test_snapshot_returns_none_on_miss` — no matching bbox → `None`, and the caller must treat `None` as "no data" not "empty aircraft list".

### 4.3 `prime_for_event` / recorder wiring

End-to-end test that a predicted transit at T+10s correctly:
1. Primes the detector with `eta_seconds=10`.
2. Drops the MF threshold to 0.45 during the prime window.
3. Opens the pre-buffer.
4. Closes the post-buffer after a synthetic hit.
5. Resets threshold to the baseline after the cooldown.

This exists conceptually in `test_transit_recorder.py` and `phase5_synthetic_transit.py` but neither asserts the threshold schedule.

### 4.4 Telescope route smoke

[test_flask_routes.py](../tests/test_flask_routes.py) does not hit any route in `telescope_routes.py`. At 5398 lines and 133 `except` blocks (finding 2), a smoke test that simply exercises GET/POST for every registered endpoint against `MockSeestarClient` would catch import-time regressions cheaply.

---

## 5. Nice-to-have (post v0.2.0)

- Property-based tests (hypothesis) for the spherical angular-separation function — it's used at a hot path and has never been fuzzed.
- Golden-recording replay regressions: pick 10 historical RTSP captures, assert detector verdict + sep_1sigma match a pinned JSON. Any refactor of the detector would have to update the golden file intentionally.
- `pytest.ini` + `make test` target that excludes the hardware-only diag scripts.
- A GitHub Actions workflow running the non-hardware subset on every push.

---

## 6. Suggested file layout

Add under `tests/`:

```
tests/unit/
    test_imm_kalman.py          # §3.1
    test_wavelet_detrend.py     # §3.2
    test_matched_filter.py      # §3.3
    test_transit_classifier.py  # §3.4
    test_flight_sources.py      # §3.5
    test_sep_1sigma.py          # §4.1
    test_opensky_snapshot.py    # §4.2
tests/fixtures/
    synthetic_transits.py       # shared helpers for §3.2-§3.4
    fake_source_backend.py      # shared helper for §3.5
```

Keep the existing `diag_phase*.py` scripts in place — they document the V0.1 hardware ladder and we want them retrievable even though they do not run in CI.

---

## 7. Effort rollup

| Item | Effort | Priority |
|---|---:|---|
| §3.1 IMM Kalman | 1.5 d | Must |
| §3.2 Wavelet detrend | 0.5 d | Must |
| §3.3 Matched filter | 1.0 d | Must |
| §3.4 ONNX CNN | 0.5 d | Must |
| §3.5 Flight sources + backoff | 1.0 d | Must |
| §4.1 sep_1sigma pinning | 0.25 d | Should |
| §4.2 OpenSky snapshot bbox | 0.25 d | Should |
| §4.3 Recorder wiring e2e | 0.5 d | Should |
| §4.4 Telescope route smoke | 0.5 d | Should |
| §5 Property / golden / CI | 1.0 d | Nice |

**Must-have total: ~4.5 days** of focused work. Rolls up into the [V0_2_0_ROADMAP.md](V0_2_0_ROADMAP.md) critical path.
