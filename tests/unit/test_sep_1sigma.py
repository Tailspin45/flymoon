"""Pinning tests for the sep_1sigma helper in src.transit.

This test file lands in the test-first style: initially it pins the
intended behaviour of a `_compute_sep_1sigma` helper that replaces the
buggy inline block at src/transit.py:580-603 (try: pass, discarded
float(), __import__('math'), bare except → None).

After the production fix lands, all three tests must be green.
"""

import math

import pytest

from src.transit import _compute_sep_1sigma


def test_populated_with_valid_inputs():
    """With a valid Kalman sigma and a typical geometry, the helper must
    return a finite positive float in degrees — not None, not radians."""
    response = {
        "aircraft_elevation": 10_000.0,
        "target_alt": 30.0,
    }
    result = _compute_sep_1sigma(min_sep_sigma_m=100.0, response=response)

    # dist_m = 10000 / sin(30°) = 20000 m
    # angular_sigma(100, 20000) = degrees(atan2(100, 20000)) ≈ 0.2865°
    assert result is not None
    assert isinstance(result, float)
    assert 0.2 < result < 0.4
    # Regression pin: check unit. Radians would be ~0.005, degrees ~0.286.
    assert result > 0.1, "result is in radians, not degrees"


def test_returns_none_when_no_sigma():
    """Missing Kalman sigma (no filter data yet) must return None cleanly."""
    response = {"aircraft_elevation": 10_000.0, "target_alt": 30.0}
    assert _compute_sep_1sigma(min_sep_sigma_m=None, response=response) is None


def test_degenerate_target_altitude_does_not_divide_by_zero():
    """Near-horizon target (alt ≈ 0) must not raise and must return a finite
    positive number thanks to the 0.05 sin-floor at transit.py:596."""
    response = {
        "aircraft_elevation": 10_000.0,
        "target_alt": 0.01,  # well below the floor
    }
    result = _compute_sep_1sigma(min_sep_sigma_m=100.0, response=response)

    # With the 0.05 floor, dist_m = 10000 / 0.05 = 200_000 m
    # angular_sigma(100, 200_000) = degrees(atan2(100, 200000)) ≈ 0.0286°
    assert result is not None
    assert math.isfinite(result)
    assert result > 0.0
    assert result < 0.1


def test_fallback_aircraft_elevation_when_missing():
    """When the response is missing aircraft_elevation, the helper must
    fall back to the 10_000 m default rather than crash or silently
    return None."""
    response = {"target_alt": 30.0}  # no aircraft_elevation
    result = _compute_sep_1sigma(min_sep_sigma_m=100.0, response=response)
    assert result is not None
    assert 0.2 < result < 0.4
