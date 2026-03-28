# Flymoon Master Phased Plan (Diagnostics-First, No Production Code)

## 1) System overview and primary problem domains

Flymoon combines:
- Python backend for flight ingestion, celestial prediction, transit scoring, telescope control, and CV detection.
- JavaScript frontend for map/radar/scope UI and operator workflows.
- Seestar-like control paths (legacy + Alpaca-related additions) and RTSP live video.

Primary domains to address in this roadmap:
1. Telescope control reliability (alt/az GoTo + manual steering, mode conflicts, Alpaca integration cleanup).
2. RTSP stability and resilience.
3. Prediction/UX consistency (upcoming transits rendering location).
4. Scope radar tracking quality (smooth motion, dead reckoning, sweep-synchronized updates).
5. Scope UI cleanup/polish items (status line removal, button placement, screw-head ornaments, control sizing/alignment, filmstrip behavior).
6. Cross-page consistency (top-right controls, remove near-misses button on map).

Guiding rule for all phases: **evidence before edits** and **no production code until phase checkpoint approval**.

---

## 2) Phase map

- Phase 0: Inventory, observability baseline, and evidence harness
- Phase 1: Telescope control + mode state-machine diagnostics
- Phase 2: RTSP connection stability diagnostics
- Phase 3: Prediction/radar data-flow and motion-model diagnostics
- Phase 4: UI layout and interaction remediation design
- Phase 5: Controlled implementation waves + regression validation
- Phase 6: End-to-end acceptance, ops handoff, and stabilization backlog

---

## 3) Detailed phase plan

### Phase 0 — Inventory and evidence harness

#### Purpose and scope
- Build a shared factual baseline of architecture, active control paths, and current failures.
- Define repeatable evidence capture so later phases can be executed in isolated fresh chats.
- In scope: mapping files/modules, runtime paths, and current behavior capture.
- Out of scope: feature changes or refactors.

#### Required inputs from you (future chat kickoff)
- Current branch and git status summary.
- Whether Seestar hardware and RTSP source are available for this session.
- `.env` non-secret relevant keys/values (redacted secrets).
- Any known failing scenarios with timestamps.

#### Diagnostic steps
- Identify active runtime paths for:
  - GoTo/manual steering commands and mode transitions.
  - RTSP open/reconnect lifecycle.
  - Transit prediction -> map/radar/upcoming-transits rendering chain.
  - Radar track update and sweep timing.
  - Scope/map UI component hierarchy and style ownership.
- Build a failure catalog template:
  - symptom, trigger, reproducibility, expected vs actual, logs/video/frame captures.
- Confirm what existing tests/checks are runnable now (lint + available tests + manual smoke scripts).

#### Design/decision outputs
- Canonical artifact list per subsystem.
- Evidence collection standard (log format, naming, folder structure).
- Prioritized risk matrix: control reliability > RTSP > tracking > UI polish.

#### Testing plan
- Commands:
  - `make lint`
  - targeted existing tests relevant to touched subsystems
  - manual smoke: launch app, map page, scope page, telescope route health
- Observe/log:
  - startup logs, control command logs, RTSP connect/disconnect traces, UI console errors.
- Scenario variety:
  - baseline idle, active tracking, manual slew, mode switch, stream restart.

#### Success criteria / complete gate
- Reproducible evidence for each major problem domain exists.
- File/module ownership map is documented.
- Phase 1 investigation inputs are complete.

#### No-code checkpoint
- **Stop and request approval before any production code changes.**

#### Fresh-chat bootstrap package
- Paste:
  - “Executing Phase 0 of Flymoon master plan”
  - short status of hardware availability
- Attach:
  - key logs, screenshots/video clips, config excerpt (non-secret), error traces.
- Artifacts naming:
  - `phase0_<subsystem>_<scenario>_<YYYYMMDD-HHMM>.log|txt|png|mp4`

---

### Phase 1 — Telescope control and mode-state diagnostics

#### Purpose and scope
- Diagnose unreliable/non-functional GoTo and manual steering.
- Verify Alpaca additions against existing Seestar-style control flow.
- Determine code to remove/retain where Alpaca supersedes legacy paths.

#### Required inputs from you
- Phase 0 artifacts for control issues.
- Hardware model/firmware info and connection method.
- Reproduction script (operator steps) for at least 3 failure variants.

#### Diagnostic steps
- Trace control command path end-to-end:
  - UI action -> API route -> control client -> device response -> UI telemetry feedback.
- Build explicit state diagram for mount/control modes:
  - tracking, slewing, manual, scenery/solar/lunar constraints, lockouts.
- Check for conflicting loops:
  - periodic tracker vs manual command stream vs GoTo orchestration.
- Identify coordinate frame conversions and verify alt/az conventions and update cadence.
- Compare “source of truth” for mode and position across backend/frontend.
- Identify dead/duplicate code introduced around Alpaca integration.

#### Evidence needed to confirm/rule out suspected causes
- Mode conflict hypothesis:
  - need timestamped logs showing concurrent command emitters and mode flips.
- Coordinate mismatch hypothesis:
  - need before/after command coordinates + telemetry coordinates in same frame.
- Stale state hypothesis:
  - need UI state snapshots and backend state at matching timestamps.
- Alpaca flow confusion hypothesis:
  - need route-to-client call graph showing duplicate or bypassed paths.

#### Design/decision outputs
- Approved control state machine (single writer rules, lock semantics, precedence).
- Canonical command arbitration strategy.
- Keep/remove matrix for legacy vs Alpaca code paths.
- Validation checklist for manual and GoTo control.

#### Testing plan
- Commands:
  - app run + targeted control route checks
  - any existing telescope-related tests/scripts
- Observe/log:
  - command queue timing, mode transitions, telemetry lag.
- Scenario variety:
  - idle -> manual -> stop -> GoTo -> tracking resume.
  - rapid mode toggles.
  - disconnect/reconnect during command activity.
- Minimum runs:
  - at least 5 successful repeated control cycles without mode desync.

#### Success criteria / complete gate
- Root cause(s) for control unreliability are evidenced and ranked.
- Change design is explicit, minimal, and approved.

#### No-code checkpoint
- **No implementation until state-machine and cleanup design are approved.**

#### Fresh-chat bootstrap package
- Paste:
  - “Executing Phase 1, using approved Phase 0 evidence”
- Attach:
  - control logs, telemetry traces, call-path notes, mode diagram draft.

---
Phase 1 — Final Summary (for Phase 2 handoff)
Context
ZWO encrypted a significant portion of JSON-RPC in a firmware update. ALPACA (port 32323) is their official replacement for all motor control. JSON-RPC survives only for: viewing modes, recording, heartbeat, focus, camera settings, and events.

Approved decisions
Question	Decision
ALPACA working on hardware?	Take a detour — Phase 1a confirms ALPACA is reachable and functional before Phase 2 touches production code
JSON-RPC speed_move fallback	Remove — encrypted firmware means it silently fails anyway; keep fallback is misleading
GoTo while nudging	Abort nudge first — stop MoveAxis (rate=0 on both axes) then proceed with slew
resume_tracking sleep(3)	Replace with alpaca.is_slewing() == False poll
Confirmed bugs (ranked by severity)
#	Bug	Location	Impact
1	SeestarClient.get_telemetry() undefined	seestar_client.py:1944	Mode state never reconciled from live device — silent AttributeError on every status call
2	JSON-RPC GoTo calls iscope_start_view(star)	seestar_client.py:1256	Wrong firmware command; sets mode=star, blocks recording until 3s resume fires; catastrophic if ALPACA unavailable
3	No serialisation: GoTo vs nudge	telescope_routes.py:621, 703	Concurrent ALPACA slew + MoveAxis → erratic firmware behaviour
4	JS nudge setInterval resends in ALPACA mode	telescope.js:1459	Unnecessary re-sends; stop command races with last interval fire
5	_alpacaConnected starts false, ~2.5s delay	telescope.js:6794	Brief window after connect where frontend sends JSON-RPC nudge payload to an ALPACA-only backend
Keep/Remove matrix (approved)
Code	Action
AlpacaClient — GoTo, MoveAxis, tracking, park	Keep as sole motor path
SeestarClient.goto_radec/altaz	Remove from routes (dead letter post-encryption)
SeestarClient.speed_move / speed_stop / manual_goto	Remove
SeestarClient.start_solar/lunar/scenery_mode	Keep — mode management is still JSON-RPC
SeestarClient.start_recording/stop_recording	Keep — no ALPACA equivalent
SeestarClient.get_status() broken telemetry call	Fix (remove/replace)
Duplicate altaz→radec in SeestarClient	Remove (keep only in AlpacaClient or extract to shared helper)
JS nudge setInterval in ALPACA mode	Replace with send-once + rate=0 on stop
Control state machine (approved design)
States: IDLE | SLEWING | NUDGING | GOTO_RESUMING
IDLE       → SLEWING       POST /telescope/goto        (abort nudge if NUDGING first)
IDLE       → NUDGING       POST /telescope/nudge       (reject if SLEWING/GOTO_RESUMING)
SLEWING    → GOTO_RESUMING alpaca.is_slewing() == False
GOTO_RESUMING → IDLE       start_solar/lunar_mode OK (JSON-RPC)
NUDGING    → IDLE          POST /telescope/nudge/stop  (moveaxis rate=0 both axes)
ANY        → IDLE          POST /telescope/stop or abort_slew


Phase 1a (revised scope) — ALPACA Confirmation + Device Telemetry Probe
Step 1–5 (motor control — unchanged from previous Phase 1a)
Same as before: reachability, discovery, connect+capabilities, MoveAxis smoke test, JSON-RPC motor rejection.

Step 6 (NEW) — Verify all polled telemetry fields
python3 - <<'EOF'
import os, json; from dotenv import load_dotenv; load_dotenv()
from src.alpaca_client import AlpacaClient
c = AlpacaClient(host=os.getenv("SEESTAR_HOST",""), port=32323)
c.connect()
c._poll_once()
print("Position:", json.dumps(c._last_position, indent=2))
print("State:", json.dumps(c._last_state, indent=2))
c.disconnect()
EOF
Expected: all 4 position fields populated (ra, dec, alt, az), tracking/slewing/parked all present.

Step 7 (NEW) — Verify JSON-RPC push events still deliver device telemetry
python3 - <<'EOF'
import os, time; from dotenv import load_dotenv; load_dotenv()
from src.seestar_client import SeestarClient
c = SeestarClient(host=os.getenv("SEESTAR_HOST",""))
c.connect()
time.sleep(10)   # wait for PiStatus, FocuserMove, ScopeTrack events
import json; print(json.dumps(c._event_device_state, indent=2))
c.disconnect()
EOF
Expected: _event_device_state populated with battery_capacity, cpu_temp, focuser_step (or at least battery fields from PiStatus).

This confirms the push event pipeline is intact and worth wiring to the status response.

Step 8 (NEW) — Confirm scope_get_horiz_coord is encrypted
python3 - <<'EOF'
import os; from dotenv import load_dotenv; load_dotenv()
from src.seestar_client import SeestarClient
c = SeestarClient(host=os.getenv("SEESTAR_HOST",""))
c.connect()
try:
    r = c._send_command("scope_get_horiz_coord", quiet=False, timeout_override=3)
    print(f"scope_get_horiz_coord: ACCEPTED → {r}")
except Exception as e:
    print(f"scope_get_horiz_coord: REJECTED — {e}")
c.disconnect()
EOF
Expected: REJECTED — confirms manual_goto is broken and must be removed.

Evidence to save
data/phase1a/phase1a_alpaca_telemetry_<timestamp>.txt   (Step 6)
data/phase1a/phase1a_push_events_<timestamp>.txt         (Step 7)
data/phase1a/phase1a_horiz_coord_<timestamp>.txt         (Step 8)
Exit criteria for Phase 2

All 5 original motor steps pass

Step 6: all position + state fields populated in ALPACA poll

Step 7:
_event_device_state
has battery data (PiStatus events flowing)

Step 8:
scope_get_horiz_coord
rejected (confirms
manual_goto
dead path)


Phase 1a Results — Hardware Confirmed
Scope: Seestar Tailspin45 S50, firmware 1.2.0-3 (Telescope V3)

Step	Result	Detail
1. ALPACA reachable	PASS	Port 32323, Value:true (scope reports already connected)
2. UDP discovery	PASS	Replies {"AlpacaPort":32323} from 192.168.4.37
3. Connect + caps	PASS	canslew, canslewasync, canmoveaxis, cansettracking, canpark all true
4. MoveAxis motor	PASS with caveat	Tracking must be disabled first. With tracking on: no motion. With tracking off at 5°/s: 7° Az confirmed. Axis max rate = 6°/s
5. scope_speed_move	DEAD	Accepted by firmware, zero motion. Silently ignored.
6. ALPACA telemetry poll	PASS	ra/dec/alt/az/tracking/slewing/parked/sidereal_time all populated
7. JSON-RPC push events	DEAD	Zero events in 30s. PiStatus, FocuserMove, ScopeTrack, RecordingStart/Stop, Client — none of them fire.
8. scope_get_horiz_coord	DEAD	Timed out. Encrypted.
Two new critical findings for Phase 2
Finding A — MoveAxis requires tracking=false

The nudge sequence must be:

PUT tracking false
PUT moveaxis {axis, rate}
On stop: PUT moveaxis {axis, 0} → then PUT tracking true if mode was sun/moon, leave false if scenery
This means nudge is no longer a fire-and-forget — it requires tracking state management. The existing tracking toggle on the ALPACA panel and the nudge handler must be coordinated.

Finding B — JSON-RPC push event system is entirely dead

All of these are now permanently empty:

_event_device_state (battery, CPU temp, charger, focuser position)
_scope_moving / _scope_tracking (ScopeTrack events)
_is_master updates (Client events)
_viewing_mode reconciliation via events (ViewStart/Stop events)
_recording state from firmware (RecordingStart/Stop events)
Consequence: The entire _handle_event infrastructure, heartbeat master-reclaim logic, and _event_device_state gap (Gap A from Phase 1) are all moot — there is no push data to expose. _viewing_mode and _recording are now 100% managed by our own method calls, never confirmed by firmware feedback.

Phase 2 keep/remove matrix — updated with hardware evidence
Code	Action	Evidence
AlpacaClient motor/position/state	Sole motor + telemetry path	Steps 3, 4, 6
scope_speed_move / manual_goto	Remove	Step 5: accepted, no motion
scope_get_horiz_coord	Remove	Step 8: timed out
_handle_event infrastructure	Gut — keep shell for future, remove all state-update branches	Step 7: zero events
_event_device_state dict	Remove — dead storage	Step 7
_scope_moving / _wait_for_scope_idle	Remove — replace with alpaca.is_slewing()	Step 7
_is_master / _reclaim_master	Remove — no Client events	Step 7
MoveAxis nudge in telescope_nudge()	Add tracking disable/restore around MoveAxis	Step 4
_viewing_mode reconciliation	Self-managed only — no firmware feedback	Step 7
_recording state	Self-managed only — no firmware feedback	Step 7
Phase 1 and 1a are complete. This is the full confirmed baseline for Phase 2 design.


### Phase 2 — RTSP stability diagnostics

#### Purpose and scope
- Diagnose frequent RTSP connection failures/drops.
- Define robust connection lifecycle/retry design with explicit failure states.

#### Required inputs from you
- Phase 0 stream logs and timestamps of drops.
- Camera/source details (codec, resolution, FPS, transport mode if known).
- Network context (wired/wifi, latency conditions, router behavior if relevant).

#### Diagnostic steps
- Trace RTSP open/read/reconnect path and timeout handling.
- Inspect buffering/threading/process boundaries for frame ingestion.
- Classify drop types:
  - initial connect fail, mid-stream timeout, decode failure, stale frame freeze.
- Correlate drop events with CPU/memory load and parallel control activity.
- Validate fallback behavior and user-facing error signaling.

#### Evidence needed to confirm/rule out causes
- Network instability hypothesis:
  - require packet/latency indicators + reconnect timeline.
- Decoder/backpressure hypothesis:
  - require frame loop timing and queue depth metrics.
- Resource contention hypothesis:
  - require correlated telemetry (CPU/load) during drops.

#### Design/decision outputs
- Connection state machine (connect, healthy, degraded, reconnecting, failed).
- Retry/backoff policy and max stale-frame threshold.
- Operator-facing status model (what UI should show and when).

#### Testing plan
- Commands:
  - existing stream diagnostics/tests if present
  - manual controlled stream start/stop/restart cycles
- Observe/log:
  - time-to-first-frame, reconnection time, freeze duration, drop frequency.
- Scenario variety:
  - normal run, forced source restart, brief network disruption, long disruption.
- Minimum runs:
  - at least 10 restart/reconnect cycles with measured outcomes.

#### Success criteria / complete gate
- Dominant drop mechanisms are evidenced.
- Approved resilience design with measurable targets.

#### No-code checkpoint
- **No production edits until reconnect/failure semantics are approved.**

#### Fresh-chat bootstrap package
- Paste:
  - “Executing Phase 2 with Phase 0 artifacts”
- Attach:
  - RTSP logs, timing table, environment notes.

---

### Phase 3 — Prediction/radar flow and motion-model diagnostics

#### Purpose and scope
- Resolve map/radar “upcoming transits” placement mismatch.
- Design smooth aircraft motion with dead reckoning and sweep-synchronized updates.

#### Required inputs from you
- Current UI behavior captures (map + scope).
- Any sample telemetry streams (live or recorded) showing erratic scope tracks.
- Expected operator behavior/spec confirmation:
  - update only on sweep pass, continue straight if no updates, fly off radar.

#### Diagnostic steps
- Trace data path:
  - backend transit prediction output -> frontend ingestion -> specific UI container rendering.
- Identify where radar track position is updated and how often.
- Compare map flight-path rendering logic vs scope track logic for reusable smoothing patterns.
- Model dead-reckoning assumptions:
  - last known heading/speed, timeout horizon, removal/off-radar rule.
- Validate sweep-pass gating mechanism and timing source.

#### Evidence needed to confirm/rule out causes
- Wrong render target hypothesis:
  - need DOM/component event path proving current insertion point.
- Jitter from unsmoothed updates hypothesis:
  - need per-frame position deltas and update timestamps.
- Sweep timing mismatch hypothesis:
  - need sweep phase timestamps vs update apply times.

#### Design/decision outputs
- Canonical motion model for scope radar:
  - hold-until-sweep, interpolate/extrapolate, decay/fly-off behavior.
- Upcoming-transits rendering contract (backend payload to frontend slot).
- Reuse decision on map path logic in scope context (with constraints).

#### Testing plan
- Commands:
  - app run + targeted debug logging flags
  - any existing transit/classification tests relevant to payload integrity
- Observe/log:
  - track smoothness metrics, jitter count, position error vs expected trajectory.
- Scenario variety:
  - frequent updates, sparse updates, no updates, abrupt heading change.
- Minimum runs:
  - at least 3 representative live runs + 3 replay/offline runs.

#### Success criteria / complete gate
- Root causes for wrong block rendering and erratic tracks are proven.
- Motion model spec and rendering fix spec are approved.

#### No-code checkpoint
- **No implementation until motion-model and rendering contracts are approved.**

#### Fresh-chat bootstrap package
- Paste:
  - “Executing Phase 3 with approved control/RTSP assumptions”
- Attach:
  - UI captures, telemetry excerpts, expected-behavior notes.

#### Phase 3 implementation status (completed)
- Completed backend/frontend contract wiring:
  - Added `generated_at_ms` to `/flights` and `/transits/recalculate` responses for ETA aging sync.
  - Updated scope radar `injectMapTransits(...)` to age ETAs from backend generation time.
- Completed telescope control reliability items:
  - Exposed `ctrl_state` from `/telescope/status` for frontend awareness.
  - Added RA/Dec GoTo completion wait thread so state remains `slewing` until motion ends.
  - Added GoTo guard while recording is active (`409`).
  - Added ALPACA nudge rate cap/clamp via `axisrates` (fallback 6.0°/s).
  - Seeded nudge tracking restore from ALPACA tracking state (reconnect drift mitigation).
- Completed radar motion hardening:
  - Reduced α-β gains (`alpha=0.10`, `beta=0.02`).
  - Added velocity bootstrap damping on sparse updates.
  - Added outlier rejection gate on residuals.
  - Added stale-track pruning (`20s`) to stop drift.
  - Gated sweep motion on fresh measurement activity.
- Validation completed:
  - `python -c "import src.telescope_routes"` passed.
  - `python3 -m py_compile src/alpaca_client.py src/telescope_routes.py app.py` passed.
  - `python3 tests/test_flask_routes.py` passed.
  - `make lint` still reports pre-existing repository baseline issues unrelated to this phase.
  - `tests/test_alpaca_motor.py` is hardware-dependent and failed in this environment due to host reachability.

---

### Phase 4 — UI layout and interaction remediation design

#### Purpose and scope
- Design and verify UI fixes for all listed polish items without introducing regressions.
- Items include:
  - remove “watching for transits” line/emoji under radar,
  - move Event History button under detection tester buttons,
  - add screw heads (live detection line + right-panel blank blocks),
  - unify focus-step button sizes,
  - align Sun/Moon target block titles/data,
  - filmstrip slide aspect-ratio correction,
  - filmstrip multi-select delete behavior preserving favorites,
  - top-right button consistency across map/scope,
  - remove near-misses button from map page.

#### Required inputs from you
- Current screenshots for map and scope at standard viewport sizes.
- Browser/device matrix used operationally.
- Confirm design preference for screw-head style (size/color/spacing).

#### Diagnostic steps
- Map UI ownership:
  - template files, JS component logic, CSS source/order/specificity.
- Build UI issue matrix:
  - element selector, current behavior, desired behavior, dependency risks.
- Define deterministic interaction specs for filmstrip selection/deletion.
- Verify responsive breakpoints and top-right control layout rules.

#### Evidence needed to confirm/rule out causes
- CSS conflict hypothesis:
  - need computed style trace for impacted elements.
- JS event handling bug hypothesis:
  - need event sequence logs for ctrl/shift selection and delete actions.
- Layout inconsistency hypothesis:
  - need side-by-side viewport comparisons and DOM structure checks.

#### Design/decision outputs
- Approved per-item UI spec table (before/after behavior).
- Priority order for UI changes (high visibility + low coupling first).
- Regression checklist for map and scope pages.

#### Testing plan
- Commands:
  - app run + browser devtools checks
- Observe/log:
  - screenshots before/after per UI item, interaction logs for filmstrip events.
- Scenario variety:
  - at least 3 viewport sizes and 2 browsers.
- Minimum runs:
  - two full UI walkthrough passes with recorded checklist.

#### Success criteria / complete gate
- Every UI item has an explicit, testable acceptance criterion.
- Interaction edge cases are covered (favorites preserved, range selection semantics).

#### No-code checkpoint
- **No UI implementation until per-item acceptance table is approved.**

#### Fresh-chat bootstrap package
- Paste:
  - “Executing Phase 4 UI remediation design”
- Attach:
  - screenshots, viewport matrix, design preferences.

---

### Phase 5 — Controlled implementation waves + regression validation

#### Purpose and scope
- Execute approved designs in small waves with strict regression controls.
- Scope is implementation only for designs approved in Phases 1–4.

#### Required inputs from you
- Approved design outputs from prior phases.
- Priority order if not all fixes can be done in one wave.

#### Implementation wave structure
- Wave A: control state-machine/command arbitration changes.
- Wave B: RTSP lifecycle resilience changes.
- Wave C: prediction/radar rendering + motion model.
- Wave D: UI layout/interaction fixes.

#### Validation steps per wave
- Run targeted regression checks tied to that wave.
- Re-run impacted smoke tests from previous waves.
- Collect before/after evidence with same scenario definitions.

#### Testing plan
- Commands:
  - existing lint/tests + wave-specific manual scenarios
- Observe/log:
  - failure count, reconnect metrics, command reliability metrics, UI checklist pass rate.
- Minimum runs:
  - repeat each critical scenario until stable across at least 3 consecutive passes.

#### Success criteria / complete gate
- No critical regression in earlier waves.
- Wave acceptance criteria all pass with evidence.

#### No-code checkpoint
- **Within Phase 5, each wave still requires explicit approval before editing starts.**

#### Fresh-chat bootstrap package
- Paste:
  - “Executing Phase 5, Wave <A|B|C|D>”
- Attach:
  - approved design section for this wave + regression checklist.

---

### Phase 6 — End-to-end acceptance and handoff

#### Purpose and scope
- Validate integrated behavior under realistic operations.
- Produce concise runbook and stabilization backlog.

#### Required inputs from you
- Availability window for live end-to-end run.
- Priority weighting (missed transits vs false positives vs UI quality).

#### Diagnostic/validation steps
- Execute complete operator flow:
  - startup -> connect -> monitor -> predicted transit handling -> scope tracking -> detection pipeline -> cleanup.
- Verify no contradictions between map, radar, upcoming transits, and control telemetry.
- Confirm graceful degradation for stream/control disruptions.

#### Testing plan
- Commands:
  - full app runtime + selected scenario scripts from earlier phases
- Observe/log:
  - integrated timeline, operator-visible errors, recovery behavior.
- Scenario variety:
  - nominal day, intermittent network, sparse telemetry, rapid manual interventions.

#### Success criteria / complete gate
- End-to-end flow meets approved acceptance criteria.
- Handoff package ready: what changed, what to monitor, known limits, next backlog.

#### No-code checkpoint
- **No new scope added during handoff unless explicitly approved as a follow-up phase.**

#### Fresh-chat bootstrap package
- Paste:
  - “Executing Phase 6 final acceptance”
- Attach:
  - phase outputs summary + final logs/screenshots/videos.

---

## 4) Cross-phase evidence and non-guessing policy

For every suspected cause:
- State hypothesis explicitly.
- Define falsifiable evidence required.
- Define instrumentation/logging needed.
- Define pass/fail test criteria before edits.

No “try and see” production changes are allowed without:
- hypothesis,
- expected effect,
- measurable confirmation conditions.

---

## 5) Standard test artifact organization (for fresh chats)

Use consistent artifact names so each future chat can be isolated:
- `artifacts/phaseN/`
- `logs/phaseN_<subsystem>_<scenario>_<timestamp>.log`
- `screens/phaseN_<page>_<viewport>_<timestamp>.png`
- `video/phaseN_<scenario>_<timestamp>.mp4`
- `notes/phaseN_findings.md` (facts only, no speculative fixes)

If small diagnostic scripts are needed (non-production):
- place under `tests/diagnostics/phaseN_<purpose>.py|js`
- include usage header and expected outputs
- keep scripts disposable and phase-scoped

---

## 6) How to use this plan in future chats

When starting a new phase chat:
1. Paste only:
   - the phase section from this plan,
   - last phase completion summary,
   - current hardware availability note.
2. Attach only the required artifacts listed for that phase.
3. Request output in this order:
   - findings,
   - confirmed/rejected hypotheses,
   - design decisions,
   - test results,
   - explicit approval checkpoint.
4. Do not proceed to coding in that phase chat until the checkpoint is approved.
5. At phase end, produce a compact “Phase Completion Record” containing:
   - objective met/not met,
   - evidence list,
   - accepted decisions,
   - open risks,
   - exact inputs for next phase.

---

## 7) Immediate next step

Start Phase 0 in a fresh chat with:
- this plan’s Phase 0 section,
- current hardware availability,
- latest logs/screenshots for control + RTSP + scope/map UI.

---

## 8) Primary target selection and execution order (locked)

To reduce risk and prevent cross-subsystem churn, execution priority is locked as:
1. **Control reliability first** (GoTo/manual steering + mode conflicts + Alpaca path cleanup).
2. **RTSP stability second** (stream lifecycle and reconnect semantics).
3. **Tracking/prediction rendering third** (upcoming transits placement + scope motion model).
4. **UI polish fourth** (layout/interaction items and visual consistency).

Rationale:
- Control and stream reliability are hard blockers for real capture sessions.
- Tracking quality depends on stable control + stable frame source.
- UI polish should follow behavioral fixes so CSS/interaction work targets final behavior.

Phase completion dependency rules:
- Do not start Phase 2 before Phase 1 design checkpoint is approved.
- Do not start Phase 3 before Phase 2 resilience design is approved.
- Do not start Phase 4 implementation decisions before Phase 3 motion/render contracts are approved.
- Phase 5 only executes approved designs from Phases 1–4.

Fresh-chat kickoff template (copy/paste):
- `Executing Phase <N> of Flymoon master plan`
- `Hardware: <available/unavailable>, RTSP source: <details>`
- `Inputs attached: <artifact list>`
- `Goal for this phase: <objective from plan>`
- `Constraint: no production code changes until approval checkpoint`

---

## 9) Phase 0 execution packet (ready to run)

### 9.1 Exact files to inspect first

Backend/control and runtime:
- `app.py`
- `src/telescope_routes.py`
- `src/seestar_client.py`
- `src/transit_monitor.py`
- `src/transit.py`
- `src/position.py`
- `src/flight_data.py`

Detection/streaming:
- `src/transit_detector.py`
- `src/transit_analyzer.py`
- any RTSP helper module referenced by detector/analyzer

Frontend/UI:
- `templates/` files for map/scope/radar pages
- `static/` JS/CSS for map, scope, radar, telescope controls, filmstrip

Configuration/docs for assumptions:
- `README.md`
- `SEESTAR_CONNECTION_IMPROVEMENTS.md`
- `.env` (redacted copy of relevant non-secrets only)

### 9.2 Phase 0 command checklist

Core quality/baseline:
- `make lint`
- run existing targeted tests available for routing, transit logic, and position logic

Runtime smoke:
- `python app.py`
- load map page and scope page
- trigger telescope status/read endpoints
- trigger one transit data refresh cycle

Data capture:
- save backend logs with timestamps
- capture browser console logs for map/scope
- take screenshots of:
  - upcoming transit placement
  - radar track behavior
  - UI elements listed in issue set

### 9.3 Artifact checklist for Phase 0 completion

Required artifacts:
- control path log sample with timestamps
- RTSP connect/drop timeline sample
- map/scope screenshots for each UI issue
- short note for each issue:
  - expected behavior
  - observed behavior
  - reproducibility frequency

Required summary table:
- `Issue`
- `Subsystem`
- `Repro steps`
- `Observed`
- `Expected`
- `Evidence files`
- `Risk (High/Med/Low)`

### 9.4 Phase 0 completion gate

Phase 0 is complete only when:
- every listed problem has at least one evidence artifact,
- at least one reproducible path exists for control and RTSP failures,
- ownership map (file/function level) exists for each subsystem,
- Phase 1 input package is assembled and named consistently.

### 9.5 Next-chat kickoff text (copy/paste)

`Executing Phase 0 of Flymoon master plan. Hardware/feed available. Attached: current logs, screenshots, and redacted config excerpt. Goal: complete inventory + evidence harness only. Constraint: no production code changes until checkpoint approval.`
