# Zipcatcher Operational Risk Audit

**Date:** 2026-04-14
**Branch:** `v0.2.0`
**Framing:** Operational reliability — what can silently fail, give the wrong answer, or degrade the system without alerting an operator. Not an academic redesign.

This audit takes the as-built state (see `AS_BUILT_REFERENCE.md`) as the starting point and asks a single question: *given what the code does today, where are the ways it can hurt us?*

Each finding has a severity, a file:line reference, why it matters, and a concrete fix direction. Severities follow the CLAUDE.md discipline: **Critical** only for "user-visible incorrect behaviour," **High** for "significant accuracy or reliability impact," **Medium** for "real but not blocking," **Low** for "cleanup."

---

## 1. Prediction uncertainty output is buggy — `sep_1sigma` is likely silently None in production

**Severity:** High
**Where:** [src/transit.py:580-603](../src/transit.py#L580-L603)

The `_min_sep_sigma_m` → `response["sep_1sigma"]` block has several code smells in a single 20-line function body:

- `try:` followed immediately by a no-op `pass`.
- A `float(response.get("angular_separation", 0))` call whose result is discarded on the next line.
- Inline `__import__("math")` at lines 593-594 for `sin` and `radians` — `math` is already imported at module top; the inline form suggests someone merged this block from a different context and never cleaned up.
- A bare `except Exception:` that assigns `response["sep_1sigma"] = None` with no logging at all.

The operational consequence: the moment anything in this block throws — including on a perfectly healthy aircraft — the uncertainty is silently set to None, the UI cannot render the `2.1° ± 0.4°` band the IMM filter was built to provide, and **no log line reveals the failure**. `imm_kalman.angular_sigma` being broken would look identical to it working correctly — both paths produce None for the UI.

**Fix direction:** remove the leading `try: pass`, move `from src.imm_kalman import angular_sigma` to module top, replace `__import__("math")` with the already-imported `math.sin`/`math.radians`, log the exception body at WARNING (not swallow it), and add a unit test that exercises both the healthy path and a filter-raises path. This is also an opportunity to extract a small helper (`_compute_sep_1sigma`) so the logic is testable in isolation.

**Verification test needed:** does the live `/flights` endpoint return non-None `sep_1sigma` for any aircraft right now? If not, the feature is dark.

---

## 2. Telescope routes layer is the operational-reliability concentration

**Severity:** High
**Where:** [src/telescope_routes.py](../src/telescope_routes.py) — 5398 lines, 133 `except` blocks

This single module is larger than `transit_detector.py` and `transit.py` combined. It contains the motor state machine, RTSP lifecycle, detection-settings endpoint, GoTo/nudge/slew serialisation, ALPACA fallback, discovery UDP broadcast, and the auto-record wiring. Every route is in one flat namespace with a shared `_ctrl_state` global, a shared `_ctrl_lock`, and a shared `_pre_nudge_tracking` global.

The `except Exception: pass` pattern at [telescope_routes.py:103-104](../src/telescope_routes.py#L103-L104) swallows all NDJSON agent-log write failures. That particular one is arguably defensible (debug-only telemetry). Less defensible:

- [line 753-754](../src/telescope_routes.py#L753-L754), [line 811-812](../src/telescope_routes.py#L811-L812): `except Exception: break` inside while-loops that poll telescope state during GoTo resume. On any hiccup the poll terminates without distinguishing "finished" from "crashed mid-poll," and the subsequent `time.sleep(0.5)` never runs.
- [line 947-948](../src/telescope_routes.py#L947-L948): `except Exception: pre_tracking = viewing_mode in ("sun","moon")` — the fallback guesses whether tracking was on based on mode, which is correct for most sessions but wrong whenever the user has manually paused tracking in solar mode.
- [line 1450-1451](../src/telescope_routes.py#L1450-L1451), [line 1503-1504](../src/telescope_routes.py#L1503-L1504), [line 1513-1514](../src/telescope_routes.py#L1513-L1514): silent `except Exception: pass` / `return None` patterns in the discovery and local-IP code. Acceptable in isolation but they accumulate.

**The structural risk.** 133 error paths in one module is a reliability ceiling. Any change to motor state machine logic has to reason about which of those exception handlers might swallow the new condition. A single missed `return` or `_ctrl_state = _CtrlState.IDLE` inside one of them leaves the state machine wedged, and there's no watchdog for "we've been in SLEWING for 60 seconds with no finalisation."

**Fix direction (v0.2.0 work):**

- **(a)** Introduce a `@route_handler` decorator that produces a standard JSON error body with traceback summary, logs at WARNING, and resets the motor state machine to IDLE on unhandled exceptions. Replace the per-route `except Exception as e: return handle_error(e)` boilerplate with it. This is a pure mechanical refactor, low risk, measurable effect: the `except` count should fall by ~40.
- **(b)** Split the module horizontally: `telescope_motor_routes.py` (GoTo/nudge/stop/state), `telescope_rtsp_routes.py` (streaming/detection/autofocus), `telescope_lifecycle_routes.py` (connect/disconnect/discover/settings). Each ~1500 lines. This is a larger refactor — do it in a separate PR after (a) is in and tests green.
- **(c)** Add a motor-state-machine watchdog: if `_ctrl_state` has been non-IDLE for > N seconds (N=60 is safe), log WARNING, force `_ctrl_state = _CtrlState.IDLE`, and release the lock. Prevents wedged state from persisting across a single hung request.

---

## 3. `seestar_client.py` has 66 exception handlers and is 2333 lines

**Severity:** Medium
**Where:** [src/seestar_client.py](../src/seestar_client.py)

Second-most-exception-dense file. Most handlers are defensive wrapping around JSON-RPC reads and socket operations, which is appropriate for hardware glue — but the module also contains `TransitRecorder` (at line 2096) and the prime_for_event wiring (line 2188). Scheduling bugs in `TransitRecorder.schedule_transit_recording` can silently drop a HIGH-event recording if `det.prime_for_event()` raises during initialisation — the debug log at [line 2190](../src/seestar_client.py#L2190) is the only trace.

**Fix direction:** promote the prime_for_event log line from `logger.debug` to `logger.warning` (it's a prediction↔detection contract failure, not a debug event). Consider extracting `TransitRecorder` into its own module — it has no reason to live inside the low-level client.

---

## 4. IMM Kalman filter has zero unit test coverage

**Severity:** High
**Where:** [src/imm_kalman.py](../src/imm_kalman.py) (515 lines), [tests/](../tests/)

`imm_kalman.py` is 515 lines of numerical code with per-ICAO24 state, Kalman update matrices, mode probability blending, filter cleanup, and an `angular_sigma` conversion. A `grep` across `tests/` for `imm_kalman` or `IMMKalman` returns zero matches. This is the most critical module in the prediction stack after `transit.py`, and it is completely untested.

If someone tweaks the process-noise covariance or the H matrix, there is no regression signal. The only way a bug would be caught is by a user reporting "the uncertainty band looks weird" — which, combined with Finding #1 (sep_1sigma may not even be reaching the UI), means **a broken IMM filter could persist for weeks undetected**.

**Fix direction:** add `tests/test_imm_kalman.py` with at minimum:

- `test_initialisation_from_first_observation` — one observation in, state out with sane velocity priors.
- `test_constant_velocity_convergence` — 10 fake observations along a straight line, check that the CV mode probability saturates and CA stays low.
- `test_turn_detection` — observations along a curved track, check that CA mode probability rises.
- `test_angular_sigma_monotone` — at fixed observer, `angular_sigma(sigma_m, distance)` must be monotonically decreasing in distance.
- `test_predict_then_update_reduces_covariance` — covariance after an update must be ≤ covariance after the predict step alone.
- `test_cleanup_stale_filters` — filters older than the retention window are dropped.

These are pure-numpy tests, no Flask, no network, no hardware. Should take a day to write.

---

## 5. Wavelet / matched-filter / CNN paths have zero direct test coverage

**Severity:** High
**Where:** [src/transit_detector.py](../src/transit_detector.py), [src/transit_classifier.py](../src/transit_classifier.py), [tests/](../tests/)

`grep` across `tests/` for `wavelet`, `matched_filter`, `_wavelet_detrend`, `_mf_hit_required`, or `TransitClassifier` returns zero. The detection pipeline's three flagship gates — D1 wavelet detrending, D2 matched-filter, E3 CNN classifier — are entirely exercised by live-video diagnostic scripts (`diag_phase2_live_detector_test.py`, `diag_phase5_synthetic_transit.py`) and not by unit tests. The diagnostic scripts require either a real Seestar, a recorded MP4, or a long-running subprocess — none of which run in CI.

**Consequence:** any refactor of the gate logic (e.g., changing the `_MF_TEMPLATES` tuple, adjusting the wavelet level, tweaking the confidence logit) relies on "I ran it against one recording and it looked OK." This is the opposite of the CLAUDE.md #3 directive ("every fix must be accompanied by a concrete test strategy").

**Fix direction:**

- `tests/test_wavelet_detrend.py` — synthesize a signal (slow 0.1 Hz drift + 0.5 Hz impulse + gaussian noise), run `_wavelet_detrend`, assert the impulse survives and the drift is suppressed. Pure numpy.
- `tests/test_matched_filter_gate.py` — construct a `triggered` buffer matching each of the 8 template durations, assert `_mf_hit_required` returns the expected count, assert the gate fires at or above the threshold and not below.
- `tests/test_transit_classifier.py` — skip if the model file is absent; otherwise construct a synthetic 15-frame clip (pure noise, pure step, pure transit-shaped), call `classify`, assert the availability flag and the output shape. Does not verify model accuracy — just the wiring.
- `tests/test_detector_confidence_logit.py` — call `_handle_detection_fire` in isolation with constructed inputs (mocked `_save_diagnostic_frames` and `_start_detection_recording`), assert the confidence_score is monotone in SNR and that each soft penalty has the documented effect.

---

## 6. `get_latest_snapshot()` picks the most recently fetched bbox by timestamp, not the one matching the event location

**Severity:** Medium
**Where:** [src/opensky.py:265-279](../src/opensky.py#L265-L279)

`get_latest_snapshot()` returns `max(_cache.values(), key=lambda v: v["ts"])["data"]`. If two bboxes are cached (e.g., the background monitor fetched a wide corridor at T-30s and the UI refreshed a narrower custom bbox at T-10s), the enrichment at detection time will use whichever was fetched most recently — **even if that bbox doesn't contain the aircraft that caused the transit**. In the worst case the enrichment will return the wrong aircraft or nothing at all, the CSV log will record the wrong `detected_flight_id`, and the prediction↔detection match logic downstream will silently misattribute the event.

This is probably rare in practice (both monitor and UI usually share a corridor) but the bug is latent and invisible — there is no log line that says "I picked snapshot A over snapshot B."

**Fix direction:** `get_latest_snapshot(near_alt=None, near_az=None)` — pick the cached bbox that geographically encloses (or is closest to) the event position. If no bbox contains the point, return `{}` and log a WARNING so the enrichment fallback path is taken.

---

## 7. Backoff windows create a blind spot when all ADS-B sources back off simultaneously

**Severity:** Medium
**Where:** [src/flight_sources.py](../src/flight_sources.py), `_SourceBackoff` class

Per-source exponential backoff (60 s → 3600 s cap) is good, but the multi-source architecture does not guarantee that at least one source is always queryable. If a network outage takes down multiple upstreams simultaneously, each enters its own backoff, and the outage-recovery moment can leave all sources backed-off for minutes.

More importantly: **there is no operator-visible signal** for "we currently have zero working ADS-B sources." `_record_http_call` tracks per-source usage for observability, but a dashboard panel showing "active sources: 0/5" does not exist.

**Fix direction:** add a `get_health()` function that returns per-source `{name, in_backoff, seconds_remaining, last_success_age_s}` and expose it via a `/api/adsb/health` endpoint. The UI should display a banner when active-source count drops to 1 (warning) or 0 (critical). The existing disc-lost warning UI is the template — add an adsb-lost warning next to it.

---

## 8. CNN advisory is one-way — no feedback loop from reviewed events into training data labelling

**Severity:** Medium
**Where:** [src/transit_detector.py](../src/transit_detector.py) (_save_training_clip), [data/training/unlabeled/](../data/training/unlabeled/)

Training clips are saved to `data/training/unlabeled/` labelled only by confidence tier (`strong`/`weak`/`speculative`) at save time. There is no mechanism to re-label a clip after a human has reviewed it ("that was actually a cloud, not a transit") and no guarantee the next training run won't happily ingest mislabelled data. The working tree currently has dozens of untracked `det_20260414_*_strong.npz` files from today's session alone — they will accumulate indefinitely.

**Fix direction:** add `data/training/labelled/` with subdirectories `transit/` and `not_transit/`, and a lightweight review route in the gallery UI that lets a reviewer reclassify an unlabelled clip with one click. The training pipeline should ignore `unlabeled/` entirely — only labelled clips contribute. This is a small UI change + a directory migration; it unblocks the CNN retraining loop.

---

## 9. Silent exception patterns in prediction-side code

**Severity:** Low-Medium
**Where:** [src/transit.py:55-56, 103-104](../src/transit.py#L55), [src/position.py:237-238](../src/position.py#L237)

- `transit.py:55-56` — `_save_fa_counts()` swallows any failure to write `data/fa_counts.json`. If the file is on a read-only volume or the disk is full, FA call tracking silently stops and cost tracking breaks.
- `transit.py:103-104` — `_load_fa_counts()` returns `0.0, 0` on any exception. Same issue — a corrupt file resets counters without any log line.
- `position.py:237-238` — `except (TypeError, ValueError): pass` in a coordinate conversion helper. If an upstream delivers a non-numeric lat/lon, the result is silently None and the aircraft is silently dropped from the prediction.

None of these are individually urgent. Collectively they represent a "swallow exceptions and hope for the best" habit that the wavelet and matched-filter functions also exhibit (`_wavelet_detrend` catches `Exception` and returns the raw signal without logging — fine at the inner-loop level, but means a pywt regression wouldn't be noticed for days).

**Fix direction:** introduce a `DEBUG_SWALLOW` env var that, when set, logs every swallowed exception at DEBUG. Default off. Developers turn it on during troubleshooting; production runs stay clean. A day's work, helps diagnose the next "it just stopped working" ticket.

---

## 10. CLAUDE.md references a stale working directory (`/Users/Tom/flymoon/`)

**Severity:** Low
**Where:** [CLAUDE.md](../CLAUDE.md)

The project instructions tell any assistant "Always modify active code in `/Users/Tom/flymoon/`" — but the actual repo is `/Users/Tom/Zipcatcher`. A fresh agent following CLAUDE.md literally could either write to a non-existent path or, worse, write to a stale `flymoon/` directory if one still exists. Found during Phase 0 of this audit.

**Fix direction:** update CLAUDE.md to `/Users/Tom/Zipcatcher/`, and add a line at the top stating that the `Flymoon` branding was renamed to `Zipcatcher` in v0.x and any `flymoon` references in legacy docs are historical.

---

## 11. `archive/` tree is importable and imports live code

**Severity:** Low
**Where:** [archive/examples/seestar_transit_trigger.py](../archive/examples/seestar_transit_trigger.py)

`archive/examples/seestar_transit_trigger.py` does `from src.seestar_client import TransitRecorder, create_client_from_env`. Python's import system is happy to execute this file if any tool or test runner picks it up. Nothing currently does, but the accidental hazard is real — a `pytest` collection glob or a packaging misconfiguration could pull legacy scripts into live runs.

**Fix direction:** add `archive/` to `pyproject.toml`/`setup.cfg` pytest collect-ignore, and add a README inside `archive/` stating the tree is frozen and `from src.*` imports will break if the relevant files are refactored. Or more aggressively: move `archive/` outside the repo entirely.

---

## 12. Documentation rot is itself an operational risk

**Severity:** Medium
**Where:** [docs/TRANSIT_PREDICTION_AND_DETECTION.md](./TRANSIT_PREDICTION_AND_DETECTION.md)

The canonical technical reference is ~8 months behind the code and reads as authoritative. Anyone (human contributor or AI assistant) planning work from that document will re-propose already-built features. This audit itself nearly generated a fully-independent scintillation-rejection blueprint duplicating the existing `TRANSIT_IMPROVEMENT_PLAN.md` before the code was actually read. Documentation drift has a cost measured in wasted engineering time per new contributor.

**Fix direction:** replace TRANSIT_PREDICTION_AND_DETECTION.md contents with a header pointing at `AS_BUILT_REFERENCE.md`, and set a rule (enforced in code review) that any PR touching `src/transit_detector.py`, `src/transit.py`, `src/imm_kalman.py`, or `src/flight_sources.py` must update `AS_BUILT_REFERENCE.md` in the same commit.

---

## Summary — severity rollup

| # | Finding | Severity | Effort |
|---|---|---|---|
| 1 | `sep_1sigma` block buggy / silent-None | High | < 1 day |
| 2 | telescope_routes.py size + error-path density | High | Medium: ~1 week (decorator + split + watchdog) |
| 3 | seestar_client.py exception density | Medium | < 1 day (logging + extract TransitRecorder) |
| 4 | Zero IMM Kalman test coverage | High | 1 day |
| 5 | Zero wavelet / matched-filter / CNN test coverage | High | 2–3 days |
| 6 | `get_latest_snapshot` picks wrong bbox | Medium | < 1 day |
| 7 | No operator-visible signal for all-sources-backoff | Medium | 1 day |
| 8 | CNN training loop has no labelling feedback | Medium | 2–3 days |
| 9 | Silent exception habit (swallow-and-continue) | Low-Medium | 1 day (DEBUG_SWALLOW env) |
| 10 | CLAUDE.md stale working directory | Low | 5 minutes |
| 11 | `archive/` tree imports live code | Low | 30 minutes |
| 12 | Documentation rot is itself a risk | Medium | Ongoing policy |

**Highest-priority set for v0.2.0:** Findings 1, 4, 5 directly (code + tests), Finding 2(a) (decorator refactor only; defer split), Finding 10 (CLAUDE.md one-line fix), Finding 12 (replace the stale doc pointer). Rough budget: 5–7 days of focused work. Everything else slides to v0.2.1.
