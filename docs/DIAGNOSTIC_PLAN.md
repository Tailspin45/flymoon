# Flymoon Diagnostic & Repair Plan

## System Overview

Flymoon predicts and captures aircraft transits across the Sun and Moon. It combines:

- **Prediction**: OpenSky ADS-B data + Skyfield ephemeris → angular separation → transit probability.
- **Detection**: Dual-signal CV algorithm on live RTSP stream (160×90 @ 15 fps) with adaptive thresholds.
- **Telescope Control**: Seestar S50 via JSON-RPC 2.0 (port 4700), alt/az GoTo via servo loop, solar/lunar tracking modes.
- **RTSP Streaming**: 3 concurrent FFmpeg processes (low-res detector, high-res pre-buffer, preview) from port 4554.

### Primary Problem Domains (priority order)

1. **RTSP stability** — stream silently dies, reconnects, dies again.
2. **Detection robustness** — no confirmed real transit capture; grasshopper swarm detected once but config may have drifted.
3. **Prediction accuracy** — untested against ground truth; OpenSky latency and rate limits.
4. **Alt/az GoTo reliability** — stale position feedback in scenery mode; `pi_set_time` unknown.

---

## How to Use This Plan

Each phase runs in a **separate chat**. At the start of that chat:

1. Paste the **Phase N section** from this document (just that phase, not the whole plan).
2. Attach or reference the **required inputs** listed for that phase.
3. Paste the **results summary** from the previous phase (a short paragraph produced at the end of each phase).
4. Tell Claude: "Execute Phase N. The plan is in `docs/DIAGNOSTIC_PLAN.md`. Previous phase results: [paste summary]."

**Naming convention for artifacts:**
- Diagnostic scripts: `tests/diag_phaseN_description.py`
- Log captures: `docs/diag_logs/phaseN_description.log`
- Results summaries: appended to the bottom of this file under "Phase Results"

**No production code is modified until a phase's design is approved.** Each phase has an explicit checkpoint.

---

## Phase 0: Inventory & Baseline

### Purpose
Establish ground truth about current system state. Capture baseline logs and confirm hardware connectivity before any changes.

### Scope
- Verify Seestar JSON-RPC connection and `pi_set_time` behavior
- Verify RTSP stream availability and measure time-to-first-frame
- Confirm current detection parameters match what's in code
- Catalog the 3 sample transit videos and verify they play/decode
- Run existing test suite and record pass/fail baseline

### Out of Scope
- No fixes, no parameter changes, no code modifications

### Required Inputs
- Hardware: Seestar S50 powered on, connected to network, in Station Mode
- iOS Seestar app: running (you mentioned 8 connections supported)
- `.env` file: current production values (will be read, not modified)
- Transit sample videos: `/Users/Tom/flymoon/transits from David/transit-{1,2,3}.mp4`

### Diagnostic Steps

1. **Connection & Time Sync**
   - Connect to Seestar via `SeestarClient.connect()`
   - Capture full connection log (including `pi_set_time` response)
   - Query `scope_get_equ_coord` and record raw response
   - Query device state and record firmware version
   - Log all unsolicited Event messages for 30 seconds

2. **RTSP Baseline**
   - Probe RTSP stream with `ffprobe` — record codec, resolution, fps
   - Time how long from mode-start to first decodable frame
   - Start 3 concurrent FFmpeg readers (matching production config); log which ones connect, how long each lasts before failure
   - Record exact FFmpeg error/exit-code when stream dies

3. **Detection Parameter Snapshot**
   - Read current env vars: `CONSEC_FRAMES_REQUIRED`, `CENTRE_EDGE_RATIO_MIN`, `DETECTION_COOLDOWN`, `SENSITIVITY_SCALE`, etc.
   - Compare to hardcoded defaults in `transit_detector.py`
   - Record any mismatches

4. **Sample Video Inventory**
   - Run `ffprobe` on each of the 3 transit videos: resolution, duration, codec, fps
   - Extract 1 frame from each and verify they show a solar/lunar disk with a transit silhouette
   - These will be test fixtures for Phase 3

5. **Existing Test Suite**
   - Run `python3 tests/test_integration.py` and record output
   - Run `python3 tests/test_classification_logic.py` and record output
   - Run `python3 tests/test_transit_detection.py` and record output
   - Run `python3 tests/test_seestar_altaz_roundtrip.py` and record output

### Design/Decision Outputs
- Baseline inventory document: firmware version, RTSP capabilities, detection config, test results
- List of any immediate red flags (e.g., `pi_set_time` failing, RTSP never connecting, tests failing)

### Tests and Success Criteria
- [ ] JSON-RPC connection succeeds and heartbeat runs for 30+ seconds
- [ ] `pi_set_time` response captured (success or failure — either is information)
- [ ] RTSP probe returns valid stream metadata
- [ ] At least 1 of 3 concurrent FFmpeg readers stays alive for 60 seconds
- [ ] All 3 transit videos decode successfully
- [ ] Test suite runs (pass or fail — baseline recorded)

### Checkpoint
**No code changes in this phase.** Output is a baseline document. Review it and confirm before proceeding to Phase 1.

### Results Template
```
Phase 0 Results:
- Firmware: [version]
- pi_set_time: [success/fail/not-sent]
- RTSP: [codec, resolution, fps, time-to-first-frame]
- Concurrent streams: [how many survived 60s]
- RTSP failure mode: [exact error]
- Detection config: [any env overrides vs defaults]
- Transit videos: [duration, resolution, visible transit? for each]
- Test suite: [N passed, M failed, list failures]
- Red flags: [list]
```

---

## Phase 1: RTSP Stability

### Purpose
Diagnose and fix the silent RTSP stream death. This is the highest-priority issue because detection, preview, and recording all depend on a stable stream.

### Scope
- Determine root cause of silent stream death
- Determine if 3 concurrent RTSP clients is the bottleneck
- Implement a reliable reconnection strategy
- Validate fix under sustained operation (30+ minutes)

### Out of Scope
- Detection algorithm changes
- Telescope control changes

### Required Inputs
- Phase 0 results (especially RTSP failure mode and concurrent stream behavior)
- `src/transit_detector.py` (lines 600–860: `_reader_loop`, `_hires_reader_loop`)
- `src/telescope_routes.py` (lines 2400–2501: preview stream)
- Access to running Seestar in solar mode

### Diagnostic Steps

1. **Isolate the failure**
   - Run a single FFmpeg RTSP reader (low-res) alone for 30 minutes. Does it die?
   - Add a second reader (high-res). Does either die sooner?
   - Add the third (preview). When does the first death occur?
   - Hypothesis: Seestar RTSP server has a max-client or bandwidth limit. Evidence: if failures correlate with concurrent client count.

2. **Characterize the death**
   - Instrument `_reader_loop` to log: bytes received per second, last-frame timestamp, FFmpeg process state (poll()), stderr output at death
   - Determine if FFmpeg exits (returncode != None) or hangs (blocks on stdout.read with no data)
   - Check if Seestar sends RTSP TEARDOWN or just stops sending RTP packets

3. **Test keepalive hypotheses**
   - RTSP/TCP keepalive: Does FFmpeg's `-rtsp_transport tcp` use TCP keepalive? Test with `-stimeout` and `-rtsp_flags +listen`
   - Seestar idle timeout: Does the stream die faster when there's no telescope activity vs. active tracking?
   - Network-level: Is the WiFi connection to Seestar dropping? (Check `ping` statistics over 30 minutes)

4. **Design the fix** (one or more of):
   - **Single multiplexed reader**: One FFmpeg process captures full-res; Python demuxes to detector (downscaled) + pre-buffer + preview. Eliminates multi-client issue.
   - **Supervised reader with health check**: Watchdog thread monitors bytes/second; proactive kill+restart before silent stall propagates.
   - **RTSP re-probe on reconnect**: After reconnect, verify stream is actually delivering frames before marking "connected."

### Design/Decision Outputs
- Root cause determination (max-clients, idle timeout, network, or firmware bug)
- Chosen fix strategy with rationale
- Reconnection protocol specification

### Tests and Success Criteria
- [ ] Identify exact failure mode (FFmpeg exit code, stderr, or hang characterization)
- [ ] Determine if concurrent client count is the trigger (single vs. multi-client test)
- [ ] Chosen fix keeps stream alive for 30+ continuous minutes
- [ ] After an intentional disruption (disconnect WiFi for 10s), stream recovers within 30s
- [ ] Preview, detector, and pre-buffer all receive frames simultaneously for 30+ minutes

### Checkpoint
**Approve the fix design before any code changes.** The design document must specify:
- What changes to which files
- How reconnection works
- How to verify the fix

### Testing Plan (post-fix)
```bash
# 1. Start system in solar mode
python app.py
# Connect telescope, start solar mode via UI

# 2. Monitor for 30 minutes, logging frame counts
# (diagnostic script will be created in this phase)
python tests/diag_phase1_rtsp_stability.py --duration 1800

# 3. Intentional disruption: disconnect Seestar WiFi for 10s, reconnect
# Observe: does stream recover? How long?

# 4. Check: are all 3 consumers (detector, pre-buffer, preview) alive?
curl http://localhost:5000/telescope/preview/stream.mjpg -o /dev/null -w "%{http_code}" --max-time 5
```

---

## Phase 2: Detection Robustness

### Purpose
Validate and tune the CV transit detection pipeline against real transit videos. Ensure it can reliably detect aircraft transits while rejecting false positives.

### Scope
- Validate detector against the 3 sample transit videos
- Tune thresholds for real transit data (not grasshoppers)
- Ensure detection → recording pipeline works end-to-end
- Validate `transit_analyzer.py` post-capture analysis

### Out of Scope
- RTSP stability (fixed in Phase 1)
- Prediction pipeline
- Telescope control

### Required Inputs
- Phase 0 results (video inventory, detection config snapshot)
- Phase 1 results (confirmed stable RTSP stream)
- Transit videos: `/Users/Tom/flymoon/transits from David/transit-{1,2,3}.mp4`
- `src/transit_detector.py` (full file)
- `src/transit_analyzer.py` (full file)

### Diagnostic Steps

1. **Characterize the transit videos**
   - For each video: identify the transit frame range (start frame, end frame, duration in ms)
   - Measure: transit silhouette size in pixels, transit speed in px/frame, disk radius in pixels
   - These become the ground-truth parameters

2. **Run transit_analyzer on each video**
   - Use current default parameters
   - Record: did it detect the transit? False positives? Timing accuracy?
   - If it misses: which gate rejected it? (signal threshold? consecutive frames? centre-edge ratio? track consistency?)

3. **Signal characterization**
   - For each video, compute Signal A and Signal B values frame-by-frame through the transit
   - Plot (or log) the signal traces vs. adaptive thresholds
   - Determine: are the signals crossing thresholds? By how much margin?
   - Check centre-edge ratio during transit frames

4. **Parameter sensitivity analysis**
   - Vary `CONSEC_FRAMES_REQUIRED` (3, 5, 7, 10) and measure detection rate vs. false positive rate
   - Vary `CENTRE_EDGE_RATIO_MIN` (1.5, 2.0, 2.5, 3.0) similarly
   - Vary `SENSITIVITY_SCALE` (0.5, 1.0, 2.0, 5.0)
   - Find the parameter set that detects all 3 transits with zero false positives

5. **End-to-end recording test**
   - Feed a transit video through the detector (file-based, not RTSP)
   - Verify: detection fires, pre-buffer + post-buffer recording is created, sidecar JSON written
   - Verify: `transit_analyzer.py` post-analysis on the recording finds the transit

6. **Moon-specific considerations**
   - The transit videos — are they solar or lunar transits? (Phase 0 will tell us)
   - If solar only: note that Moon transits may need different parameters (lower contrast, different disk appearance)
   - Design a Moon detection parameter set (even if untested against real data)

### Design/Decision Outputs
- Ground-truth characterization of each transit video
- Optimal parameter set for solar transits
- Proposed parameter set for lunar transits (theoretical)
- Any algorithm changes needed (e.g., if a gate is fundamentally wrong)

### Tests and Success Criteria
- [ ] All 3 transit videos detected by `transit_analyzer.py` with correct timing (±0.5s)
- [ ] Zero false positives on all 3 videos
- [ ] Live detector (file-fed) triggers on all 3 videos
- [ ] Recording pipeline produces valid MP4 with correct pre/post buffer
- [ ] Signal margin documented: how close are real transits to the threshold?

### Checkpoint
**Approve parameter changes and any algorithm modifications before applying to production.** Document:
- Old vs. new parameter values
- Rationale for each change
- Expected impact on false positive rate

### Testing Plan (post-tuning)
```bash
# 1. Run analyzer on each transit video
python tests/diag_phase2_detection_validation.py \
  --videos "/Users/Tom/flymoon/transits from David/transit-*.mp4" \
  --output docs/diag_logs/phase2_results.json

# 2. Run file-fed detector test
python tests/diag_phase2_live_detector_test.py \
  --video "/Users/Tom/flymoon/transits from David/transit-1.mp4"

# 3. Sustained false-positive test: run detector on 10 minutes of plain solar disk
# (no transit). Must produce zero detections.
python tests/diag_phase2_false_positive_test.py --duration 600

# 4. If RTSP is stable (Phase 1 done): run live detector for 1 hour
# Record any detections. Review each manually.
```

---

## Phase 3: Prediction Accuracy

### Purpose
Validate that transit predictions are geometrically correct and that the pipeline doesn't miss transits that are within the detection window.

### Scope
- Validate coordinate transforms and angular separation math
- Test prediction against known transit events (the 3 sample videos have timestamps/locations)
- Validate OpenSky data freshness and bounding box coverage
- Ensure transit classification thresholds are appropriate

### Out of Scope
- Detection algorithm (fixed in Phase 2)
- RTSP (fixed in Phase 1)
- Telescope control

### Required Inputs
- Phase 0 results (test suite baseline)
- Phase 2 results (confirmed detection works on sample videos)
- Transit video metadata: **date, time (UTC), observer lat/lon, target (sun/moon)** for each video — needed to reconstruct the flight that caused each transit
- `src/transit.py`, `src/position.py`, `src/astro.py`, `src/opensky.py`
- `.env` production values (observer location, bbox)

### Diagnostic Steps

1. **Coordinate transform validation**
   - Run `tests/test_geographic_to_altaz.py` — does it pass?
   - Add test cases using known star positions (Polaris, Sirius) at known times from the observer's location
   - Verify `angular_separation()` against known pairs (Sun-Moon separation on a specific date)

2. **Prediction geometry end-to-end test**
   - For each sample transit video (if metadata available):
     - Look up the actual flight (FlightAware historical or OpenSky historical)
     - Feed that flight's position/speed/heading into `predict_position()` + `geographic_to_altaz()`
     - Verify the prediction shows HIGH probability at the correct time
   - If metadata not available: use synthetic flights with known geometry

3. **Bounding box coverage test**
   - Compute `transit_corridor_bbox()` for the observer's location with Sun at various azimuths
   - Verify the bbox is large enough to capture flights at typical transit altitudes (30,000–40,000 ft)
   - Check: does the bbox shrink too aggressively when the target is near the horizon?

4. **OpenSky data freshness**
   - Log timestamps of OpenSky position reports vs. current time
   - Measure typical staleness (expected: 5–15 seconds for live, up to 60 seconds for some aircraft)
   - Determine: does staleness cause missed HIGH transits? (A 15-second-old position at 900 km/h = 3.75 km offset)

5. **Rate limiting impact**
   - With current `AUTO_REFRESH_INTERVAL_MINUTES=10`, how many API calls per day?
   - Is the 400 req/day (registered) or 100 req/day (anonymous) budget sufficient?
   - How many transit-capable flights pass through the corridor per day?

6. **Threshold validation**
   - Current: HIGH ≤ 2.0°, MEDIUM ≤ 4.0°. Sun/Moon apparent diameter ≈ 0.5°.
   - A "transit" (silhouette crosses disk) requires angular separation < ~0.5° + aircraft angular size
   - Is 2.0° for HIGH too generous? Or is it appropriately conservative given prediction uncertainty?
   - Compute: what prediction error margin does 2.0° provide at typical aircraft distances?

### Design/Decision Outputs
- Validation report: coordinate transforms correct or not
- Prediction error budget: how much uncertainty at each step
- Recommended threshold adjustments (if any)
- OpenSky query strategy (frequency, bbox sizing)

### Tests and Success Criteria
- [ ] `angular_separation()` matches reference values to < 0.01°
- [ ] `geographic_to_altaz()` matches Skyfield direct computation to < 0.1°
- [ ] For at least 1 sample transit: prediction produces HIGH at correct time (±2 min)
- [ ] Bounding box covers all aircraft that could transit within 15-minute window
- [ ] OpenSky staleness characterized and acceptable (or mitigation designed)

### Checkpoint
**Approve any threshold or algorithm changes before modifying production code.**

### Testing Plan
```bash
# 1. Coordinate validation
python tests/test_geographic_to_altaz.py
python tests/test_position.py

# 2. End-to-end prediction test (synthetic)
python tests/diag_phase3_prediction_validation.py \
  --observer-lat LAT --observer-lon LON \
  --target sun --date "2025-06-15T12:00:00Z"

# 3. Bbox coverage visualization
python tests/diag_phase3_bbox_coverage.py --output docs/diag_logs/bbox_map.html

# 4. OpenSky freshness measurement (run for 1 hour during busy airspace time)
python tests/diag_phase3_opensky_freshness.py --duration 3600
```

---

## Phase 4: Alt/Az GoTo & Manual Steering

### Purpose
Fix position feedback and manual GoTo servo loop so the telescope can be reliably pointed to arbitrary alt/az coordinates.

### Scope
- Determine if `scope_get_equ_coord` updates in scenery mode
- Test `scope_get_horiz_coord` (if firmware supports it)
- Fix servo loop position feedback
- Validate `pi_set_time` and LST computation
- Test nudge (manual steering) in all modes

### Out of Scope
- RTSP (fixed in Phase 1)
- Detection (fixed in Phase 2)
- Prediction (fixed in Phase 3)

### Required Inputs
- Phase 0 results (especially `pi_set_time` status and `scope_get_equ_coord` behavior)
- `src/seestar_client.py` (full file, especially `_manual_goto_inner`, `get_telemetry`, `_altaz_from_equatorial_for_goto`)
- `docs/telescope-slew-goto-debugging.md`
- Hardware: Seestar S50 powered on, in a position where you can observe physical movement

### Diagnostic Steps

1. **`pi_set_time` validation**
   - Send `pi_set_time` with current UTC; log the response
   - Query the scope's time (if possible) and compare to system UTC
   - If time sync fails: this corrupts all RA/Dec → alt/az conversions

2. **Position feedback in scenery mode**
   - Enter scenery mode
   - Nudge the telescope (known direction, known duration)
   - Immediately query `scope_get_equ_coord` — did RA/Dec change?
   - If not: RA/Dec is stale in scenery mode (confirmed hypothesis)
   - Test: `scope_get_horiz_coord` — does this command exist? Does it return current alt/az directly?

3. **Event message position data**
   - Log all unsolicited Event messages during a nudge
   - Check if any event contains position updates (some Seestar firmware versions broadcast position)
   - If yes: parse and use as position feedback

4. **Servo loop validation (if position feedback available)**
   - Command GoTo to a known position (e.g., alt=45, az=180)
   - Log each iteration: current position, target, delta, speed, angle, firmware angle
   - Verify: angle computation drives toward target (not away)
   - Verify: stall detection works (aim at a mechanical limit)

5. **Angle convention audit**
   - Nudge up → does telescope physically move up?
   - Nudge right → does telescope physically move right?
   - Test all 4 directions
   - Log: API angle → firmware angle → physical direction

6. **Mode transition safety**
   - From solar tracking: nudge → does it stop tracking and switch to scenery?
   - From scenery: start solar mode → does it resume tracking?
   - Rapid mode switches: sun → nudge → sun → nudge (race conditions?)

### Design/Decision Outputs
- Position feedback strategy: which source(s) to use in scenery mode
- `pi_set_time` fix (if needed)
- Servo loop corrections (if angle math or feedback is wrong)
- Mode transition protocol

### Tests and Success Criteria
- [ ] `pi_set_time` succeeds and scope clock matches UTC (±2 seconds)
- [ ] Position feedback updates after physical movement in scenery mode
- [ ] GoTo to alt=45, az=180 arrives within 0.5° (tolerance) in < 60 seconds
- [ ] All 4 nudge directions match physical movement
- [ ] Mode transitions (sun → scenery → sun) work without errors
- [ ] GoTo from sun mode → scenery → target → back to sun mode works

### Checkpoint
**Approve servo loop and feedback changes before modifying `seestar_client.py`.**

### Testing Plan
```bash
# 1. Connection and time sync diagnostic
python tests/diag_phase4_time_sync.py

# 2. Position feedback test (requires physical observation)
python tests/diag_phase4_position_feedback.py --mode scenery --nudge-direction up --duration 3

# 3. GoTo test (requires physical observation)
python tests/diag_phase4_goto_test.py --target-alt 45 --target-az 180

# 4. Nudge direction test (all 4 directions)
python tests/diag_phase4_nudge_directions.py

# 5. Mode transition stress test
python tests/diag_phase4_mode_transitions.py --cycles 5
```

---

## Phase 5: Integration & End-to-End Validation

### Purpose
Validate that all subsystems work together: prediction triggers telescope pointing and recording, RTSP stays stable, detection catches transits during automated operation.

### Scope
- End-to-end test: synthetic flight → prediction → telescope response → recording → detection
- Sustained operation test (2+ hours)
- Failure recovery test (network drop, mode conflict)

### Out of Scope
- Individual subsystem fixes (all should be resolved by now)

### Required Inputs
- Phase 1–4 results (all subsystems individually validated)
- `app.py`, `transit_capture.py`
- Hardware: Seestar in solar mode, pointed at Sun
- Synthetic flight data generator: `data/test_data_generator.py`

### Diagnostic Steps

1. **Synthetic transit injection**
   - Generate a synthetic flight that will produce a HIGH transit in ~5 minutes
   - Verify: prediction picks it up, UI shows it, monitor logs it
   - Verify: if `transit_capture.py` is running, it schedules a recording

2. **Recording + detection chain**
   - While Seestar streams the Sun, inject a synthetic HIGH transit
   - Verify: recording starts (pre-buffer), runs for expected duration, stops
   - Run `transit_analyzer.py` on the recording — does it find anything? (No real aircraft, so it should find nothing, confirming no false positive from the trigger itself)

3. **RTSP sustained operation**
   - Run the full system (app.py + detector + preview + timelapse) for 2+ hours
   - Log frame counts per minute for all consumers
   - Verify: no silent stream deaths, or if any, recovery is automatic and < 30s

4. **Failure injection**
   - Disconnect Seestar WiFi for 15 seconds → reconnect
   - Verify: JSON-RPC reconnects, RTSP recovers, detection resumes, no crash
   - Switch mode rapidly (sun → scenery → moon → scenery → sun)
   - Verify: no state corruption, telemetry remains accurate

5. **Real-world soak test**
   - Run the full system for a full solar observation session (sunrise to sunset, or a 4-hour subset)
   - Record: number of predictions checked, any HIGH/MEDIUM alerts, any detections, any RTSP drops, any errors
   - This is the final acceptance test

### Tests and Success Criteria
- [ ] Synthetic HIGH transit detected by prediction pipeline within 1 refresh cycle
- [ ] Recording triggered by prediction (if transit_capture.py in automatic mode)
- [ ] RTSP stable for 2+ continuous hours (all 3 consumers)
- [ ] Recovery from WiFi disconnect < 30 seconds
- [ ] No unhandled exceptions in 4-hour soak test
- [ ] All subsystem metrics logged and reviewable

### Checkpoint
**This phase produces the final "system ready" determination.** If any test fails, loop back to the relevant phase.

### Testing Plan
```bash
# 1. Synthetic transit end-to-end
python tests/diag_phase5_synthetic_transit.py \
  --observer-lat LAT --observer-lon LON \
  --target sun --transit-in-minutes 5

# 2. Sustained operation (2 hours)
python tests/diag_phase5_soak_test.py --duration 7200

# 3. Failure injection
python tests/diag_phase5_failure_injection.py

# 4. Full session (manual — run app.py, observe for 4 hours)
python app.py
# Monitor logs: tail -f flymoon.log
```

---

## Phase Dependencies

```
Phase 0 (Baseline)
    ├──→ Phase 1 (RTSP Stability)
    │        └──→ Phase 2 (Detection Robustness)
    │                 └──→ Phase 3 (Prediction Accuracy)
    │                          └──→ Phase 4 (Alt/Az GoTo)
    │                                   └──→ Phase 5 (Integration)
    │
    └── Phase 2 can partially run without Phase 1
        (file-based detection tests don't need RTSP)
```

**Parallelization note:** Phase 2 (detection) file-based tests and Phase 3 (prediction) are independent of Phase 1 (RTSP). If you want to move faster, you can run Phase 2's file-based validation and Phase 3 in parallel with Phase 1, then do Phase 2's live tests after Phase 1 completes.

---

## Phase Results

_(Append results here as each phase completes)_

### Phase 0 Results
```
[To be filled after Phase 0 execution]
```

### Phase 1 Results
```
[To be filled after Phase 1 execution]
```

### Phase 2 Results
```
[To be filled after Phase 2 execution]
```

### Phase 3 Results
```
[To be filled after Phase 3 execution]
```

### Phase 4 Results
```
[To be filled after Phase 4 execution]
```

### Phase 5 Results
```
[To be filled after Phase 5 execution]
```
