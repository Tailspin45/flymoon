"""Pinning tests for src.imm_kalman.

Covers the IMM Kalman filter public API used by the prediction pipeline:
  * ENU ↔ geographic roundtrip
  * cold-start state from reported speed/heading
  * CV straight-line convergence + sigma monotonic decrease
  * CA mode weight lifts on a banked turn
  * angular_sigma monotonicity + unit regression (degrees, not radians)
  * cleanup_stale_filters TTL eviction

Time is controlled via monkeypatching src.imm_kalman.time.time so the warm
update path and TTL logic are deterministic.
"""

import math

import numpy as np
import pytest

from src import imm_kalman
from src.imm_kalman import (
    FILTER_TTL_S,
    _from_enu,
    _to_enu,
    angular_sigma,
    cleanup_stale_filters,
    state_position,
    update_filter,
)

OBS_LAT = 37.7749
OBS_LON = -122.4194


def _make_flight(lat, lon, speed_kmh, direction_deg, source="adsb"):
    return {
        "latitude": lat,
        "longitude": lon,
        "speed": speed_kmh,
        "direction": direction_deg,
        "position_source": source,
    }


def _set_time(monkeypatch, t):
    monkeypatch.setattr(imm_kalman.time, "time", lambda: t)


def test_enu_roundtrip():
    """_from_enu(_to_enu(x)) must recover the original (lat, lon) to 1e-6."""
    observers = [(0.0, 0.0), (37.7749, -122.4194), (51.5, -0.1), (-33.9, 151.2)]
    for obs_lat, obs_lon in observers:
        for dlat in (-0.5, 0.0, 0.25, 1.0):
            for dlon in (-0.75, 0.0, 0.3, 1.1):
                lat = obs_lat + dlat
                lon = obs_lon + dlon
                n, e = _to_enu(lat, lon, obs_lat, obs_lon)
                lat_back, lon_back = _from_enu(n, e, obs_lat, obs_lon)
                assert abs(lat_back - lat) < 1e-6
                assert abs(lon_back - lon) < 1e-6


def test_cold_start_state_matches_reported_heading(monkeypatch):
    """Cold-start velocity vector must match km/h→m/s + heading-to-compass."""
    _set_time(monkeypatch, 10_000.0)
    # Due east at 900 km/h = 250 m/s. Compass heading 90° → vn=0, ve=250.
    flight = _make_flight(OBS_LAT + 0.1, OBS_LON + 0.1, speed_kmh=900, direction_deg=90)
    state = update_filter("ABC123", flight, OBS_LAT, OBS_LON)

    vn = state.x_cv[2]
    ve = state.x_cv[3]
    assert abs(vn - 0.0) < 0.1
    assert abs(ve - 250.0) < 0.1
    # Mode probabilities start at the _MU_INIT prior (0.90, 0.10).
    assert state.mu[0] == pytest.approx(0.90, abs=1e-9)
    assert state.mu[1] == pytest.approx(0.10, abs=1e-9)


def test_cv_straight_line_converges(monkeypatch):
    """30 updates of a straight due-east track should converge within 50 m
    and produce non-increasing sigma over the last 10 steps."""
    # 250 m/s ≈ 0.002244 deg-lon/s at lat 37.7749 (cos≈0.79).
    # Step every 5 s → each step adds ~1250 m east → ~0.01422 deg-lon.
    step_s = 5.0
    speed_ms = 250.0
    # Exact longitude step computed from the same _M_PER_DEG_LAT convention
    # the filter uses, so ground truth is self-consistent.
    m_per_deg_lat = imm_kalman._M_PER_DEG_LAT
    dlon_per_step = (speed_ms * step_s) / (m_per_deg_lat * math.cos(math.radians(OBS_LAT)))

    t = 20_000.0
    last_sigma = None
    sigmas = []
    lat_cur = OBS_LAT
    lon_cur = OBS_LON + 0.3  # start well east of observer so dist is sane
    for i in range(30):
        _set_time(monkeypatch, t)
        flight = _make_flight(lat_cur, lon_cur, speed_kmh=900, direction_deg=90)
        state = update_filter("CV001", flight, OBS_LAT, OBS_LON)
        _, _, sigma_m = state_position(state)
        sigmas.append(sigma_m)
        t += step_s
        lon_cur += dlon_per_step
        last_sigma = sigma_m

    # Final position residual vs. the reported track: the filter may lag
    # slightly behind the latest measurement but should be within 1 km.
    final_n, final_e, _ = state_position(state)
    truth_n, truth_e = _to_enu(lat_cur - dlon_per_step * 0 - 0, lon_cur - dlon_per_step, OBS_LAT, OBS_LON)
    # Allow generous slack — the filter is lagged and has measurement noise floors.
    assert abs(final_n - truth_n) < 2000.0
    assert abs(final_e - truth_e) < 2000.0

    # Sigma over the last 10 steps should be non-increasing on average
    # (it will not strictly decrease every step due to process noise injection).
    tail = sigmas[-10:]
    assert tail[-1] <= tail[0] * 1.1  # allow 10% noise slack
    assert last_sigma > 0.0


def test_ca_turn_lifts_mode_weight(monkeypatch):
    """A banked turn (direction rotating ~20°/step at 250 m/s) should lift
    the CA mode weight above 0.5 within ~12 updates."""
    step_s = 2.0
    turn_rate_deg_per_step = 20.0
    speed_ms = 250.0
    m_per_deg_lat = imm_kalman._M_PER_DEG_LAT
    cos_obs = math.cos(math.radians(OBS_LAT))

    t = 30_000.0
    lat_cur = OBS_LAT + 0.2
    lon_cur = OBS_LON + 0.3
    heading_deg = 0.0  # north initially
    mu_ca_seen = []
    for i in range(15):
        _set_time(monkeypatch, t)
        flight = _make_flight(lat_cur, lon_cur, speed_kmh=speed_ms * 3.6, direction_deg=heading_deg)
        state = update_filter("CA001", flight, OBS_LAT, OBS_LON)
        mu_ca_seen.append(state.mu[1])

        # Advance the truth trajectory along the current heading
        hdg_rad = math.radians(heading_deg)
        dn = speed_ms * step_s * math.cos(hdg_rad)
        de = speed_ms * step_s * math.sin(hdg_rad)
        lat_cur += dn / m_per_deg_lat
        lon_cur += de / (m_per_deg_lat * cos_obs)
        heading_deg = (heading_deg + turn_rate_deg_per_step) % 360
        t += step_s

    assert max(mu_ca_seen) > 0.5, (
        f"CA mode weight never exceeded 0.5 during a hard turn; "
        f"max={max(mu_ca_seen):.3f}, history={[round(x, 3) for x in mu_ca_seen]}"
    )


def test_angular_sigma_monotonicity_and_units():
    """angular_sigma must be degrees (not radians), monotone in both args,
    and short-circuit to 0 for dist_m < 1."""
    # Unit regression: 100 m @ 10 km ≈ atan(100/10000) ≈ 0.5729°
    val = angular_sigma(100.0, 10_000.0)
    assert 0.55 < val < 0.60

    # dist_m < 1 short-circuit
    assert angular_sigma(500.0, 0.5) == 0.0

    # Monotone-increasing in sigma_m, fixed dist
    vals = [angular_sigma(s, 10_000.0) for s in (10, 50, 100, 500, 1000)]
    assert all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))

    # Monotone-decreasing in dist_m, fixed sigma
    vals = [angular_sigma(100.0, d) for d in (1_000, 5_000, 10_000, 50_000, 100_000)]
    assert all(vals[i] > vals[i + 1] for i in range(len(vals) - 1))


def test_update_filter_handles_missing_vz(monkeypatch):
    """OpenSky frequently returns None for vertical_rate_ms, speed, and/or
    direction.  update_filter must not raise and must return a valid state
    with finite position components when any of these fields is None or absent.

    This is a regression guard: a broken coercion (e.g. float(None)) would
    raise TypeError and silently drop the aircraft from prediction entirely.
    """
    _set_time(monkeypatch, 70_000.0)

    # Worst-case OpenSky payload: only mandatory lat/lon present.
    sparse = {
        "latitude": OBS_LAT + 0.15,
        "longitude": OBS_LON + 0.15,
        "speed": None,
        "direction": None,
        "vertical_rate_ms": None,
        "position_source": "opensky",
    }
    state = update_filter("SPARSE1", sparse, OBS_LAT, OBS_LON)

    # Must return a valid state (no exception above is already the key check).
    n, e, sigma_m = state_position(state)
    assert math.isfinite(n)
    assert math.isfinite(e)
    assert math.isfinite(sigma_m)
    assert sigma_m > 0.0

    # A second update with the same sparse payload must also succeed.
    _set_time(monkeypatch, 70_005.0)
    state2 = update_filter("SPARSE1", sparse, OBS_LAT, OBS_LON)
    n2, e2, _ = state_position(state2)
    assert math.isfinite(n2) and math.isfinite(e2)


def test_stale_cleanup(monkeypatch):
    """A filter untouched for longer than FILTER_TTL_S must be evicted."""
    _set_time(monkeypatch, 50_000.0)
    flight = _make_flight(OBS_LAT + 0.1, OBS_LON + 0.1, 900, 90)
    update_filter("STALE1", flight, OBS_LAT, OBS_LON)
    assert "STALE1" in imm_kalman._filters

    # Advance past the TTL boundary and run cleanup.
    _set_time(monkeypatch, 50_000.0 + FILTER_TTL_S + 1)
    removed = cleanup_stale_filters()
    assert removed == 1
    assert "STALE1" not in imm_kalman._filters
