# Detection Assessment - Copilot - 2026-04-15

Date: 2026-04-15  
Scope: Design-level false-positive reduction strategy (implementation-agnostic)

## Objective
Reduce false positives without sacrificing capture opportunity for true aircraft transits.

## Design Recommendations

### 1) Use a staged decision architecture
1. Stage A: High-recall candidate generation (propose potential events).
2. Stage B: Strong verification (trajectory, morphology, and context checks).
3. Stage C: Confidence calibration and action policy (alert, record-only, or reject).

Rationale: Most false positives are eliminated when weak single-signal candidates cannot directly become positives.

### 2) Incorporate prediction-conditioned priors
1. Keep normal sensitivity inside predicted transit windows.
2. Outside predicted windows, require stronger evidence.
3. Treat no-nearby-prediction as a negative prior, not an absolute veto.

Rationale: Transit geometry is predictable; using that prior materially reduces nuisance detections.

### 3) Elevate physical plausibility to a primary gate
Require candidate events to satisfy physically plausible transit behavior:
1. Coherent path over the disc.
2. Plausible angular velocity range.
3. Plausible duration envelope.
4. Consistent silhouette/motion evolution over time.

Rationale: Atmospheric jitter, edge effects, and random artifacts generally fail these constraints.

### 4) Use multi-class decisioning instead of binary transit/non-transit
Recommended classes:
1. Aircraft transit.
2. Bird/insect near-field crossing.
3. Atmospheric or limb artifact.
4. Unknown.

Policy:
- Only class 1 is positive for operator alerts.
- Unknown can still be retained for review/training without polluting positive alerts.

### 5) Separate capture policy from truth policy
1. Keep recording policy permissive (to preserve recall).
2. Keep alert/truth policy strict (to preserve precision).

Rationale: A missed transit is unrecoverable, while an extra recording is recoverable.

### 6) Build a hard-negative learning loop into system design
1. Continuously collect local hard negatives (insects, birds, cloud edges, scintillation).
2. Keep temporally segmented validation sets (different days, seeing conditions, seasons).
3. Track drift and retune decision boundaries on schedule.

Rationale: False-positive reduction depends on representing real nuisance modes in evaluation.

### 7) Optimize explicitly for precision under constraints
1. Set a precision target first.
2. Maximize recall subject to the precision floor.
3. Monitor operating point drift over time.

Rationale: Systems without an explicit precision objective drift toward over-triggering.

## Proposed Decision Policy (Design-Level)
1. Candidate detected.
2. Apply physical plausibility score.
3. Fuse with prediction prior.
4. Produce multi-class outcome with calibrated confidence.
5. Route outcome:
   - Aircraft transit + high confidence: alert + record.
   - Aircraft transit + medium confidence: record + low-priority review.
   - Non-aircraft class: suppress alert, optional record.
   - Unknown: record for review/labelling.

## Expected Outcome
If applied as a coherent design (not isolated threshold tweaks), this architecture should reduce operational false positives substantially while preserving true transit capture probability.

---
Prepared by Copilot on 2026-04-15.
