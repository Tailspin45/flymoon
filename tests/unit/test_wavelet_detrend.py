"""Pinning tests for src.transit_detector._wavelet_detrend.

The function returns a single float — the magnitude of the detrended last
sample. Tests therefore assert on the scalar, not on an array shape.
"""

import collections

import pytest

from src import transit_detector
from src.transit_detector import _wavelet_detrend


def _deque(values):
    return collections.deque(values)


def test_short_buffer_fallback_to_abs():
    """len(buf) < 16 must return abs(buf[-1]) without invoking pywt."""
    buf = _deque([0.0] * 9 + [-3.7])
    assert _wavelet_detrend(buf) == pytest.approx(3.7, abs=1e-9)


def test_dc_input_returns_near_zero():
    """A constant buffer has no detail energy — the detrended last sample
    should be very close to zero once the approximation band is zeroed."""
    buf = _deque([5.0] * 128)
    assert abs(_wavelet_detrend(buf)) < 1e-3


def test_linear_ramp_is_attenuated():
    """A linear ramp is partially attenuated but not killed entirely. The
    periodization boundary mode creates a discontinuity at the end of the
    window (buf[-1] wraps to buf[0]), and that discontinuity survives in
    the detail band. The test pins that the detrended value is strictly
    less than the raw magnitude — i.e., the slow-trend subtraction is
    doing *something* — without overclaiming that it flattens slow trends
    completely."""
    buf = _deque([i * (10.0 / 127.0) for i in range(128)])
    raw = abs(buf[-1])
    detrended = abs(_wavelet_detrend(buf))
    assert detrended < raw
    assert detrended >= 0.0


def test_transient_survives():
    """A 6-sample triangular pulse at the tail of a 128-sample zero buffer
    should retain a substantial fraction of its peak amplitude."""
    samples = [0.0] * 122 + [2.0, 4.0, 6.0, 8.0, 10.0, 10.0]
    buf = _deque(samples)
    result = _wavelet_detrend(buf)
    assert result >= 5.0


def test_pywt_unavailable_path(monkeypatch):
    """With pywt flagged unavailable the function must fall back to
    abs(buf[-1]) for any buffer length, including long ones."""
    monkeypatch.setattr(transit_detector, "_PYWT_AVAILABLE", False)
    buf = _deque([0.0] * 127 + [-2.5])
    assert _wavelet_detrend(buf) == pytest.approx(2.5, abs=1e-9)
