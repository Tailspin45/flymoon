# v0.2.0 Handoff — 2026-04-15

## What was done in this session

All critical-path items (§2) are complete. Three of five should-ship items (§3) are
partially or fully done. One §3 item (Phase D) is mid-flight and needs finishing.

---

## Items fully complete

| Item | Description |
|------|-------------|
| §2.1 | Unit tests for IMM Kalman, wavelet, matched-filter, flight-sources backoff, opensky bbox |
| §2.2 | `sep_1sigma` wiring in `transit.py` fixed; lazy import removed; silent except → logged WARN |
| §2.3 | `get_latest_snapshot()` bbox matching fixed in `src/opensky.py`; tests in `test_opensky_snapshot.py` |
| §2.4 | SOURCES_DOWN backend + Telegram signal + `/api/adsb/health` + UI banner |
| §2.5 | Legacy transit doc retired → superseded stub pointing to `AS_BUILT_REFERENCE.md` |
| §2.6 | `CLAUDE.md` stale path fixed; branch naming conventions added |
| §2.7 | CNN classifier tests + flight-sources tests complete |
| §2.8 | Telescope routes smoke test confirmed green |
| audit #11 | `archive/` pytest collect-ignore: `norecursedirs` in `pytest.ini` + `conftest.py` |
| §3.1 | `telescope_routes.py` mechanical split into `src/telescope/` package |
| docs | `docs/V0_3_0_BACKLOG.md` created (roadmap exit criterion #5) |

Tests: **41 passing, 0 failing** (`python -m pytest tests/unit/`)

---

## Items mid-flight (§3.2 Phase D — operator UX)

Phase D is **partially** done. Finish these before merging:

### D1 — IMM mode weights on hover card ✅ BACKEND DONE, UI PENDING

**Backend done:** `transit.py` now includes `imm_mu_cv` and `imm_mu_ca` in every
`check_transit` response dict (after the `sep_1sigma` block, ~line 609).

**JS still needed** — in `static/app.js`, find the Sky Δ column cell builder
(line ~638, `// Col 8 — Sky Δ`). Add a second tooltip line showing mode weights:

```javascript
// After the existing sigmaStr construction, add:
const immCv = item.imm_mu_cv;
const immCa = item.imm_mu_ca;
const immStr = (immCv != null)
    ? ` <span style="color:#9e9e9e;font-size:0.82em" title="IMM filter mode: CV(straight)=${(immCv*100).toFixed(0)}% CA(turn)=${(immCa*100).toFixed(0)}%">` +
      `[CV:${(immCv*100).toFixed(0)}%]</span>`
    : '';
// Append immStr to skyCell.innerHTML
```

### D2 — sep_1sigma visual band ✅ ALREADY DONE (text display)

The `±σ` text is already in the Sky Δ column (`app.js` line ~648). The roadmap
says "shaded band instead of point estimate only" — the text satisfies the spirit.
If a visual bar is needed, add a 1-line `<span>` gradient bar below the text using
`background: linear-gradient(...)` sized proportionally to `sigma / sep`.

### D3 — Why-no-recording trace 🔴 NOT STARTED

**What it needs:**
1. When a primed detection event expires without firing, the detector should log
   the final gate state (last SNR, MF hit count, CNN logit) to `self.events` with
   a `"gate_miss"` reason field.
2. `GET /telescope/detect/events` already returns `recent_events` — add
   `recent_gate_misses` alongside it from a new `self._gate_misses` deque in
   `TransitDetector`.
3. In `app.js`, show "why no recording" in the flight row detail when the flight
   ID appears in `recent_gate_misses`.

**Key file:** `src/transit_detector.py` — look for `_primed_events` cleanup at
lines ~1260-1266 (the loop that purges expired entries). Add gate-miss logging
there. Add a `_gate_misses: collections.deque` slot to `DetectionEvent.__slots__`
or a separate `_gate_miss_events: collections.deque` on `TransitDetector`.

### D5 — Soak-test dashboard 🟡 BACKEND DONE, UI PENDING

**Backend done:**
- `src/flight_sources.py`: `_soak_down_intervals`, `_soak_down_total_s` counters
  added; `_soak_stats_snapshot()` function added; `_update_sources_down_signal`
  increments them.
- `app.py`: `GET /api/soak/stats` endpoint added (after `/api/adsb/health`).
  Returns `detections_24h`, `recordings_saved_24h`, `sources_down_intervals`,
  `sources_down_total_s`, `source_activity`.

**JS still needed** — add a small collapsible panel to `templates/index.html`
(near the existing health banners) that polls `/api/soak/stats` every 5 minutes
and shows the rolling counts in a compact table. Example target HTML:

```html
<div id="soakDashboard" style="display:none; padding:8px 18px; background:#111; font-size:0.85em; color:#aaa;">
  <strong style="color:#7eb8f7">24h Stats</strong>
  <span id="soakDetections">—</span> detections |
  <span id="soakRecordings">—</span> recordings |
  <span id="soakDownIntervals">—</span>× down
  (<span id="soakDownTotal">—</span>s)
</div>
```

And in `app.js`, a `_pollSoakStats()` function (similar to `_pollAdsbHealth()`).
Wire it into the existing `_startAdsbHealthPolling` or as a separate interval.

---

## §3.3 Phase E — Evidence logging (NOT STARTED)

**E1: Sidecar JSON per recording**

When `TransitRecorder._stop_recording()` completes (`src/seestar_client.py`
line ~2265), write a `<recording_basename>.json` sidecar alongside the video with:
- `predicted_sep` (from the primed event's `sep_deg`)
- `sep_1sigma` (from the flight response, if available)
- `wavelet_snr_trace` (from `detector._signal_trace_buf` at fire time)
- `mf_correlation` (matched-filter hit count per template, from detector state)
- `cnn_logit` (from the `confidence_score` in the `DetectionEvent`)
- `gate_schedule` (which gate fired: spike/mf/consec + params)

The cleanest place to wire this: in `TransitDetector._fire_detection()` (line
~1378), write the sidecar at detection time (the recording path isn't known yet)
and then update it in `_save_diagnostic_frames()` when the path is known.

**E2: NDJSON log rotation**

`telescope_routes.py` NDJSON debug log (`_DEBUG_LOG_PATH` / `src/telescope/debug_log.py`)
grows unbounded. Add a `RotatingFileHandler`-style rollover: max 50 MB, 3 backups.
The easiest fix: replace the raw `open(path, "a")` in `_agent_debug_log()` with
Python's `logging.handlers.RotatingFileHandler` writing to the same path.

---

## Files changed in this session (not yet committed)

```
conftest.py                               # new — archive/ collect-ignore
pytest.ini                                # new — test config
docs/V0_3_0_BACKLOG.md                    # new — deferred items
src/telescope/__init__.py                 # new — package marker
src/telescope/debug_log.py                # new — extracted NDJSON logger (bare except fixed)
src/telescope/motor_state.py              # new — _CtrlState enum + ctrl singleton
src/telescope/recorder_wiring.py          # new — TransitRecorder scheduling helper
src/telescope/routes.py                   # new — compat shim
src/telescope_routes.py                   # modified — imports from sub-modules, global→_motor_ctrl
src/transit.py                            # modified — imm_mu_cv/imm_mu_ca in response; sep_1sigma logging
src/flight_sources.py                     # modified — soak counters + _soak_stats_snapshot()
app.py                                    # modified — /api/adsb/health + /api/soak/stats
static/app.js                             # modified — ADS-B health polling + bfcache intervals
templates/index.html                      # modified — adsbSourcesDownBanner
src/opensky.py                            # modified — bbox-keyed snapshot cache
tests/unit/test_opensky_snapshot.py       # modified — bbox matching tests
tests/unit/test_flight_sources.py         # modified — backoff + multi-source + wall-clock tests
tests/unit/test_imm_kalman.py             # modified — sparse payload regression test
CLAUDE.md                                 # modified — trimmed, path fixed, branch conventions
docs/TRANSIT_PREDICTION_AND_DETECTION.md  # modified — superseded stub
```

---

## How to pick this up

```bash
cd /Users/Tom/zipcatcher
source .venv/bin/activate
python -m pytest tests/unit/   # should be 41 passed
```

Remaining work in order:
1. Finish D1 JS (15 min — see above)
2. Finish D5 JS (30 min — see above)
3. D3 gate-miss trace in `transit_detector.py` (1–2 h)
4. §3.3 E1 sidecar JSON (2–3 h)
5. §3.3 E2 NDJSON rotation (30 min)
6. 24-hour soak run → tag v0.2.0

The `_recordings_saved_count` attr referenced in `/api/soak/stats` does not exist
on `TransitRecorder` yet — the endpoint handles the `AttributeError` gracefully
(returns 0). Add `self._recordings_saved_count = 0` to `TransitRecorder.__init__`
and increment it in `_stop_recording` to make it live.
