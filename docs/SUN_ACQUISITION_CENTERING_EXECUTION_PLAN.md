# Sun Acquisition and Centering Execution Plan

## Objective
Implement a production safe Sun acquisition and centering routine for Zipcatcher using existing ALPACA mount control and detector disc telemetry, with optional intensity based enhancements.

## Scope
In scope:
1. Automatic coarse point, search, center, and maintain logic.
2. API endpoints and UI controls for operator start/stop/status.
3. Logging, safety limits, and validation tests.

Out of scope for first delivery:
1. New detector backbone.
2. Replacing current telescope control architecture.
3. Hardware specific firmware reverse engineering.

## Architecture Summary
1. New Python service layer handles centering state machine.
2. Service consumes:
   - astronomy target alt/az
   - ALPACA telemetry and movement APIs
   - detector disk_info and disc_lost signals
3. JavaScript UI controls and telemetry panel expose centering state.
4. Electron shell remains unchanged and serves the existing Flask app.

## Phase Plan

### Phase 0 - Design Lock and Interface Contract (0.5 day)
Deliverables:
1. Final state machine diagram and transitions.
2. API contract for start/stop/status and tuning parameters.
3. Safety limits table.

Exit criteria:
1. Contract reviewed and frozen.

### Phase 1 - Data Path Validation (0.5 to 1 day)
Tasks:
1. Verify ALPACA telemetry freshness and update intervals.
2. Verify detector status cadence and disk_info reliability.
3. Verify control latency from nudge command to telemetry response.

Deliverables:
1. Diagnostic report with measured latencies and failure modes.

Exit criteria:
1. Confirmed minimum viable feedback rate for closed loop control.

### Phase 2 - Python Service Skeleton (1 day)
Tasks:
1. Add service module for centering state machine.
2. Add route handlers:
   - POST start
   - POST stop
   - GET status
   - PATCH settings
3. Add structured logs and error codes.

Deliverables:
1. Runnable service with PRECHECK and FAIL_SAFE paths.

Exit criteria:
1. API endpoints pass smoke tests with mock clients.

### Phase 3 - Coarse Point and Acquisition Search (1 day)
Tasks:
1. Integrate astronomy target calculation.
2. Implement coarse goto.
3. Implement expanding search around target.
4. Implement acquisition scoring without requiring flux.

Deliverables:
1. Stable transition from COARSE_POINT to ACQUIRE_SEARCH to lock state.

Exit criteria:
1. Disc lock achieved in controlled test scenarios.

### Phase 4 - Fine Center Closed Loop Controller (1.5 days)
Tasks:
1. Implement PI controller from disk center offset.
2. Add deadband, clamps, anti windup, and rate limiting.
3. Add loss handling and recovery transition.

Deliverables:
1. Stable centering loop in live tests.

Exit criteria:
1. No sustained oscillation and acceptable steady state error.

### Phase 5 - JavaScript UI Integration (1 day)
Tasks:
1. Add centering controls to telescope panel.
2. Add lock indicators, error badges, and timing stats.
3. Add safe disable behavior on disconnect.

Deliverables:
1. Operator workflow for start, monitor, stop, and recover.

Exit criteria:
1. Manual operator acceptance test passes.

### Phase 6 - Validation and Tuning (1.5 days)
Tasks:
1. Unit tests for state machine transitions.
2. Integration tests for API and control paths.
3. Field validation for morning and midday conditions.
4. Tune gains and thresholds per axis.

Deliverables:
1. Test logs and tuning profile.

Exit criteria:
1. Meets acceptance metrics below.

### Phase 7 - Documentation and Release Readiness (0.5 day)
Tasks:
1. Update telescope guide and quick reference.
2. Add troubleshooting matrix.
3. Add rollout checklist and rollback notes.

Exit criteria:
1. Docs merged and operator handoff complete.

## Acceptance Metrics
1. Median time to first lock less than 25 s after coarse slew.
2. Steady state centering error less than 0.12 disc radii.
3. Recovery after short disc loss less than 15 s.
4. No uncontrolled oscillation in 30 minute hold test.
5. Clean stop behavior with axes stopped and safe state reported.

## Risks and Mitigations
1. Disc detection instability in clouds:
   - Mitigation: hold timer, confidence gating, staged recovery.
2. Telemetry lag causing overshoot:
   - Mitigation: lower control bandwidth and stronger rate limits.
3. Axis asymmetry and backlash:
   - Mitigation: axis specific gains and minimum command pulse width.
4. ALPACA camera endpoint not available on some setups:
   - Mitigation: make intensity/imagearray features optional.

## Locked Operating Decisions
1. Start in strict tolerance; automatically fallback to conservative if flailing is detected.
2. Use infinite recovery attempts with configured rest periods between attempts.
3. Require Solar mode to start; do not force mode switches automatically.
4. Provide a manual recenter button that restarts acquisition from scratch.
