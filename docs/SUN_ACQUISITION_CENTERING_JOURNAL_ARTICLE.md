# Hybrid Astronomical and Vision Closed Loop Sun Acquisition and Centering for Seestar Class Systems

## Abstract
This paper presents a practical algorithm for automatic Sun acquisition and centering in a consumer smart telescope control stack that combines Electron, JavaScript, Python, ALPACA, and RTSP video analysis. The method is hybrid by design: (1) open loop astronomical pointing gives a fast first estimate, (2) an active search policy recovers from mount model error, and (3) a vision closed loop controller centers and maintains the solar disc near image center. Unlike purely intensity maximizing methods, this approach does not require irradiance sensors or absolute flux calibration. It uses observables already present in Zipcatcher: mount telemetry via ALPACA, disc fit status from the detection pipeline, and optional image statistics. The method is robust to firmware constraints where JSON-RPC query commands may be restricted and where only ALPACA telemetry is reliable for mount state.

## 1. Introduction
Automatic Sun centering is a control problem with three coupled uncertainties:
1. Ephemeris uncertainty is tiny, but local pointing error can still be significant due to mount model, leveling, backlash, and offsets.
2. Perception uncertainty changes with seeing, cloud edges, haze, and occasional disc loss.
3. Actuation uncertainty exists because axis response is rate based, not perfect position servo control at the application layer.

For this class of system, a single strategy is not enough:
1. Pure ephemeris pointing is fast but can miss the disc if the mount model has bias.
2. Pure image search is robust but slow and may fail in poor contrast.
3. Pure intensity hill climbing can be unstable under clouds.

A hybrid approach is therefore preferred and is standard in solar tracking systems: use time and location for coarse aim, then use sensor feedback for fine nulling.

## 2. Available Signals and Control Channels in Zipcatcher
The algorithm is constrained to what the current stack exposes.

### 2.1 Control
1. ALPACA movement and state:
   - MoveAxis (axis rate control)
   - Stop axes
   - GoTo Alt/Az (implemented via Alt/Az to RA/Dec conversion and asynchronous slew)
   - Tracking on/off
   - Slewing status
2. Telescope mode via Seestar JSON-RPC:
   - Start solar mode
   - Start scenery mode

### 2.2 Observables
1. ALPACA telemetry for mount state (cached and polled): alt, az, tracking, slewing.
2. Detector status stream includes:
   - disk_detected
   - disk_info with cx, cy, radius
   - disc_lost_warning and disc_lost_frames
   - signal_trace (diagnostic)
3. Optional auxiliary observables:
   - ALPACA camera imagearray if camera device endpoints are available
   - RTSP derived luma metrics if needed

The key result is that direct irradiance is optional, not required.

## 3. Problem Statement
Given observer site $(lat, lon, elev)$ and current time $t$, define target Sun direction in mount coordinates:
$$
(alt_s(t), az_s(t))
$$
Let image center be $(u_0, v_0)$ and detected disc center be $(u_d, v_d)$.
Define image error vector:
$$
\mathbf{e} = [u_d - u_0,\ v_d - v_0]^T
$$
Goal: drive $\|\mathbf{e}\|$ below a pixel tolerance while keeping the disc continuously detected and avoiding oscillation.

## 4. Proposed Algorithm

### 4.1 State Machine
1. PRECHECK
2. COARSE_POINT
3. ACQUIRE_SEARCH
4. FINE_CENTER
5. LOCK_MONITOR
6. RECOVER
7. FAIL_SAFE

### 4.2 PRECHECK
1. Verify Seestar connected.
2. Verify ALPACA connected and telemetry fresh.
3. Ensure Sun altitude is above a configurable floor (for example 10 deg).
4. Enter solar mode.
5. Start detector if not running.

Exit condition: all preconditions true.

### 4.3 COARSE_POINT (Ephemeris Open Loop)
1. Compute current Sun alt/az from astronomy module.
2. Issue GoTo Alt/Az using existing route.
3. Wait for slew settle and telemetry stability.

Exit condition:
1. disk_detected true within timeout, or
2. transition to ACQUIRE_SEARCH on timeout.

### 4.4 ACQUIRE_SEARCH (Robust Recovery)
Use an expanding rosette or spiral around the ephemeris point:
1. Generate candidate offsets $(dalt_i, daz_i)$ in increasing radius.
2. At each candidate:
   - nudge or micro-goto
   - wait settle window
   - evaluate acquisition score
3. Acquisition score (no flux requirement):
   - primary: disk_detected true with stable radius for N frames
   - secondary: disc_lost_warning false
   - optional tie breaker: radial edge strength or luma confidence

Exit condition: stable disc lock for N consecutive checks.

### 4.5 FINE_CENTER (Closed Loop Servo)
Use disc centroid error to drive axis rates with deadband and anti windup.

1. Convert pixel error to normalized image error:
$$
\tilde{e}_u = \frac{u_d-u_0}{r_d},\quad \tilde{e}_v = \frac{v_d-v_0}{r_d}
$$
where $r_d$ is disc radius in pixels.

2. PI controller per axis:
$$
\omega_{az}=K_{p,az}\tilde{e}_u + K_{i,az}\int \tilde{e}_u dt
$$
$$
\omega_{alt}=K_{p,alt}\tilde{e}_v + K_{i,alt}\int \tilde{e}_v dt
$$
3. Clamp to safe rates and apply slew rate limiting.
4. Deadband near center to prevent chatter.
5. Stop axes when both errors are within tolerance for hold time.

Suggested initial design values:
1. Center tolerance: 0.10 to 0.15 disc radii.
2. Control tick: 4 to 8 Hz.
3. Max correction rate: conservative fraction of max move rate.
4. Integral reset whenever disc is lost.

### 4.6 LOCK_MONITOR
1. Keep tracking in desired mode.
2. Re-evaluate error periodically.
3. If error drifts above threshold, re-enter FINE_CENTER.
4. If disc lost beyond grace threshold, enter RECOVER.

### 4.7 RECOVER and FAIL_SAFE
1. RECOVER first attempts local micro-search near last known lock.
2. If repeated failures, re-run COARSE_POINT then ACQUIRE_SEARCH.
3. FAIL_SAFE stops axes, reports reason, and requires operator acknowledgment.

## 5. Why This Method Fits This Stack
1. It is compatible with ALPACA request response semantics and polling.
2. It is robust to known Seestar JSON-RPC query limitations on newer firmware.
3. It reuses detector outputs already computed for transit detection, so no expensive new vision stack is required.
4. It avoids dependence on absolute intensity, which is unstable under cloud and haze.
5. It supports optional intensity/imagearray enhancements when available.

## 6. Evaluation Protocol
Measure performance in staged tests.

### 6.1 Bench and Dry Run
1. Replay archived solar clips to validate centroid loop logic.
2. Inject synthetic disc offsets and verify convergence.

### 6.2 Field Tests
1. Time to first lock from cold start.
2. Final centering error in pixels and in disc radii.
3. Drift under 30 to 60 minute hold.
4. Recovery time after forced disc loss.
5. False recoveries under patchy cloud.

### 6.3 Acceptance Targets
1. First lock median under 25 s after coarse slew.
2. Steady state error under 0.12 disc radii.
3. Recovery under 15 s for short disc loss events.
4. No uncontrolled oscillation or persistent rate saturation.

## 7. Implementation Notes for Electron, Python, JavaScript
1. Python:
   - Add a sun centering service module with the above state machine.
   - Expose start/stop/status routes under telescope API.
2. JavaScript:
   - Add a centering control panel and status badges.
   - Poll service status and detector status, display lock quality.
3. Electron:
   - No special IPC is required if Flask endpoints are used from existing UI.

## 8. Limitations
1. Strong cloud edges can still break disc fit and trigger recovery loops.
2. Mount backlash and stiction may need axis specific tuning.
3. Camera frame delay and telemetry delay reduce control bandwidth.
4. Very low Sun altitude increases atmospheric distortion and can degrade lock quality.

## 9. References
1. Reda, I., Andreas, A., Solar Position Algorithm for Solar Radiation Applications, Solar Energy 76(5), 2004. NREL SPA page: https://midcdmz.nrel.gov/spa/
2. ASCOM Alpaca Device API (v1): https://ascom-standards.org/api/
3. OpenCV Hough Circle Transform tutorial: https://docs.opencv.org/4.x/d4/d70/tutorial_hough_circle.html
4. Seestar ALPACA technical reference in this repository: docs/SEESTAR_ALPACA_TECHNICAL_REFERENCE.md
5. Zipcatcher as-built architecture reference: docs/AS_BUILT_REFERENCE.md
6. Community discussion on Seestar firmware lockout and ALPACA fallback: https://github.com/smart-underworld/seestar_alp/issues/697
