# Phase 5: Integration & End-to-End Validation — Session Notes
**Date:** 2026-03-21 / updated 2026-03-22  
**Status:** 🟢 All Phase 5 automated tests PASSED

---

## Context (Phases 1–4 Summary)

All four root causes from Phase 4 were diagnosed and fixed live against firmware 7.06:

| # | Root Cause | Fix |
|---|---|---|
| 1 | GoTo servo loop diverging | `atan2(d_az, -d_alt)` — confirmed converging 15.5°→3.8° in 90 s |
| 2 | ▲/▼ nudge buttons inverted | Swapped button angles (▲: 270, ▼: 90) so +180 firmware offset maps correctly |
| 3 | Position feedback blind in scenery mode | `scope_get_horiz_coord` added as primary alt/az source in `get_telemetry()` |
| 4 | Firmware mode name mismatch | Added `solar_sys`/`lunar` → `sun`/`moon` mapping in `get_telemetry()` |

---

## Phase 5 Work Completed This Session

### Diagnostic scripts written

| Script | Purpose | Status |
|---|---|---|
| `tests/diag_phase5_synthetic_transit.py` | Synthetic flight injection → prediction pipeline validation | ✅ Written |
| `tests/diag_phase5_soak_test.py` | RTSP sustained-operation (2 h soak, 3 consumers) | ✅ Written |
| `tests/diag_phase5_failure_injection.py` | JSON-RPC reconnect + RTSP kill + rapid mode cycling | ✅ Written |

### Phase 5 tests run (2026-03-22) against firmware 7.06

| Test | Result | Notes |
|---|---|---|
| Synthetic transit injection | ✅ PASS | SYN_HIGH: sep=1.253°, level=HIGH, t=5.03 min |
| Test A: JSON-RPC reconnect | ✅ PASS | Recovery 5.0 s < 30 s threshold |
| Test B: RTSP kill/recover | ✅ PASS | Instant recovery (<1 s) |
| Test C: Mode cycling (5 cycles) | ✅ PASS | 0 mismatches, 0 exceptions |

### Bugs found and fixed during this session

**Bug 1 — `diag_phase5_failure_injection.py` used wrong telemetry keys**  
`get_telemetry()` returns firmware telemetry (no `connected`/`client_viewing_mode` keys).  
The test was checking `telem.get("connected")` which always returned `None` → all Test C cycles
reported failure. Fix: check `client._connected` and `client._viewing_mode` directly.

**Bug 2 — `get_telemetry()` race: `solar_sys` overwrites `moon` mode during transition**  
When `start_lunar_mode()` sends the mode-switch command, the firmware still briefly reports
`solar_sys`. The existing telemetry sync code unconditionally set `_viewing_mode = "sun"`,
clobbering the newly set "moon". Fix in `src/seestar_client.py`: only update to `"sun"` from
`solar_sys` when `_viewing_mode` is not already `"sun"` or `"moon"`.

**Note on Test B prerequisite**: RTSP stream must be active (Seestar in solar mode) before
running Test B. First call `start_solar_mode()` or ensure the scope is in solar/lunar mode.

---

## How to Resume Tomorrow

### Prerequisites
- Seestar powered on, in **solar mode**, pointed at the Sun
- `python app.py` running (for the optional `--app-url` check in Test 1)
- `.env` loaded (all scripts call `load_dotenv()` automatically)

### Step 1 — Synthetic transit injection (no hardware required)
```bash
cd ~/flymoon && source .venv/bin/activate

python tests/diag_phase5_synthetic_transit.py \
  --observer-lat 33.111369 --observer-lon -117.310169 \
  --target sun --transit-in-minutes 5

# Expected output:
# ✅ PASS: SYN_HIGH flight detected as HIGH probability
```

If the app is running:
```bash
python tests/diag_phase5_synthetic_transit.py \
  --observer-lat 33.111369 --observer-lon -117.310169 \
  --target sun --transit-in-minutes 5 \
  --app-url http://localhost:5000
```

### Step 2 — RTSP soak test (quick smoke, 5 minutes)
```bash
python tests/diag_phase5_soak_test.py --duration 300

# For the full 2-hour soak:
python tests/diag_phase5_soak_test.py --duration 7200
```
Success criteria: all consumers show >0 frames/minute, max recovery time <30 s.  
Logs land in `docs/diag_logs/phase5_soak_<timestamp>.log`.

### Step 3 — Failure injection
```bash
python tests/diag_phase5_failure_injection.py --mode-cycles 5

# Run individual tests only:
python tests/diag_phase5_failure_injection.py --tests A    # JSON-RPC reconnect
python tests/diag_phase5_failure_injection.py --tests B    # RTSP kill/recover
python tests/diag_phase5_failure_injection.py --tests C    # Mode cycling
```
Success criteria: all three pass within 30 s recovery window, no mode mismatches.

### Step 4 — Real-world soak (4-hour session)
```bash
python app.py
# In another terminal:
tail -f flymoon.log | grep -E "ERROR|WARNING|TransitMonitor|Detector|Seestar"
```
Record: prediction count, HIGH/MEDIUM alerts, RTSP drops, unhandled exceptions.

---

## Success Criteria Checklist (from DIAGNOSTIC_PLAN.md)

- [x] Synthetic HIGH transit detected by prediction pipeline within 1 refresh cycle
- [ ] Recording triggered by prediction (transit_capture.py in automatic mode) — *requires live transit*
- [ ] RTSP stable for 2+ continuous hours (all 3 consumers) — *run `diag_phase5_soak_test.py --duration 7200` at next session*
- [x] Recovery from socket/WiFi drop < 30 s (Test A: 5.0 s)
- [ ] No unhandled exceptions in 4-hour soak test — *run Step 4 at next session*
- [x] All subsystem metrics logged and reviewable

---

## File Inventory (Phase 5)

```
tests/
  diag_phase5_synthetic_transit.py   # prediction pipeline test
  diag_phase5_soak_test.py           # RTSP sustained operation
  diag_phase5_failure_injection.py   # failure recovery (A/B/C)

docs/
  diag_phase5_results.md             # this file
  DIAGNOSTIC_PLAN.md                 # updated with Phase 5 progress note
```

---

## Notes / Observations

- `get_transits(..., test_mode=True)` reads from `data/raw_flight_data_example.json`.  
  `diag_phase5_synthetic_transit.py` overwrites that file with a purpose-built HIGH flight, then restores the module path.  
  Do **not** run `app.py --test` simultaneously with the synthetic transit test (it will compete for the same file).

- The soak test spawns three concurrent ffmpeg processes. On macOS, if ffmpeg is bundled in the app, set `SEESTAR_FFMPEG_PATH` in `.env` or the test will fall back to the system `ffmpeg`.

- Test C (mode cycling) leaves the scope in **solar mode** at the end, ready for observation.

- The RTSP stream URL is `rtsp://<SEESTAR_HOST>:<SEESTAR_RTSP_PORT>/stream` (defaults: `192.168.4.112:4554`).
