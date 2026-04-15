# v0.2.0 Roadmap

**Branch:** `v0.2.0`
**Cut from:** `main` at tag `v0.1.6`
**Date:** 2026-04-14
**Framing:** Operational reliability hardening. **Not** a greenfield reference implementation — every item below exists in the code and needs to be fixed, tested, or cleaned.

**Companion docs:**
- [AS_BUILT_REFERENCE.md](AS_BUILT_REFERENCE.md) — canonical description of the system as it actually ships
- [OPERATIONAL_RISK_AUDIT.md](OPERATIONAL_RISK_AUDIT.md) — 12 numbered findings with severity ratings
- [TEST_GAPS.md](TEST_GAPS.md) — test-suite reality check and required new coverage

---

## 1. Goals for v0.2.0

1. **Zero silent-failure paths** on the prediction → detection → recording critical chain. Any failure that today drops data on the floor must become either a recovered operation or a logged, alertable event.
2. **Test-gated refactoring** of the largest risk concentration (telescope_routes.py). No structural changes land without the unit tests that pin current behaviour.
3. **Documentation that matches the code.** The legacy transit reference is ~8 months stale; [AS_BUILT_REFERENCE.md](AS_BUILT_REFERENCE.md) is its replacement. The stale doc must be retired or clearly marked.
4. **Complete the surviving work from v0.1's [TRANSIT_IMPROVEMENT_PLAN.md](TRANSIT_IMPROVEMENT_PLAN.md).** Phases A-C are done; Phase D (operator UX) and Phase E (evidence logging) are still open and still valuable.

**Non-goals for v0.2.0:**
- No new detector backbone. The wavelet + matched-filter + CNN stack is adequate; we are not swapping it for a transformer.
- No new prediction model. The IMM Kalman + 5-source ADS-B is adequate; we are fixing how it's wired, not replacing it.
- No new hardware support. Seestar S50 remains the only target.

---

## 2. Critical path (must ship)

Ordered so each item unblocks the next. Total budget: **~7 working days**.

### 2.1 Pin current behaviour of untested hot paths — ~3 d

Before touching any of the risky modules, land the unit tests from [TEST_GAPS.md §3](TEST_GAPS.md#3-critical-gaps-must-fix-in-v020):

- `tests/unit/test_imm_kalman.py` (§3.1, 1.5 d)
- `tests/unit/test_wavelet_detrend.py` (§3.2, 0.5 d)
- `tests/unit/test_matched_filter.py` (§3.3, 1.0 d)

**Why first:** every subsequent item risks breaking one of these. Landing the tests first means refactors can run green-to-green instead of hoping.

**Exit criteria:**
- `pytest tests/unit/` passes on a clean checkout with no hardware.
- Coverage report (new) shows `src/imm_kalman.py` at ≥80% line coverage, wavelet + MF paths at ≥70%.

### 2.2 Fix `sep_1sigma` wiring in `transit.py:580-603` — ~0.5 d

**Audit finding 1** (High).

Current state: `try: pass`, then a second import of `angular_sigma`, then a `float(response.get(...))` whose result is thrown away, then an inline `__import__('math')`, then a bare `except Exception: response['sep_1sigma'] = None`. Every failure silently yields `None`, which the frontend renders as "unknown" — no operator sees anything wrong.

Work:
1. Land `tests/unit/test_sep_1sigma.py` ([TEST_GAPS.md §4.1](TEST_GAPS.md#41-sep_1sigma-regression-test)) pinning expected output for three scenarios: filter present, filter absent, degenerate distance.
2. Rewrite the block as a small helper function with named returns and exactly one try/except around the IMM state lookup.
3. When no filter exists, log INFO (not silent) with the ICAO24.
4. Ensure degrees, not radians, land in the response — current code has a unit bug in at least one branch.

**Exit criteria:** new tests green, manual smoke against a live multi-source bbox shows sep_1sigma populated for filtered aircraft.

### 2.3 Fix `get_latest_snapshot` bbox matching — ~0.5 d

**Audit finding 6** (Medium).

`src/opensky.py` currently does `max(_cache.values(), key=lambda v: v['ts'])['data']` — which returns the **temporally** newest cached snapshot regardless of whether its bbox covers the requested event. If an earlier request cached a snapshot for a different observer location, the predictor will happily use it.

Work:
1. Land `tests/unit/test_opensky_snapshot.py` ([TEST_GAPS.md §4.2](TEST_GAPS.md#42-get_latest_snapshot-bbox-matching)).
2. Change the lookup to match by (rounded) bbox corners, fall back to `None` on miss.
3. Caller in `transit.py` must treat `None` as "no data available" and log at WARN.

**Exit criteria:** new tests green, replay of an older recording against the current code reproduces the same predicted aircraft set (no regressions).

### 2.4 Raise an operator signal when every ADS-B source is in backoff — ~0.5 d

**Audit finding 7** (Medium). Depends on [TEST_GAPS.md §3.5](TEST_GAPS.md#35-multi-source-ads-b-src-flight_sourcespy-_sourcebackoff).

Today, when OpenSky + ADSB-One + adsb.lol + adsb.fi + ADSBX + dump1090 are all either rate-limited or unreachable, `fetch_flights_in_bbox()` returns `[]` silently. The dashboard shows "0 aircraft in view" and no one notices.

Work:
1. Land `tests/unit/test_flight_sources.py` pinning the backoff schedule and the all-down case.
2. Emit a WARN log + a `SOURCES_DOWN` event to the existing Telegram notifier path when the all-down condition persists for more than 90 s.
3. Surface a small red indicator in the web UI's status strip (existing health pill — add a new state).

**Exit criteria:** simulated all-sources-down run for 2 minutes produces exactly one WARN log, one Telegram notification, and one UI state change. No flapping.

### 2.5 Retire legacy transit reference doc — ~0.25 d

**Audit finding 12** (Medium).

The old doc describes a 160×90 @ 15 fps EMA-only pipeline. Reality is 180×320 @ 30 fps with wavelet + matched-filter + CNN. Every onboarding session since Q3 has hit the same wall.

Work:
1. Replace the body of the legacy transit reference doc with a one-line pointer: "Superseded by [AS_BUILT_REFERENCE.md](AS_BUILT_REFERENCE.md). See git history for the v0.1 version."
2. Update README's docs table-of-contents.
3. Grep the repo for other references to the old filename and either redirect or delete.

**Exit criteria:** legacy transit-reference mentions in active docs are reduced to the superseded stub pointer only.

### 2.6 Fix stale CLAUDE.md working-directory path — ~0.1 d

**Audit finding 10** (Low).

CLAUDE.md §6 ends with: *"Always modify active code in /Users/Tom/flymoon/ and never touch legacy files under /Users/Tom/flymoon/archive/development/dist/Zipcatcher-Web/."* Both paths are stale. Active code lives in `/Users/Tom/Zipcatcher/`.

Work: one Edit to CLAUDE.md updating the path. Also double-check §4 "Project Context" for any other `flymoon` references.

### 2.7 CNN classifier + multi-source tests — ~1.5 d

- `tests/unit/test_transit_classifier.py` ([TEST_GAPS.md §3.4](TEST_GAPS.md#34-onnx-cnn-classifier-srctransit_classifierpy), 0.5 d)
- `tests/unit/test_flight_sources.py` finalization if not already done in 2.4 (1.0 d)

### 2.8 Telescope routes regression smoke — ~0.5 d

**Audit finding 2a** (High — precursor).

Before any structural split of [telescope_routes.py](../src/telescope_routes.py) (5398 lines, 133 except blocks), land a smoke test that GETs/POSTs every registered endpoint against `MockSeestarClient`. This is the safety net for item 3.1 below.

**Exit criteria:** a `pytest tests/test_telescope_routes_smoke.py` run exercises every `@app.route` in the module with at least one valid-shape request, and returns 2xx or a documented error code.

---

## 3. Should-ship (v0.2.0 if 2.x finishes with time left)

### 3.1 First-pass split of `telescope_routes.py` — ~2 d

**Audit finding 2** (High).

5398 lines in one module is now the single biggest operational risk after the sep_1sigma block. The file mixes:
- HTTP route handlers
- A motor state machine (`_ctrl_state`, `_ctrl_lock`)
- A NDJSON debug logger (with `except: pass` at line 103)
- The `TransitRecorder` wiring
- Alpaca auto-discovery glue
- Joystick + nudge utilities

Split (do **not** rewrite) into:
- `src/telescope/routes.py` — Flask blueprint only
- `src/telescope/motor_state.py` — `_ctrl_state`, lock, nudge/joystick loops
- `src/telescope/recorder_wiring.py` — TransitRecorder + `prime_for_event` glue
- `src/telescope/debug_log.py` — NDJSON logger with real error handling (not `except: pass`)

**Prerequisite:** item 2.8 (smoke test) must be green first.

**Effort:** 2 days if kept as a mechanical move; a full week if we try to clean up the 133 except blocks at the same time. **Keep it mechanical in v0.2.0.** Cleaning excepts is v0.3.

### 3.2 Phase D — operator UX items from [TRANSIT_IMPROVEMENT_PLAN.md](TRANSIT_IMPROVEMENT_PLAN.md) — ~1.5 d

From the earlier v0.1 plan, the surviving Phase D items:

- D1: Show per-aircraft IMM state (CV vs CA mode weight) on the hover card.
- D2: Show sep_1sigma as a shaded band on the prediction chart instead of the point estimate only.
- D3: "Why no recording?" trace — for any transit that predicted HIGH but produced no recording, show which gate refused (SNR, MF, CNN, cooldown, buffer miss).
- D5: Soak-test dashboard — 24-hour rolling counts of sources up/down, recordings written, false positives.

D4 (auto-tuning of detection thresholds) is explicitly deferred to v0.3 — it needs real labeled data that we do not yet have.

### 3.3 Phase E — evidence logging — ~1 d

From the v0.1 plan, Phase E items that survived:

- E1: Every recording writes a sidecar JSON with: predicted sep, sep_1sigma, wavelet SNR trace, MF correlation per template, CNN logit, gate schedule.
- E2: Log rotation + retention for the NDJSON debug stream that currently grows unbounded.

---

## 4. Explicitly deferred to v0.3+

- Refactor of the 133 except blocks in `telescope_routes.py` — mechanical split in 3.1 is enough for v0.2.0.
- CNN retraining pipeline with real labels — needs data collection first.
- Hypothesis / property-based tests — nice-to-have, not urgent.
- GitHub Actions CI — desirable, but blocked on deciding whether tests run on a self-hosted runner with the ONNX file available.
- Any swap of the detector backbone (transformer, learned matched filter, etc.) — not happening in v0.2.0.
- Support for additional telescopes (ZWO, Celestron, etc.) — not in scope.

---

## 5. Mapping to audit findings

| Audit finding (severity) | Roadmap item | Status in this plan |
|---|---|---|
| 1. sep_1sigma buggy (High) | 2.2 | **Critical path** |
| 2a. telescope_routes.py smoke (High precursor) | 2.8 | **Critical path** |
| 2. telescope_routes.py split (High) | 3.1 | Should-ship |
| 3. seestar_client.py 66 excepts (Medium) | deferred to v0.3 | Noted |
| 4. IMM Kalman untested (High) | 2.1 | **Critical path** |
| 5. Wavelet/MF/CNN untested (High) | 2.1 + 2.7 | **Critical path** |
| 6. get_latest_snapshot bbox bug (Medium) | 2.3 | **Critical path** |
| 7. No signal on all-sources-down (Medium) | 2.4 | **Critical path** |
| 8. CNN has no labelling loop (Medium) | v0.3 | Deferred |
| 9. Silent-except habit (Low-Med) | 3.1 partial | Should-ship |
| 10. CLAUDE.md stale path (Low) | 2.6 | **Critical path** |
| 11. `archive/` imports live code (Low) | v0.3 | Deferred |
| 12. Documentation rot (Medium) | 2.5 | **Critical path** |

---

## 6. Surviving items from v0.1 [TRANSIT_IMPROVEMENT_PLAN.md](TRANSIT_IMPROVEMENT_PLAN.md)

The v0.1 plan had phases A-E. Status as of the branch cut:

| Phase | Topic | Status |
|---|---|---|
| A | Wavelet detrend + matched filter | **Shipped** (code confirms) |
| B | IMM Kalman + sep_1sigma | **Partially shipped** — Kalman exists, sep_1sigma wiring is broken (item 2.2) |
| C | Multi-source ADS-B with backoff | **Shipped** — but no operator signal (item 2.4) |
| D | Operator UX — hover card, why-no-record, soak dashboard | **Not started** — item 3.2 |
| E | Evidence sidecars + log rotation | **Not started** — item 3.3 |

The academic literature citations L1-L7 from the old plan are no longer driving any decisions. They were useful during the scintillation / matched-filter design in v0.1. For v0.2.0 the framing is reliability, not novelty — we are not motivating work from papers.

---

## 7. Exit criteria for the branch

v0.2.0 is ready to tag and merge when:

1. All items in §2 are shipped and green.
2. `tests/unit/` runs clean with no hardware and no network.
3. [AS_BUILT_REFERENCE.md](AS_BUILT_REFERENCE.md) is still accurate (update it with any delta from the work in §2 and §3 as we go).
4. A 24-hour soak run on the production observer location shows:
   - Zero silent-empty-aircraft intervals longer than 90 s without a logged WARN.
   - sep_1sigma populated on ≥95% of predicted transits.
   - No new exception patterns in `logs/` not seen in v0.1.6.
5. The items deferred to v0.3 are tracked in a new `docs/V0_3_0_BACKLOG.md` (create at merge time).

---

## 8. Rough schedule

Assuming single-developer work, ~6 productive hours/day:

| Day | Work |
|---|---|
| 1 | §2.1a — `test_imm_kalman.py` |
| 2 | §2.1b — `test_wavelet_detrend.py` + `test_matched_filter.py` start |
| 3 | §2.1c — finish MF tests |
| 4 | §2.2 sep_1sigma + §2.3 snapshot bbox |
| 5 | §2.4 all-sources-down signal + §2.5 doc retire + §2.6 CLAUDE.md |
| 6 | §2.7 CNN + flight_sources tests |
| 7 | §2.8 telescope routes smoke |
| 8-9 | §3.1 telescope_routes split (if on track) |
| 10 | §3.2 Phase D operator UX items |
| 11 | §3.3 Phase E evidence sidecars |
| 12 | Soak test + fixes + tag |

Hard budget: 7 days for §2 (critical path). Stretch: 12 days including §3. If §2 slips, drop §3 items — do not drop §2.
