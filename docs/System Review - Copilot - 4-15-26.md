# System Review - Copilot - 4/15/26

Date: April 15, 2026  
Repository: zipcatcher  
Branch reviewed: v0.2.0

## Executive Summary
The current system design is viable and technically strong for automated aircraft transit prediction and capture. The architecture combines robust prediction methods, resilient multi-source ADS-B ingestion, and layered visual detection gates. The highest remaining risks are operational timing correctness, enrichment-context consistency, and maintainability of large control modules.

## Findings (Ordered by Severity)

### 1) High: Recording schedule can become stale as predictions shift
- The scheduler avoids duplicate timers by flight ID, but does not always replace an existing timer when ETA changes meaningfully.
- In practice, this can drift recording windows away from updated predicted transit center times.
- Risk: missed center-of-transit captures despite valid HIGH prediction.

Recommended enhancement:
- Make scheduling reschedulable, not only deduplicated.
- Replace an existing timer when ETA delta exceeds a configured threshold.

### 2) Medium-High: Detection enrichment does not consistently use bbox-specific snapshot selection
- Snapshot lookup capability supports precise bbox matching, but detector-side enrichment is not fully constrained to a bbox-first lookup path in all contexts.
- Risk: occasional enrichment mismatch (wrong aircraft metadata or no metadata) in mixed-bbox scenarios.

Recommended enhancement:
- Use bbox-specific snapshot retrieval first at detection-time enrichment.
- Keep explicit fallback to fresh query when exact snapshot is unavailable.

### 3) Medium: Route smoke coverage exists, but stream endpoints may make smoke runs non-deterministic
- Broad route smoke testing is present, including stream endpoint exercise.
- In this review session, the stream-heavy smoke test path was long-running and required manual stop.
- Risk: CI/runtime confidence erosion if smoke validation hangs intermittently.

Recommended enhancement:
- In smoke mode, cap streaming checks with deterministic timeout/fixture behavior.
- Keep stream route assertions shallow but bounded.

### 4) Medium: Exception-heavy control surfaces reduce diagnosability
- Control-layer modules contain many broad exception handlers.
- This protects uptime, but can obscure root causes and make field debugging slower.
- Risk: silent partial degradation and delayed operator response.

Recommended enhancement:
- Standardize exception policy by layer.
- Promote critical-path exceptions to structured warnings/errors with counters.
- Expose error-rate telemetry in health/status routes.

### 5) Low: Some audit documentation is behind current code/test state
- Earlier risk docs still reference zero-coverage conditions that are no longer true in key modules.
- Risk: planning friction and duplicated effort.

Recommended enhancement:
- Update or trim stale audit sections at each release cut.
- Keep as-built and risk docs synchronized with test evidence.

## Current System Plusses
- Strong layered detection design (disc gating, adaptive thresholds, wavelet detrending, matched-filter confirmation, track coherence, advisory CNN scoring).
- Strong prediction design (spherical separation logic + IMM Kalman uncertainty integration).
- Strong ingestion resilience (parallel multi-source fetch, per-source backoff, wall timeout, short-term caching, source-down signaling path).
- Existing prediction-to-detection bridge (pre-arming detector around predicted event windows).
- Focused reliability tests for core prediction/detection pieces pass in this environment.

## Validation Evidence During This Review
Focused non-hardware test subset was executed successfully:
- 50 passed in 14.75s

Covered areas included:
- IMM Kalman
- Wavelet detrending
- Matched-filter schedule/gating
- Multi-source backoff/merge logic
- Transit classifier wrapper
- sep_1sigma helper behavior
- OpenSky snapshot behavior
- TransitRecorder scheduling/timing

## Viability Assessment
Overall viability is good. The system is beyond prototype quality in core algorithm design and operational fallback behavior. The next highest-value work is not a redesign; it is operational correctness hardening, deterministic validation behavior, and maintainability improvements in high-complexity control modules.

## Clarifying Questions (for prioritization)
1. Is your top priority minimizing missed true transits, or minimizing nuisance recordings?
2. Is deployment single-site only, or do you regularly run multiple location/bbox contexts?
3. How much ETA drift is typically observed in the final 5 minutes before predicted HIGH events?
4. Is enrichment correctness mission-critical, or mainly convenience for log review?

## Actionable Recommendations

### P0 (Immediate)
1. Add reschedulable recording timers with ETA-delta replacement logic.
2. Make enrichment snapshot selection bbox-first with explicit fallback path.

### P1 (Near-Term)
3. Make telescope route smoke tests deterministic for stream endpoints.
4. Add unified reliability telemetry: gate-reason counters, schedule-adjust counters, source-down duration, enrichment provenance.

### P2 (Structural)
5. Rationalize exception handling policy in control modules and expose error budgets in status views.
6. Keep audit/docs in sync with current tests and code behavior at each release checkpoint.

---
Prepared by Copilot system review workflow, April 15, 2026.
