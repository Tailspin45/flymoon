# v0.3.0 Backlog

**Created at:** v0.2.0 merge  
**Purpose:** Tracks items explicitly deferred from v0.2.0. Not a commitment — each item needs scoping before it lands on a sprint.

---

## 1. telescope_routes.py except-block cleanup (audit finding 2, 9)

`src/telescope_routes.py` contains ~133 bare `except: pass` blocks. The v0.2.0 mechanical split into `src/telescope/` (§3.1) leaves them in place — cleaning them is deferred here.

**Work:**
- Audit each `except` block for a safe recovery action (log + return 500, re-raise, or swallow with a WARN).
- Replace bare `except: pass` with typed exceptions and `logger.warning(...)`.
- Add targeted unit tests for the newly-visible error paths.

**Dependency:** v0.2.0 §3.1 (split) must land first so tests can be written at the per-file level.

---

## 2. CNN retraining pipeline (audit finding 8)

The ONNX classifier (`src/transit_classifier.py`) ships with a static model checkpoint. There is no pipeline to retrain it from labelled recordings.

**Work:**
- Define a labelling schema for recording sidecar JSONs (from v0.2.0 §3.3 / Phase E).
- Build a minimal training script (`scripts/retrain_classifier.py`) that reads labelled sidecars, fine-tunes the model, and exports a new ONNX checkpoint.
- Add a smoke test that runs the script on a tiny synthetic dataset.

**Dependency:** v0.2.0 §3.3 (evidence sidecars) must ship first to have the raw data.

---

## 3. Phase D4 — auto-tuning of detection thresholds

Auto-tune the SNR gate, matched-filter hit-count thresholds, and CNN logit cutoff using accumulated labelled recordings.

**Deferred because:** needs a populated label corpus (see §2 above). Without real positives the auto-tuner will converge to a useless prior.

**Work:**
- Grid-search or Bayesian optimisation over `(snr_threshold, mf_hit_rate, cnn_cutoff)` on the labelled dataset.
- Write the result back to `src/constants.py` or an `.env` override.
- Validate with a held-out recording set before promoting.

---

## 4. Hypothesis / property-based tests

The existing unit test suite is example-based. Property-based testing (Hypothesis) would catch more edge cases in the prediction and detection pipeline.

**Priority targets:**
- `src/imm_kalman.py` — ENU ↔ geographic roundtrip, filter convergence invariants.
- `src/transit.py` — `angular_sigma` monotonicity, `get_possibility_level` boundary conditions.
- `src/flight_sources.py` — `_SourceBackoff` schedule properties.

---

## 5. GitHub Actions CI

Tests currently run manually (`make test`). A CI workflow would catch regressions on every push.

**Blocker:** the ONNX model file (`src/model.onnx` or similar) is large and may not live in the repo. Decide between:
- A self-hosted runner that has the model file on disk.
- Stubbing the classifier during CI (`MockTransitClassifier`).
- Storing the model in LFS or a CI artifact cache.

**Work (once blocker is resolved):**
- `.github/workflows/test.yml` — install deps, run `pytest tests/unit/`, upload coverage report.
- Add branch-protection rule requiring CI green before merge.

---

## 6. Additional telescope support

Seestar S50 is the only target hardware. ZWO ASI cameras, Celestron mounts, and other Alpaca-compatible devices have been requested.

**Deferred because:** the Seestar-specific JSON-RPC protocol is embedded throughout `src/seestar_client.py` and `src/telescope_routes.py`. A proper abstraction layer (interface + adapter pattern) is needed before a second device can be wired in cleanly.

**Pre-requisite:** complete the v0.2.0 §3.1 split so telescope logic is isolated before adding more implementations.

---

## 7. Detector backbone evaluation (not a commitment)

The current wavelet + matched-filter + CNN stack is adequate. A transformer-based or learned matched-filter approach has been discussed.

**This is explicitly not a v0.3.0 commitment.** Only open a spike if:
- The false-positive rate on labelled data exceeds 10 % after threshold tuning (§3 above).
- Or a pre-trained transit-detection model becomes publicly available.

---

## Status tracking

| Item | Area | Depends on | Priority |
|---|---|---|---|
| 1. except cleanup | reliability | v0.2.0 §3.1 | High |
| 2. CNN retraining | ML ops | v0.2.0 §3.3 | Medium |
| 3. Phase D4 auto-tune | detection | §2 (labels) | Medium |
| 4. Hypothesis tests | quality | — | Low |
| 5. GitHub Actions CI | infra | ONNX decision | Medium |
| 6. Additional telescopes | hardware | v0.2.0 §3.1 | Low |
| 7. Backbone evaluation | R&D | labelled data | On-hold |
