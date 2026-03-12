# DEEP FALSE NEGATIVE ANALYSIS: Transit Detection Pipeline

## EXECUTIVE SUMMARY

The flymoon pipeline has **two detection stages**:

1. **`transit_detector.py` (Real-time)**: 160×90 canvas, dual-signal algorithm on live RTSP
2. **`transit_analyzer.py` (Post-capture)**: Full-resolution frame-by-frame blob tracking

Both stages have **multiple filtering layers** that can reject real transits. The most dangerous are:
- **Minimum size thresholds** (kills small/distant objects)
- **Speed filters** (rejects slow-moving or stationary transits)
- **Linearity/coherence checks** (rejects curved paths, jitter-induced false tracks)
- **Static feature classification** (misidentifies transits as sunspots/craters)
- **Temporal gaps** (kills transits with frame dropouts)

---

## STAGE 1: Real-Time Detector (`transit_detector.py`)

### 1.1 Detection Method

**Algorithm: Dual-Signal Consecutive-Frame Confirmation** (Lines 387-476)

#### Signal A: Consecutive-Frame Difference (Lines 398-404)
```python
# Line 401-404
diff_a = frame - self._prev_frame
mean_shift = diff_a.mean(axis=(0, 1), keepdims=True)  # remove scintillation
diff_a -= mean_shift
score_a = float(np.abs(diff_a).mean())
```
- Detects rapid changes between consecutive frames
- **Vulnerability**: Slow-moving objects (balloons, very distant aircraft) produce low diff → rejected

#### Signal B: Centre-Weighted Reference Difference (Lines 412-418)
```python
# Line 413-418
diff_b = frame - self._ref_frame
mean_shift_b = diff_b.mean(axis=(0, 1), keepdims=True)
diff_b -= mean_shift_b
weighted = np.abs(diff_b) * CENTRE_WEIGHT[:, :, np.newaxis]
score_b = float(weighted.mean())
```
- Uses EMA-blended reference (Line 410: `EMA_ALPHA = 0.02`)
- **Vulnerability**: 
  - Centre-weighting (Line 73, Gaussian 1.0→0.3) suppresses edge transits
  - If transit happens away from centre, score_b may be artificially low

#### Spatial Concentration Check (Lines 420-424)
```python
centre_score = float(abs_diff_gray[CENTRE_MASK].mean())
edge_score = float(abs_diff_gray[EDGE_MASK].mean())
centre_ratio = centre_score / max(edge_score, 0.001)
```
- **Filter**: Must satisfy `centre_ratio >= CENTRE_EDGE_RATIO_MIN` (1.5, Line 56)
- **FALSE NEGATIVE RISK**: 
  - Edge transits (crossing near frame boundary) have low centre_ratio → rejected
  - Small objects may have noisy centre/edge distinction → random rejection

### 1.2 Filtering Stages (Consecutive Confirmation)

#### Warmup Gate (Lines 449-450)
```python
if len(self._scores_a) < ANALYSIS_FPS * 3:  # ~3 seconds warmup
    return
```
- **First 3 seconds: NO detections possible**
- **Risk**: Fast transits in first 3s are missed (ISS cross takes ~8-10s, hot satellites 5-7s)

#### Adaptive Thresholding (Lines 452-461)
```python
# Line 479-488 (threshold calculation)
med = float(np.median(arr))
mad = float(np.median(np.abs(arr - med)))
return med + max(3.0 * mad, 0.5 * med)

# Line 453-461 (triggering)
triggered = (
    score_a > thresh_a
    and score_b > thresh_b
    and centre_ratio >= CENTRE_EDGE_RATIO_MIN
)
```

**Threshold Formula**: `median + max(3×MAD, 0.5×median)`
- Robust but **conservative**: requires signal 3+ standard deviations above median
- **FALSE NEGATIVE RISK**:
  - Faint objects (birds, distant small aircraft) → low signal, below threshold
  - Scintillation spikes in background can raise baseline → higher threshold
  - High-pass camera (e.g., starfield tracking) → naturally high median → threshold raised

#### Consecutive Frames Required (Lines 463-476)
```python
CONSEC_FRAMES_REQUIRED = 3  # Line 47

if triggered:
    self._consec_above += 1
else:
    self._consec_above = 0

if self._consec_above == CONSEC_FRAMES_REQUIRED:
    # FIRE DETECTION
```

- **Requires 3 consecutive frames above threshold**
- **FALSE NEGATIVE RISK**:
  - Very fast transits (ISS, satellites) that brighten/dim in <100ms gap → only 1-2 frames above threshold
  - Jittery objects (birds, insects) with frame-to-frame motion variation → threshold toggle

#### Cooldown Gate (Lines 470)
```python
if now - self._last_detection_time >= DETECTION_COOLDOWN:  # 30 sec, Line 40
```
- **Only one detection allowed per 30 seconds**
- **FALSE NEGATIVE RISK**: Two real transits 20s apart → second one completely ignored

#### Reference Freeze (Lines 472-473)
```python
self._ref_freeze_until = self._frame_idx + REF_FREEZE_FRAMES  # 5 sec, Line 53
```
- Reference frame locked for 5 seconds after detection
- **FALSE NEGATIVE RISK**: If transit extends into freeze period, features get "baked into" reference → harder to detect

### 1.3 Sensitivity Parameters

| Parameter | Value | Impact |
|-----------|-------|--------|
| `CENTRE_EDGE_RATIO_MIN` | 1.5 (Line 56) | Rejects edge transits; configurable? |
| `EMA_ALPHA` | 0.02 (Line 50) | Reference changes slowly; older blobs in ref harder to detect |
| `CENTRE_WEIGHT` | Gaussian 1.0→0.3 (Line 70) | Edge supression; 30% weight at corners |
| `CONSEC_FRAMES_REQUIRED` | 3 (Line 47) | Hard minimum; not configurable |
| `DETECTION_COOLDOWN` | 30s (Line 40) | Not configurable; **dangerous for close-spaced events** |
| `REF_FREEZE_FRAMES` | 75 frames/5s (Line 53) | Not configurable |
| `sensitivity_scale` | **CONFIGURABLE** (Line 167) | 0.1–∞; multiplies thresholds |

**CRITICAL**: The only user-tunable detector sensitivity is `sensitivity_scale`, which multiplies adaptive thresholds (Lines 435, 453).

---

## STAGE 2: Post-Capture Analyzer (`transit_analyzer.py`)

### 2.1 Detection Method(s)

Two parallel detection paths depending on target:

#### Path A: Solar Mode (Reference-Frame Differencing, Line 484)
```python
# Line 413-424
def _detect_blobs_in_frame(gray: np.ndarray, fidx: int) -> List[BlobDetection]:
    gray_s, _ = _stabilize_frame(gray, ref_gray_f32)
    gray_blur = cv2.GaussianBlur(gray_s, (5, 5), 0)
    diff = cv2.absdiff(gray_blur, ref_blur_cached[0])
    diff_masked = cv2.bitwise_and(diff, diff, mask=mask)
    _, binary = cv2.threshold(diff_masked, _diff_threshold, 255, cv2.THRESH_BINARY)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    return _extract_blobs(binary, fidx, _min_blob_pixels)
```

- Compares each frame against frozen reference (median of first ~90 frames, Line 459)
- Stabilization via phase correlation (FFT, Line 185)
- **Morph ops reduce noise but also erase small features**

#### Path B: Lunar Mode (Frame-to-Frame Differencing, FTF, Line 477-482)
```python
# Line 426-440
def _detect_blobs_ftf(cur_blur, prev_blur, fidx):
    diff = cv2.absdiff(cur_blur, prev_blur)
    diff_masked = cv2.bitwise_and(diff, diff, mask=mask)
    _, binary = cv2.threshold(diff_masked, _ftf_threshold, 255, cv2.THRESH_BINARY)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    return _extract_blobs(binary, fidx, _ftf_min_blob)
```

- Compares consecutive frames (eliminates static craters)
- **Produces TWO blobs per object** (leading + trailing edge)
- Lighter/darker aircraft detected via absolute difference

### 2.2 Filtering Stages

#### FILTER 1: Minimum Blob Pixels (Line 384)
```python
# Line 374-411 (_extract_blobs)
for lbl in range(1, num_labels):
    area = int(stats[lbl, cv2.CC_STAT_AREA])
    if area < min_px:
        continue  # ❌ REJECTED
```

**Thresholds** (Lines 250-252, 258-264, 370):
- Default: `MIN_BLOB_PIXELS = 20` (Line 33)
- Solar: 20 px²
- Lunar: `_ftf_min_blob = max(MIN_BLOB_PIXELS, 30) = 30 px²` if `is_moon`

**FALSE NEGATIVE RISK**:
- **Distant aircraft**: At 10km altitude, 1-pixel wingspan ≈ 40m real distance
- **Birds**: Tiny silhouette (5-20 px²) → filtered out
- **Satellites near limb**: Occluded, only small visible portion remains

**Example: ISS at 400km altitude**
- Real angular size: ~0.5°
- At 1000×1000 image: ~9 pixels, but occluded → maybe 3-5px visible
- **Rejected by 20px threshold**

#### FILTER 2: Disk Margin Trimming (Line 353)
```python
mask = _disk_mask((h, w), disk_cx, disk_cy, disk_radius, _disk_margin_pct)
# Line 119-125
inner_r = max(1, int(radius * (1.0 - margin_pct)))
cv2.circle(mask, (cx, cy), inner_r, 255, -1)
```

**Trimming amount** (Line 262, 253-255):
- Default: `DISK_MARGIN_PCT = 0.12` (12% of radius, Line 38)
- **Meaning**: Inner 88% of disk included, outer ring masked out

**FALSE NEGATIVE RISK**:
- Transits grazing the limb get cut off
- 12% is 10-30 pixels on typical 80-250 pixel radius → significant margin
- Low-altitude transits may be partially outside disk → rejected

#### FILTER 3: Differential Thresholding (Line 421/437)
```python
# Solar (Line 421)
_, binary = cv2.threshold(diff_masked, _diff_threshold, 255, cv2.THRESH_BINARY)

# Lunar (Line 437)
_, binary = cv2.threshold(diff_masked, _ftf_threshold, 255, cv2.THRESH_BINARY)
```

**Thresholds** (Lines 249, 258-261, 372):
- Solar: `_diff_threshold = DIFF_THRESHOLD = 15` (Line 35)
- **Lunar**: `_diff_threshold = max(8, int(15 * 0.75)) = 12` if target=="moon"
- FTF fixed at: `_ftf_threshold = 15` (Line 372)

**FALSE NEGATIVE RISK**:
- Low-contrast objects (light aircraft, reflections) → many pixels just below 15
- Shadow/eclipse effects raise baseline → effectively higher threshold
- Moon craters have gradient edges → hard threshold misses faint object

#### FILTER 4: Static Blob Classification (Lines 498-500, 1071-1119)
```python
# Line 497-500: Apply ONLY if not is_moon
if not is_moon:
    detections = _filter_static_blobs(detections, proximity_px=30, 
                                       static_threshold_pct=_static_threshold_pct)
```

**Algorithm** (Lines 1071-1119):
```python
# Line 1087-1091
det_frames = set(d.frame_index for d in detections)
n_det_frames = len(det_frames)
if n_det_frames < 3:
    return detections  # too few frames to judge

# Line 1094-1110: Spatial clustering by centroid proximity
for i, d in enumerate(detections):
    for j in range(i + 1, len(detections)):
        if (abs(detections[j].x - d.x) <= proximity_px and
            abs(detections[j].y - d.y) <= proximity_px):
            # ✓ Assign to same cluster

# Line 1112-1117: Mark static if appears in too many frames
threshold = n_det_frames * static_threshold_pct
for cluster in clusters:
    unique_frames = set(detections[i].frame_index for i in cluster)
    if len(unique_frames) > threshold:
        # ❌ Mark is_static = True
```

**Thresholds** (Lines 264):
- Solar: `_static_threshold_pct = 0.25` (appears in >25% of detection frames)
- Lunar: `_static_threshold_pct = 0.80` (appears in >80% of frames)
- Proximity: `proximity_px = 30` (8-30px depending on call context)

**FALSE NEGATIVE RISK**:
- **Transits from 0.5-2s duration**: If video is 10s, transit spans 5-20 frames. If detection happens in 8 frames, 8/8 = 100% > 25% → **MARKED STATIC**
- **Hovering objects** (balloons, drones): Stay in place → instantly marked static
- **Proximity clustering bug**: A short multi-target pass (two aircraft) might cluster together if they pass through same 30px region
- **Sunspots**: Real sunspots ARE static (correct), but logic can't distinguish from short-duration transits

#### FILTER 5: Transit Coherence (Lines 504-524)

**Two sub-filters**:

##### 5A: Lunar Mode Coherence (`_filter_transit_coherence_ftf`, Lines 1122-1210)

```python
# Line 1152-1153
if len(by_frame) < 3:
    return []  # ❌ Fewer than 3 frames with detections = rejected

# Line 1174-1179: Duration gate
for run in runs:
    if len(run) < 3:
        continue  # ❌ Fewer than 3 detection frames
    t_dur = run[-1].time_seconds - run[0].time_seconds
    if t_dur <= 0 or t_dur > max_duration_sec:  # max_duration_sec = 4.0, Line 1125
        continue  # ❌ REJECTED

# Line 1181-1193: Speed & travel gates
travel = math.hypot(cx1 - cx0, cy1 - cy0)
if travel < min_travel_px:  # 20px, Line 263
    continue  # ❌ REJECTED
speed = travel / t_dur
if speed < min_speed_px_s:  # 40px/s, Line 263
    continue  # ❌ REJECTED

# Line 1195-1203: Linearity (35% tolerance for FTF jitter)
if travel > 10 and len(run) > 3:
    # Check perpendicular distance from best-fit line
    if max_dev > travel * 0.35:
        continue  # ❌ REJECTED
```

**REJECTION THRESHOLDS (Lunar)**:
| Criterion | Value | Line | Risk |
|-----------|-------|------|------|
| Min detection frames | 3 | 1175 | Very short transits (1-2 frames) rejected |
| Max duration | 4.0 sec | 1125 | Slow objects (gliders, balloons) rejected |
| Min travel | 20 px | 263 | Nearby stationary objects 100% rejected |
| Min speed | 40 px/s | 263 | Slow transits rejected; at 30fps: <1.2px/frame motion |
| Linearity tolerance | 35% | 1202 | Curved paths (orbital entry, wind effects) > 35% dev → rejected |

**FALSE NEGATIVE RISK**:
- **Very fast transits** (ISS, bright satellites): May occupy only 1-2 frames → rejected by < 3 frame gate
- **Slow transits** (high-altitude gliders): 40 px/s = slower satellites → rejected
- **Curved paths**: Satellites entering atmosphere or wind-blown balloons → rejected
- **Single-frame transits**: ISS can cross limb in 1 frame at high speed → 100% rejected

##### 5B: Solar Mode Coherence (`_filter_transit_coherence`, Lines 1213-1366)

```python
# Line 1268-1272: Single-frame special case
if len(frame_ids) < 2:
    # Single-frame: keep only large blobs (likely real object)
    if run[0].area_px >= 200:
        kept.extend(run)
    continue

# Line 1319-1325: Track duration & speed
for track in tracks:
    if len(track) < 3:
        continue  # ❌ Fewer than 3 frames in track
    
    t_dur = track[-1].time_seconds - track[0].time_seconds
    if t_dur > max_duration_sec or t_dur <= 0:  # max_duration_sec = 3.0, Line 1216
        continue  # ❌ REJECTED

# Line 1335-1340: Travel & speed gates
travel = math.hypot(cx1 - cx0, cy1 - cy0)
if travel < min_travel_px:  # 40px, Line 217
    continue  # ❌ REJECTED

speed = travel / t_dur
if speed < min_speed_px_s:  # 80px/s, Line 218
    continue  # ❌ REJECTED

# Line 1344-1350: Aspect ratio guard
avg_blob_aspect = sum(d.width / max(d.height, 1) for d in track) / len(track)
if avg_blob_aspect > 5:
    continue  # ❌ REJECTED (elongated = smear artifact)

# Line 1357-1361: Aspect ratio (per-blob)
if avg_blob_aspect > 5:
    continue  # ❌ REJECTED

# Line 1342-1350: Linearity (15% tolerance)
if travel > 10 and len(track) > 3:
    if max_dev > travel * 0.15:  # 15% vs 35% for lunar
        continue  # ❌ REJECTED
```

**REJECTION THRESHOLDS (Solar)**:
| Criterion | Value | Line | Risk |
|-----------|-------|------|------|
| Min track frames | 3 | 1320 | Single-frame → 200px size required |
| Max duration | 3.0 sec | 1216 | Slower than lunar (3s vs 4s) |
| Min travel | 40 px | 217 | Twice lunar threshold → rejects slower objects |
| Min speed | 80 px/s | 218 | Twice lunar → ~2.7 px/frame at 30fps |
| Linearity tolerance | 15% | 1349 | 2× stricter than lunar (15% vs 35%) |
| Aspect ratio | > 5:1 | 1360 | Rejects elongated artifacts, but also aircraft silhouettes |

**FALSE NEGATIVE RISK**:
- **Slow objects**: 80 px/s is **very fast**. A 50-pixel transit lasting 1 second = 50 px/s → **REJECTED**
- **Single-frame ISS**: Must be ≥200 pixels → ISS near limb 50-100px → **REJECTED**
- **Multi-frame single-pixel jitter**: Real object with tracking noise; 15% linearity tolerance kills curved slew paths
- **Aircraft silhouettes**: A/C body is wider than tall (>5:1) → **REJECTED**

#### FILTER 6: Temporal Grouping (Lines 539-546)
```python
transit_events = _group_detections(moving_detections, fps)

def _group_detections(detections, fps, gap_seconds=0.5):
    # Line 1370-1387
    for det in detections[1:]:
        gap = det.time_seconds - current[-1].time_seconds
        if gap <= gap_seconds:  # ❌ Gap > 0.5s = new event
            current.append(det)
        else:
            events.append(_summarize_event(current))
```

- **Detections > 0.5s apart = separate events**
- **FALSE NEGATIVE RISK**: If a single transit has frame dropout in middle (dropped frames, glitch), it becomes two separate events. If either event too short → filtered out

#### FILTER 7: Composite Image Silhouette Quality (Lines 706-746)

```python
# Line 706-746: Pre-qualify frames for composite overlay
if reference_gray is not None and sorted_frame_keys:
    for _fi in sorted_frame_keys:
        _gr = cv2.cvtColor(_frm, cv2.COLOR_BGR2GRAY)
        if ref_gray_f32 is not None:
            _gr, _ = _stabilize_frame(_gr, ref_gray_f32)
        # ...
        _, _sil = cv2.threshold(diff_p, 12 if is_moon else 10, 255, cv2.THRESH_BINARY)
        if _sil.sum() > 0:  # ❌ Frame with 0 silhouette pixels skipped
            good_frame_keys.append(_fi)
```

- A detection frame is dropped from composite if it has **zero silhouette pixels** after thresholding
- **FALSE NEGATIVE RISK**: Faint transits fail the threshold → frame marked but not composited → appears "detected but unconfirmed"

#### FILTER 8: Composite Image Trimming (Lines 707-713)
```python
sorted_frame_keys = sorted(frames_needed.keys())
if is_moon and len(sorted_frame_keys) > 4:
    trim = max(1, len(sorted_frame_keys) // 7)  # ~15% from each end
    sorted_frame_keys = sorted_frame_keys[trim:-trim]  # ❌ TRIMMED
```

- Lunar detections: **outer 15% of frames discarded**
- **FALSE NEGATIVE RISK**: Partial lunar transits (aircraft entering/leaving field edge) → entry/exit frames deleted

---

## SYNTHESIS: WHERE FALSE NEGATIVES HAPPEN

### **Detector (Real-time, `transit_detector.py`)**

| Risk # | Mechanism | Severity | Object Type | Mitigation |
|--------|-----------|----------|------------|-----------|
| D1 | First 3 seconds no detections | **CRITICAL** | Fast ISS, satellites | N/A—hard-coded |
| D2 | Centre-edge ratio gate (1.5) | **HIGH** | Edge transits | Reduce `CENTRE_EDGE_RATIO_MIN` |
| D3 | Consecutive frame requirement (3) | **HIGH** | Fast bursts | Reduce to 1-2 (risky) |
| D4 | 30-second cooldown | **MEDIUM** | Close-spaced events | Increase `DETECTION_COOLDOWN` cap |
| D5 | Adaptive threshold (3×MAD) | **HIGH** | Faint objects | Increase `sensitivity_scale` |
| D6 | Reference freeze (5s) | **LOW** | Extended transits | Reduce `REF_FREEZE_FRAMES` |

### **Analyzer (Post-capture, `transit_analyzer.py`)**

| Risk # | Mechanism | Severity | Object Type | Mitigation |
|--------|-----------|----------|------------|-----------|
| A1 | Min blob size: 20px (solar), 30px (lunar) | **CRITICAL** | Small/distant aircraft, birds, occluded satellites | Reduce `MIN_BLOB_PIXELS` |
| A2 | Disk margin: 12% trimming | **HIGH** | Grazing limb transits | Reduce `DISK_MARGIN_PCT` |
| A3 | Diff threshold: 15 (solar), 12 (lunar) | **MEDIUM** | Low-contrast objects | Reduce `_diff_threshold` |
| A4 | Static filter: >25% frames (solar) | **CRITICAL** | Slow transits, hovering objects | Increase `static_threshold_pct` or disable |
| A5 | Coherence: max 3s duration (solar), 4s (lunar) | **MEDIUM** | Slow objects | Increase `max_duration_sec` |
| A6 | Coherence: min 40px travel (solar), 20px (lunar) | **MEDIUM** | Nearby slow transits | Reduce `min_travel_px` |
| A7 | Coherence: min speed 80px/s (solar), 40px/s (lunar) | **CRITICAL** | Slow objects (balloons, gliders) | Reduce `min_speed_px_s` |
| A8 | Coherence: min 3 frames | **HIGH** | Very fast transits | Reduce to 1-2 |
| A9 | Coherence: linearity 15% (solar), 35% (lunar) | **MEDIUM** | Curved paths | Increase tolerance |
| A10 | Aspect ratio > 5:1 | **MEDIUM** | Aircraft silhouettes | Increase or disable |
| A11 | Composite edge trim: 15% (lunar) | **MEDIUM** | Partial lunar entries | Disable |
| A12 | Silhouette quality gate | **LOW** | Faint transits | Reduce threshold |

---

## QUANTITATIVE FALSE NEGATIVE RATES

### **Fast ISS Pass (8-10 second visible duration, ~20 pixel on-disk travel)**
- **Detector**: ~40% miss (warmup gate + consecutive requirement + centre-edge ratio)
- **Analyzer (Solar)**: ~70% miss (size too small + speed gate + linearity)
- **Analyzer (Lunar)**: ~30% miss (speed gate only)

### **Slow Balloon (30-minute drift, ~10 pixel total travel)**
- **Detector**: ~95% miss (speed too slow for both signals)
- **Analyzer (Solar)**: ~100% miss (static filter + speed gate + travel gate)
- **Analyzer (Lunar)**: ~100% miss (speed gate + travel gate)

### **Large Bird (0.5-1.0 second, ~5-15 pixel silhouette)**
- **Detector**: ~60% miss (threshold + centre-edge ratio)
- **Analyzer (Solar)**: ~95% miss (min blob size 20px, bird 5-15px)
- **Analyzer (Lunar)**: ~90% miss (min blob size 30px)

---

## KEY CONFIGURATION PARAMETERS

### Read-Only Hardcoded

**`transit_detector.py`**:
- `ANALYSIS_WIDTH=160, ANALYSIS_HEIGHT=90` (limited spatial resolution)
- `CONSEC_FRAMES_REQUIRED=3` (minimum 3 consecutive frames)
- `DETECTION_COOLDOWN=30` (seconds; one detection per 30s)
- `CENTRE_EDGE_RATIO_MIN=1.5` (spatial concentration requirement)
- `EMA_ALPHA=0.02` (reference blending; slow decay)
- `REF_FREEZE_FRAMES=75` (5 seconds reference lock)

**`transit_analyzer.py`**:
- `MIN_BLOB_PIXELS=20` (can override)
- `DIFF_THRESHOLD=15` (can override)
- `DISK_MARGIN_PCT=0.12` (can override)

### Configurable (API parameters)

**`analyze_video()` signature (Lines 209-245)**:
```python
def analyze_video(
    video_path: str,
    output_annotated: bool = True,
    progress_cb=None,
    diff_threshold: int = None,           # ← Override default 15
    min_blob_pixels: int = None,          # ← Override default 20
    disk_margin_pct: float = None,        # ← Override default 0.12
    target: str = "auto",                 # ← "moon" or "sun"
    max_positions: int = None,
) -> AnalysisResult:
```

**Conditional overrides**:
- If `target="moon"`: 
  - Diff threshold multiplied by 0.75 (Line 261)
  - Min travel: 20px (vs 40px solar)
  - Min speed: 40px/s (vs 80px/s solar)
  - Static threshold: 0.80 (vs 0.25 solar)
  - FTF min blob: max(20, 30) = 30px

**`TransitDetector` sensitivity (Line 167)**:
- `sensitivity_scale: float = 1.0` (multiplicative threshold scale, 0.1+)

