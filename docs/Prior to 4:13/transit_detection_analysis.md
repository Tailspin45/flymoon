# Transit Live Detection — Analysis, Bugs & Fixes

## Pipeline Overview

The live detection pipeline operates as two parallel threads:

1. **Detection thread** (`_reader_loop`): ffmpeg reads the RTSP stream and pipes raw RGB frames at 160×90 px / 15 fps into `_process_frame`. This resolution is deliberately low for CPU efficiency; a real aircraft transit is visible as a multi-pixel dark streak even at this scale.

2. **Pre-buffer thread** (`_hires_reader_loop`): a separate ffmpeg instance captures full-resolution MJPEG frames at ~30 fps into a rolling circular buffer (≈150 frames / 5 seconds). When a detection fires, this buffer provides the pre-trigger footage for the saved MP4. Frames are **stabilized in memory** before encoding (see Recording Stabilization below).

### Per-frame logic (`_process_frame`)

Every frame goes through:

1. **Disk detection** (every 30 frames / 2 s): Hough circle detection on the grayscale frame. On success, three masks are built: `_disk_mask` (inner disk, excluding limb margin), `_limb_mask` (excluded limb ring), `_disk_weight` (smooth float weight). Falls back to rectangular centre/edge masks if the disk is not found.

2. **Signal A** — mean-subtracted absolute diff between the current frame and the immediately preceding frame. Measures instantaneous pixel motion within the inner disk. Sensitive to fast-moving objects (aircraft) but also to atmospheric scintillation.

3. **Signal B** — mean-subtracted weighted diff between the current frame and a slowly-updating EMA reference (α=0.02, ~50-frame half-life). Measures sustained deviation from the scene baseline. More robust to single-frame noise.

4. **Centre ratio** — ratio of inner-disk mean diff to limb-ring mean diff (both from the Signal B diff map). A compact dark silhouette crossing the bright interior produces high inner / low limb → ratio >> 1. Disk-wide atmospheric shimmer produces high inner / high limb → ratio ≈ 1.

5. **Adaptive threshold** — for each signal: `median(history) + max(3×MAD, 0.5×median)` over a 20-second rolling window (~300 frames). Automatically adjusts to the current scene noise level.

6. **Consecutive-frame gate** — both signals must exceed their thresholds AND `centre_ratio ≥ centre_ratio_min` for `consec_frames_required` consecutive frames before a detection fires.

7. **On detection**: reference frame is frozen for 5 seconds (prevents the transit from corrupting the baseline), diagnostic frames are saved, the pre-buffer is assembled into an MP4, and flight enrichment is attempted.

---

## Root Causes of Original Failures

### Bug 1 — Consecutive counter not reset after firing (`==` instead of `>=`)

**Location:** `_process_frame`, lines ~818–824 (original).

```python
if self._consec_above == self.consec_frames_required:   # BUG: == not >=
    ...fire_detection(...)
    # counter NOT reset here
```

`_consec_above` was never zeroed after a detection. On the next frame, if `triggered` was still true (transit still in progress), the counter incremented past N and the `== N` check was never true again — so no second detection during the same transit (benign). But on the *next* transit, if the counter started from some leftover value, the first `>= N` crossing happened at a different offset, causing unpredictable timing. Additionally, a user who changed `consec_frames_required` via the slider while a transit was in progress could end up in a state where the counter was already above the new N, preventing the next detection entirely.

**Fix:** Changed to `>= N` and added `self._consec_above = 0` immediately before the cooldown check, so each new event starts from a clean slate.

### Bug 2 — Centre ratio trivially passes when no disk is detected

**Location:** `_process_frame`, mask selection and ratio computation.

When `_disk_detected = False`, the code falls back to rectangular masks: `inner_mask = CENTRE_MASK` (centre 50% of frame) and `outer_mask = EDGE_MASK` (outer 50%). At 160×90 with a solar disk filling most of the frame, the "outer" region is the black corners — off-disk pixels with near-zero values. `outer_score ≈ 0.001`, so `centre_ratio = inner_score / 0.001` becomes thousands. Every frame trivially passed the ratio filter during startup or when the disk detection was momentarily failing.

**Fix:** When `_disk_detected = False`, the detection gate is now disabled entirely (`_consec_above` is reset and the function returns early). A transit cannot occur if the telescope is not pointed at the Sun or Moon, so this is safe and eliminates an entire false-positive class.

### Bug 3 — `_consec_above` survives reconnects

**Location:** `_reader_loop`, reconnect path.

When the RTSP stream dropped and ffmpeg was restarted, `_consec_above` and `_prev_frame` were never reset. If the counter had reached 5 when the stream died, the first 2 above-threshold frames after reconnect would immediately fire a detection (5 + 2 = 7 = default `consec_frames_required`). Stream interruptions therefore had a significant probability of generating a spurious detection immediately on reconnect.

**Fix:** `self._prev_frame = None` and `self._consec_above = 0` are now set immediately after each new `subprocess.Popen` call.

### Bug 4 — No noise density guard during high-activity periods

**Location:** `_process_frame`, threshold computation.

The 20-second adaptive window (`HISTORY_SIZE = 300`) is short enough that a sudden onset of high sunspot-like activity (AGC hunting, bad seeing, high solar activity) inflated scores before the threshold could catch up. During the ~20-second window fill, scores elevated above the old threshold appeared as hits, and streaks of 7 consecutive slightly-elevated frames fired as detections.

**Fix:** A 60-second background window (`_bg_scores_a`, 900 frames) tracks the long-run scene baseline. If the last 3 seconds of scores have a median more than 2× the 60-second median, both thresholds are multiplied by a proportional `noise_factor`. This raises the detection bar during noisy periods while having minimal effect during genuine transits (which produce a sharp spike well above any elevated baseline).

### Bug 5 — `sensitivity_scale` wired but never delivered

**Location:** `telescope_routes.py`, `start_detection()`, and `telescope.js`, `_loadTuning()`.

The route mapped an incoming `diff_threshold` field to `sensitivity_scale` (via `float(diff_threshold) / 5.0`), but the JS `_loadTuning()` function never included `diff_threshold` in its output. The actual payload sent on start contained only `disk_margin_pct`, `centre_ratio_min`, and `consec_frames`. So `sensitivity_scale` was always 1.0, and the threshold multiplier had no effect — the user had no runtime sensitivity control.

**Fix:**
- Removed the dead `diff_threshold` mapping from the route.
- Added `sensitivity_scale` to `_loadTuning()`, `TUNING_DEFAULTS`, `_syncTuningSliders()`, `_debouncedApplyTuning()`, and `_resetTuning()` in the JS.
- Added a "Sensitivity" slider to the Detection Tuning sidebar card (range 0.2–3.0, step 0.1; <1 = more sensitive, >1 = stricter).
- Route now extracts `sensitivity_scale` from the same settings bundle loop as the other parameters.

---

## Parameter Reference

| Parameter | Default | Env var | Slider range | Effect |
|---|---|---|---|---|
| `consec_frames_required` | 7 | `CONSEC_FRAMES_REQUIRED` | 2–20 | Minimum consecutive triggered frames before detection fires (~0.5 s at default). Higher = less false positives, more latency. |
| `centre_ratio_min` | 2.5 | `CENTRE_EDGE_RATIO_MIN` | 0.5–6.0 | Inner/outer signal ratio gate. Higher = more spatially concentrated signal required. |
| `disk_margin_pct` | 0.25 | `DETECTOR_DISK_MARGIN` | 5–50% | Limb exclusion zone. Higher = more limb jitter suppressed, smaller detection area. |
| `sensitivity_scale` | 1.0 | — | 0.2–3.0 | Threshold multiplier. <1 = lower threshold (more detections), >1 = higher threshold (fewer). |
| `DETECTION_COOLDOWN` | 30 s | `DETECTION_COOLDOWN` | — | Minimum gap between consecutive detections. |
| `HISTORY_SECONDS` | 20 s | — | — | Rolling window for adaptive threshold. |
| `BG_HISTORY_SECONDS` | 60 s | — | — | Long-run background window for noise density guard. |
| `RECORDING_STABILIZE` | true | `DETECTOR_STABILIZE` | — | Enable/disable in-memory recording stabilization. |
| `RECORDING_STABILIZE_MAX_SHIFT` | 30 px | `DETECTOR_STABILIZE_MAX_SHIFT` | — | Max translation shift accepted per frame. |
| `RECORDING_STABILIZE_SMOOTHING` | 0.7 | `DETECTOR_STABILIZE_SMOOTHING` | — | EMA smoothing for cumulative offset. |
| `track_min_mag` | 2.0 px | `DETECTOR_TRACK_MIN_MAG` | 0–10 px | Min centroid displacement per frame to count as directional. 0 = disabled. |
| `track_min_agree_frac` | 0.6 (60%) | `DETECTOR_TRACK_MIN_AGREE` | 0–100% | Fraction of streak frames needing consistent direction before firing. 0 = track gate off. |

---

---

## Recording Stabilization

**Problem:** The saved detection MP4 was assembled from raw MJPEG frames without any stabilization. Atmospheric distortion and minor mount drift caused the solar/lunar disk to jump noticeably between frames, making the transit difficult to review and the composite image noisy.

**Solution (`_stabilize_frames`):** A phase-correlation stabilization pass runs **in memory** on the full list of JPEG frames before they are piped to ffmpeg. It is the same technique used in the solar timelapse pipeline.

### How it works

1. The first `ref_count` frames (default 15, drawn from the quiet pre-trigger pre-buffer) are decoded and averaged into a mean reference image. This reference represents "where the disk should be."
2. Each frame is phase-correlated against that reference to compute a sub-pixel translation `(dx, dy)`.
3. Shifts larger than `max_shift` pixels are clamped (default 30 px at full res) — this allows genuine slow mount drift to be followed while preventing a single bad frame from causing a large jump.
4. The offset is EMA-smoothed (default α=0.7) so single-frame outliers (e.g. a seeing spike during the transit itself) don't snap the image.
5. `cv2.warpAffine` applies the translation with `BORDER_REPLICATE` fill (no black borders).
6. The corrected frame is re-encoded as JPEG (quality 92) before piping to ffmpeg.

If any frame fails to decode or the phase correlation returns a low-confidence result (response < 0.02), the original frame is passed through unchanged — the recording is never lost due to a stabilization failure.

### Configuration

| Env var | Default | Effect |
|---|---|---|
| `DETECTOR_STABILIZE` | `true` | Set to `false` to disable entirely (pass raw frames) |
| `DETECTOR_STABILIZE_MAX_SHIFT` | `30` | Maximum accepted shift in pixels. Atmospheric distortion is typically <8 px; larger values catch mount slip. |
| `DETECTOR_STABILIZE_SMOOTHING` | `0.7` | EMA smoothing factor for the cumulative offset. Higher = more responsive to drift, less forgiving of spikes. |

### Notes

- The stabilization reference is the pre-trigger period — intentionally chosen because the Sun/Moon is in the correct position before the aircraft crosses it.
- Atmospheric distortion (seeing) will still cause small shape changes on the disk limb; stabilization corrects translation only, not rotation or scale. This is appropriate: real atmospheric distortion is sub-arcsecond shape wobble, not a coordinate shift.
- The CPU cost is roughly one JPEG decode + one `phaseCorrelate` + one `warpAffine` + one JPEG encode per frame, at ~30 fps × 10 s = ~300 frames. On a modern CPU this takes <1 second and runs in the background recording thread before the ffmpeg encode.

---

## Centroid Track Consistency (implemented)

Implemented as a third gate in `_process_frame`, after the threshold and centre-ratio checks.

Each frame, the intensity-weighted centroid of `abs_diff_gray` within the inner disk mask is computed at detection resolution (160×90). Over the course of a consecutive streak, displacement vectors between successive centroids are tested: if the dot product with the previous displacement is positive, the frame "agrees" directionally. When the streak reaches `consec_frames_required`, the fraction of agreeing frames is compared against `track_min_agree_frac` (default 60%). If the fraction is too low, the detection is suppressed and the counter resets.

Frames where centroid displacement is below `track_min_mag` (default 2px at 160×90) are neither counted for nor against agreement — this handles large, slow transits near the disk centre where per-frame motion is sub-threshold.

The agreement count is not reset on a single negative dot-product, so a brief seeing spike mid-transit does not break an otherwise clean track.

Setting `track_min_agree_frac` to 0.0 disables the gate entirely, reverting to legacy threshold-only behaviour. Both parameters are live-tunable from the Detection Tuning sidebar and via `PATCH /telescope/detect/settings`.
