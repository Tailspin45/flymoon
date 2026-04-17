# Sun Acquisition and Centering Todo List

## Priority 0 - Must Do First
- [ ] Confirm control policy for lock loss and auto retry limits.
- [ ] Confirm centering tolerance target and hold behavior.
- [ ] Confirm UI placement for start/stop/recenter controls.

## Priority 1 - Core Backend
- [x] Create service module for centering state machine.
- [x] Add typed service state object with timestamps and reason codes.
- [x] Implement PRECHECK gate for connection, mode, and Sun altitude.
- [x] Implement COARSE_POINT using astronomy target and existing goto route.
- [x] Implement ACQUIRE_SEARCH with expanding offset pattern.
- [x] Implement FINE_CENTER with PI control, deadband, and clamps.
- [x] Implement LOCK_MONITOR drift detection and micro-correction.
- [x] Implement RECOVER workflow and max retry policy.
- [x] Implement FAIL_SAFE stop all axes and explicit error state.

## Priority 2 - API and Integration
- [x] Add POST /telescope/sun-center/start endpoint.
- [x] Add POST /telescope/sun-center/stop endpoint.
- [x] Add GET /telescope/sun-center/status endpoint.
- [x] Add PATCH /telescope/sun-center/settings endpoint.
- [x] Add structured status payload for UI polling.
- [ ] Add telemetry freshness fields to status for diagnostics.

## Priority 3 - Frontend and Operator UX
- [x] Add Sun centering panel section in telescope UI.
- [x] Add Start, Stop, and Recenter controls.
- [x] Add lock quality indicator and state badge.
- [ ] Add display of current pixel offset and normalized error.
- [ ] Add warning banner for repeated recoveries.
- [x] Add safe disable behavior when telescope disconnects.

## Priority 4 - Logging and Evidence
- [ ] Emit state transition logs with old and new states.
- [ ] Emit controller command logs with bounded sample rate.
- [ ] Save centering session summary JSON sidecar.
- [ ] Save failure snapshots when entering FAIL_SAFE.
- [ ] Add counters for acquisition attempts and recoveries.

## Priority 5 - Testing
- [x] Unit test state transition table.
- [ ] Unit test controller math with synthetic offsets.
- [x] Unit test deadband and anti windup behavior.
- [ ] Integration test API start/stop/status lifecycle.
- [ ] Integration test disc loss and recovery path.
- [ ] Field test morning low altitude conditions.
- [ ] Field test midday high contrast conditions.
- [ ] Run 30 to 60 minute hold stability test.

## Priority 6 - Optional Enhancements
- [ ] Add optional imagearray based confidence when ALPACA camera endpoint exists.
- [ ] Add optional RTSP luma confidence metric for tie breaking.
- [ ] Add adaptive gain scheduling by disc radius and seeing quality.
- [ ] Add automatic pause during severe cloud transients.

## Done Definition
A task is done only when:
1. Implementation is merged.
2. Tests are added and pass.
3. Logs and operator status are observable.
4. Relevant docs are updated.
