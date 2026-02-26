# Synthetic Test Data Generation for Flymoon Transit Detection

## Abstract

Validating an aircraft transit detection pipeline against live data is impractical: transits are rare, the target's sky position changes continuously, and API dependencies make repeatable unit testing impossible. This note describes the synthetic test data strategy used by Flymoon — how geometrically correct flight positions are constructed from first principles for any sky configuration, how the data is formatted to match the live API pipeline, and what pitfalls were discovered and corrected during development.

---

## 1. Motivation

The transit detection pipeline (`src/transit.py`) accepts a bounding box of aircraft, projects each trajectory forward 15 minutes, converts geographic positions to alt-azimuth coordinates via Skyfield, and classifies the minimum angular separation against a fixed threshold ladder (HIGH ≤ 1.5°, MEDIUM ≤ 2.5°, LOW ≤ 3.0°). To test this end-to-end without a live FlightAware or OpenSky connection, we need aircraft at *known* angular offsets from the Sun or Moon at the moment the test runs.

The key difficulty is that the Sun and Moon move. A test flight hardcoded to transit the Sun at az = 240° will fail whenever the Sun is not near that azimuth — which is most of the time. The generator therefore queries Skyfield for the *current* sky positions of both objects before placing any aircraft.

---

## 2. Coordinate Geometry

### 2.1 The Transit Ground Point

From a ground observer at position **O** = (φ, λ, h), an aircraft at altitude *H* (metres) appears to transit a celestial target at elevation angle *α* when its horizontal distance *d* from the observer satisfies:

$$d = \frac{H - h}{\tan \alpha}$$

This is the flat-Earth approximation, valid for the altitudes and ranges involved (H ~ 10 km, d ~ 9–30 km depending on α).

### 2.2 Projecting Along an Azimuth

Given the target's azimuth θ and the required horizontal distance *d*, the geographic displacement from the observer is:

$$\Delta\phi = \frac{d}{R_E} \cos\theta, \quad \Delta\lambda = \frac{d}{R_E \cos\phi} \sin\theta$$

The cosine correction on longitude (`/ cos(φ)`) is **critical**. Omitting it compresses east-west displacement by a factor of cos(φ) ≈ 0.84 at latitude 33°, producing a systematic azimuth error of ~2° that causes all test flights to miss their classification tier.

### 2.3 Angular Separation Metric

The pipeline does not use independent altitude and azimuth thresholds. Instead it computes a cosine-corrected on-sky angular separation:

$$\sigma = \sqrt{(\Delta\text{alt})^2 + (\Delta\text{az} \cdot \cos\alpha_\text{target})^2}$$

Near the zenith, azimuth lines converge, so a raw Δaz = 10° at α = 88° is geometrically negligible. The cosine factor reduces it to near zero, preventing false positives. Test flights are therefore positioned with **altitude-only offsets** (Δaz ≈ 0) to give a clean one-to-one mapping from offset size to angular separation tier:

| Flight ID | Altitude offset | Target σ | Expected tier |
|-----------|----------------|----------|---------------|
| `*_HIGH`  | +0.5°          | ~0.5°    | HIGH (≤ 1.5°) |
| `*_MED`   | +2.0°          | ~2.0°    | MEDIUM (≤ 2.5°) |
| `*_LOW`   | +2.8°          | ~2.8°    | LOW (≤ 3.0°) |
| `NONE_*`  | far off-axis   | > 20°    | UNLIKELY |

---

## 3. Data Format and Unit Conventions

The synthetic flights are serialised in FlightAware AeroAPI format so they pass through `parse_fligh_data()` without modification. One unit convention is especially important:

> **FlightAware `altitude` is in hundreds of feet, not feet.**
> A cruise altitude of FL350 (35,000 ft) is stored as `350`.
> `parse_fligh_data()` converts it to metres via `altitude × 0.3048 × 100`.
> Storing raw feet (35000) would produce an elevation of ~1,036 km, placing the aircraft in low Earth orbit and making Skyfield return an altitude angle near 90° for any observer position.

Groundspeed is stored in knots (FA native); `parse_fligh_data()` converts to km/h. The `heading` field is in degrees clockwise from north and is left as-is.

---

## 4. Pipeline Integration

### 4.1 Test Mode Guard

When `get_transits()` is called with `test_mode=True`, the function loads aircraft from `data/raw_flight_data_example.json` instead of calling FlightAware or OpenSky. A guard added during debugging is essential:

```python
if not test_mode and data_source in ("hybrid", "opensky-only"):
    # fetch from OpenSky
```

Without the `not test_mode` guard, the OpenSky block ran unconditionally (it is a separate `if`, not an `elif`), overwriting the six synthetic test flights with hundreds of real aircraft and making the test non-deterministic.

### 4.2 Celestial Position Freshness

The generator calls Skyfield at the moment it runs and stores the resulting alt/az in `_test_metadata`. The integration test reads this metadata and skips validation for any target below 15° (where bounding-box geometry becomes unreliable). This means:

- During the day: Sun tests pass; Moon tests are skipped.
- At night: Moon tests pass; Sun tests are skipped.
- Both visible simultaneously: all six target-specific flights are validated.

Because the generator uses real-time positions rather than fixtures, re-running the generator immediately before the test is recommended. The workflow is:

```bash
python3 data/test_data_generator.py --scenario dual_tracking
python3 tests/test_integration.py
```

---

## 5. Known Limitations

**15-minute projection.** `check_transit` projects each aircraft forward under constant velocity for 15 minutes. The synthetic flights are placed *at* the target at t = 0, but fly away on a random heading. If the random heading moves the aircraft directly away from the target, the minimum angular separation is found at t = 0 and the classification is correct. If the heading moves the aircraft *toward* the target briefly before diverging, the minimum may be slightly smaller than the placed offset, potentially upgrading a MEDIUM to HIGH. The fixed random seed (`seed = 42`) in the generator ensures reproducibility.

**Bounding-box clamping.** The generator clamps aircraft positions to a fixed bounding box (32.0–33.5° N, 118.0–117.0° W). For target azimuths pointing outside this box (e.g., due north or east) the clamped position will not achieve the intended angular separation and the test will fail. The current scenario always uses the Sun and Moon, whose azimuths at the San Diego observer location are sufficiently southerly during reasonable observing hours.

**Observer position is hardcoded.** Both the generator and the integration test use a fixed observer at (33.11° N, 117.31° W, 100 m). This is intentional: the bounding box in the FA API call is also centred on this location. A different deployment site requires updating `OBSERVER_LAT`, `OBSERVER_LON`, and `OBSERVER_ELEV` in `data/test_data_generator.py` and the bounding-box constants.

---

## 6. Modifying the Tests

To add a new classification tier or change thresholds:

1. Update `get_possibility_level()` in `src/transit.py`.
2. Update `HIGH_OFFSET`, `MED_OFFSET`, `LOW_OFFSET` in `generate_test_data()` so that the altitude offsets fall within the new tier boundaries after cosine correction.
3. Update the `expected_high / _medium / _low` lists in `tests/test_integration.py`.
4. Regenerate: `python3 data/test_data_generator.py --scenario dual_tracking`.
5. Run: `python3 tests/test_integration.py`.

To add a new scenario (e.g., testing a local ADS-B receiver source), add an entry to `get_scenarios()` in the generator and a corresponding `elif data_source == "adsb-local":` branch with a `test_mode` guard in `get_transits()`.
