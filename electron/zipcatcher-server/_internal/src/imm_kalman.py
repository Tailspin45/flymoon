"""
Interacting Multiple Model (IMM) Kalman Filter for aircraft trajectory prediction.

Maintains two sub-models:
    CV — Constant Velocity  [state: north, east, vn, ve]          (4-D)
    CA — Constant Acceleration  [state: north, east, vn, ve, an, ae]  (6-D)

The IMM blends both models with probability weights μ = [μ_cv, μ_ca].
During straight cruise, μ_cv → 1. When the aircraft manoeuvres, μ_ca rises
automatically, producing a better position forecast and a wider uncertainty
cone — exactly what Zipcatcher needs to avoid both false-positive and
missed-transit alerts.

Per-aircraft filter state is cached by ICAO24 for up to FILTER_TTL_S seconds
so that successive ADS-B refreshes (≈ every 60 s) progressively improve the
estimate.  On the first observation the filter is initialised and is
equivalent to constant-velocity dead-reckoning but with an explicit
uncertainty cone.  From the second observation onward the filter starts
converging.

References
----------
[L2] arXiv:2312.15721 — IMM-KF for ADS-B UAV tracking; 28.56 % RMSE reduction
     vs constant-velocity for maneuvering targets.
[L3] Aerospace 2023, 10, 698 — Adaptive IMM-UKF; robust through sensor outages.

Public API
----------
update_filter(icao24, flight, obs_lat, obs_lon)  → _IMMState
    Update (or initialise) the per-aircraft filter with a new ADS-B position.

predict_position(state, dt_s, obs_lat, obs_lon)  → (lat, lon, sigma_m)
    Propagate the combined IMM state forward *dt_s* seconds and return the
    predicted position plus 1-σ position uncertainty in metres.

angular_sigma(sigma_m, dist_m)  → sigma_deg
    Convert position uncertainty (metres) to angular uncertainty (degrees)
    given the slant distance from observer to aircraft.

cleanup_stale_filters()  → int
    Remove cached states older than FILTER_TTL_S.  Call periodically.
"""

import time
from dataclasses import dataclass
from math import atan2, cos, degrees, radians, sin, sqrt
from typing import Dict, Optional, Tuple

import numpy as np

from src import logger

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

FILTER_TTL_S: int = 1200  # 20 min — discard states not updated within this window

# IMM mode-to-mode transition probabilities  M[i→j]
# Row i = from-model, col j = to-model.  Rows must sum to 1.
_M = np.array(
    [
        [0.92, 0.08],  # CV → CV=92%, CA=8%
        [0.20, 0.80],
    ],  # CA → CV=20%, CA=80%  (CA is transient)
    dtype=float,
)

# Initial mode probabilities  [μ_cv, μ_ca]
_MU_INIT = np.array([0.90, 0.10], dtype=float)

# Process noise spectral density (m/s²)²  — higher = model trusts measurements more
# Cruise aircraft maintain very stable speed/heading: CV noise is tiny.
# CA model gets ~10× more noise to capture turns and climbs.
_Q_CV: float = 0.005  # CV model: ≈0.07 m/s² per step  (±~5 km/h drift / 15 min)
_Q_CA: float = 0.05  # CA model: ≈0.22 m/s² per step  (allows gentle turns)

# Measurement noise variance (m²)
_R_ADSB: float = 50.0**2  # direct ADS-B:  ~50 m CEP
_R_OPENSKY: float = 500.0**2  # OpenSky:       ~500 m CEP (network-aggregated)
_R_OTHER: float = 1000.0**2  # MLAT / other:  ~1 km

# Minimum variance floor (numerical stability)
_P_FLOOR: float = 1.0

# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

_M_PER_DEG_LAT: float = 111_320.0


def _to_enu(
    lat: float, lon: float, obs_lat: float, obs_lon: float
) -> Tuple[float, float]:
    """Convert (lat, lon) to ENU (north_m, east_m) relative to observer."""
    north_m = (lat - obs_lat) * _M_PER_DEG_LAT
    east_m = (lon - obs_lon) * _M_PER_DEG_LAT * cos(radians(obs_lat))
    return north_m, east_m


def _from_enu(
    north_m: float, east_m: float, obs_lat: float, obs_lon: float
) -> Tuple[float, float]:
    """Convert ENU (north_m, east_m) back to (lat, lon)."""
    lat = obs_lat + north_m / _M_PER_DEG_LAT
    lon = obs_lon + east_m / (_M_PER_DEG_LAT * cos(radians(obs_lat)) + 1e-12)
    return lat, lon


# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------


@dataclass
class _IMMState:
    x_cv: np.ndarray  # CV state  [n, e, vn, ve]  (4,)
    P_cv: np.ndarray  # CV covariance  (4,4)
    x_ca: np.ndarray  # CA state  [n, e, vn, ve, an, ae]  (6,)
    P_ca: np.ndarray  # CA covariance  (6,6)
    mu: np.ndarray  # mode probabilities  [μ_cv, μ_ca]  (2,)
    obs_lat: float
    obs_lon: float
    last_update: float


# ---------------------------------------------------------------------------
# Per-aircraft filter cache
# ---------------------------------------------------------------------------

_filters: Dict[str, _IMMState] = {}


# ---------------------------------------------------------------------------
# Transition matrices and process-noise matrices
# ---------------------------------------------------------------------------


def _F_cv(dt: float) -> np.ndarray:
    return np.array(
        [[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float
    )


def _Q_cv_mat(dt: float, q: float) -> np.ndarray:
    """Discrete-time process-noise for constant-velocity model (Singer form)."""
    t2, t3, t4 = dt * dt, dt**3, dt**4
    return q * np.array(
        [
            [t4 / 4, 0, t3 / 2, 0],
            [0, t4 / 4, 0, t3 / 2],
            [t3 / 2, 0, t2, 0],
            [0, t3 / 2, 0, t2],
        ],
        dtype=float,
    )


def _F_ca(dt: float) -> np.ndarray:
    h = dt * dt / 2
    return np.array(
        [
            [1, 0, dt, 0, h, 0],
            [0, 1, 0, dt, 0, h],
            [0, 0, 1, 0, dt, 0],
            [0, 0, 0, 1, 0, dt],
            [0, 0, 0, 0, 1, 0],
            [0, 0, 0, 0, 0, 1],
        ],
        dtype=float,
    )


def _Q_ca_mat(dt: float, q: float) -> np.ndarray:
    """Discrete-time process-noise for constant-acceleration model."""
    t2, t3, t4, t5 = dt**2, dt**3, dt**4, dt**5
    return q * np.array(
        [
            [t5 / 20, 0, t4 / 8, 0, t3 / 6, 0],
            [0, t5 / 20, 0, t4 / 8, 0, t3 / 6],
            [t4 / 8, 0, t3 / 3, 0, t2 / 2, 0],
            [0, t4 / 8, 0, t3 / 3, 0, t2 / 2],
            [t3 / 6, 0, t2 / 2, 0, dt, 0],
            [0, t3 / 6, 0, t2 / 2, 0, dt],
        ],
        dtype=float,
    )


# Observation matrices: extract [north, east] from state
_H_CV: np.ndarray = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=float)
_H_CA: np.ndarray = np.array([[1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0]], dtype=float)


# ---------------------------------------------------------------------------
# Core Kalman step
# ---------------------------------------------------------------------------


def _kalman_update(
    x: np.ndarray,
    P: np.ndarray,
    H: np.ndarray,
    R: np.ndarray,
    z: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, float]:
    """Standard Kalman update.  Returns (x+, P+, likelihood)."""
    S = H @ P @ H.T + R
    # Joseph form for numerical stability
    try:
        K = np.linalg.solve(S.T, (P @ H.T).T).T
    except np.linalg.LinAlgError:
        K = P @ H.T @ np.linalg.pinv(S)
    innov = z - H @ x
    I_KH = np.eye(len(x)) - K @ H
    x_new = x + K @ innov
    P_new = I_KH @ P @ I_KH.T + K @ R @ K.T  # Joseph form
    # Floor diagonal to prevent degenerate covariances
    P_new = np.maximum(P_new, np.eye(len(x)) * _P_FLOOR * 1e-6)

    # Gaussian likelihood of the innovation
    det_S = max(float(np.linalg.det(S)), 1e-300)
    exp_val = float(-0.5 * innov @ np.linalg.solve(S, innov))
    n_z = len(z)
    likelihood = (
        (2 * np.pi) ** (-n_z / 2) * det_S ** (-0.5) * np.exp(max(exp_val, -500.0))
    )
    return x_new, P_new, max(likelihood, 1e-300)


# ---------------------------------------------------------------------------
# Full IMM cycle  (mix → predict → update → combine)
# ---------------------------------------------------------------------------


def _imm_step(
    state: _IMMState,
    z: np.ndarray,
    R: np.ndarray,
    dt: float,
) -> _IMMState:
    """One IMM cycle given a new position measurement z = [north_m, east_m]."""
    mu = state.mu
    M = _M

    # 1 ─ Mixing probabilities
    c = M.T @ mu  # (2,)  normalisers
    mu_mix = M * mu[:, None] / np.maximum(c[None, :], 1e-300)  # (2,2)
    # mu_mix[i, j] = weight of model-i contribution to model-j mixed input

    # 2 ─ Mixed initial conditions
    # CV mixed input (4-D): blend from CV-4 and CA-4 (first 4 of CA state)
    x_cv_4 = state.x_cv
    x_ca_4 = state.x_ca[:4]
    x0_cv = mu_mix[0, 0] * x_cv_4 + mu_mix[1, 0] * x_ca_4
    d0_cv = x_cv_4 - x0_cv
    d1_cv = x_ca_4 - x0_cv
    P0_cv = mu_mix[0, 0] * (state.P_cv + np.outer(d0_cv, d0_cv)) + mu_mix[1, 0] * (
        state.P_ca[:4, :4] + np.outer(d1_cv, d1_cv)
    )

    # CA mixed input (6-D): blend from CV-6 (zero-padded) and CA-6
    x_cv_6 = np.concatenate([state.x_cv, [0.0, 0.0]])
    P_cv_6 = np.zeros((6, 6))
    P_cv_6[:4, :4] = state.P_cv
    x0_ca = mu_mix[0, 1] * x_cv_6 + mu_mix[1, 1] * state.x_ca
    d0_ca = x_cv_6 - x0_ca
    d1_ca = state.x_ca - x0_ca
    P0_ca = mu_mix[0, 1] * (P_cv_6 + np.outer(d0_ca, d0_ca)) + mu_mix[1, 1] * (
        state.P_ca + np.outer(d1_ca, d1_ca)
    )

    # 3 ─ Predict each model forward
    F_cv = _F_cv(dt)
    x_cv_pred = F_cv @ x0_cv
    P_cv_pred = F_cv @ P0_cv @ F_cv.T + _Q_cv_mat(dt, _Q_CV)

    F_ca = _F_ca(dt)
    x_ca_pred = F_ca @ x0_ca
    P_ca_pred = F_ca @ P0_ca @ F_ca.T + _Q_ca_mat(dt, _Q_CA)

    # 4 ─ Update each model with measurement
    x_cv_upd, P_cv_upd, L_cv = _kalman_update(x_cv_pred, P_cv_pred, _H_CV, R, z)
    x_ca_upd, P_ca_upd, L_ca = _kalman_update(x_ca_pred, P_ca_pred, _H_CA, R, z)

    # 5 ─ Update mode probabilities
    c_new = np.array([c[0] * L_cv, c[1] * L_ca])
    mu_new = c_new / max(c_new.sum(), 1e-300)
    # Clip to avoid numerical collapse
    mu_new = np.clip(mu_new, 0.01, 0.99)
    mu_new /= mu_new.sum()

    return _IMMState(
        x_cv=x_cv_upd,
        P_cv=P_cv_upd,
        x_ca=x_ca_upd,
        P_ca=P_ca_upd,
        mu=mu_new,
        obs_lat=state.obs_lat,
        obs_lon=state.obs_lon,
        last_update=time.time(),
    )


# ---------------------------------------------------------------------------
# Prediction-only propagation (no measurement)
# ---------------------------------------------------------------------------


def advance_state(state: _IMMState, dt: float) -> _IMMState:
    """Propagate the filter state forward *dt* seconds without a measurement.

    Returns a new _IMMState at time t + dt.  The mode probabilities are
    kept fixed (no mixing without a measurement), and the covariances grow
    according to the process-noise model for each sub-filter.

    For accurate uncertainty estimates over long prediction horizons, call
    this function *incrementally* (e.g., 5 s per step) rather than once
    with a large dt — the process-noise matrix scales as dt⁴ for position
    and using small steps avoids numerical blow-up.
    """
    F_cv = _F_cv(dt)
    Q_cv = _Q_cv_mat(dt, _Q_CV)
    x_cv_p = F_cv @ state.x_cv
    P_cv_p = F_cv @ state.P_cv @ F_cv.T + Q_cv

    F_ca = _F_ca(dt)
    Q_ca = _Q_ca_mat(dt, _Q_CA)
    x_ca_p = F_ca @ state.x_ca
    P_ca_p = F_ca @ state.P_ca @ F_ca.T + Q_ca

    return _IMMState(
        x_cv=x_cv_p,
        P_cv=P_cv_p,
        x_ca=x_ca_p,
        P_ca=P_ca_p,
        mu=state.mu.copy(),
        obs_lat=state.obs_lat,
        obs_lon=state.obs_lon,
        last_update=state.last_update,
    )


def state_position(state: _IMMState) -> Tuple[float, float, float]:
    """Extract the IMM combined position estimate from a filter state.

    Returns (north_m, east_m, sigma_m) where sigma_m is 1-σ position
    uncertainty (RMS of north + east standard deviations).

    Uncertainty model
    -----------------
    When the CV (straight cruise) mode strongly dominates (μ_cv ≥ 0.7), only
    the CV covariance is used for σ.  The CA model receives far larger process
    noise during the 60-second ADS-B update interval (Q_ca ~ dt⁵), which
    inflates P_ca to km-scale in the prediction window even at small μ weights.
    That inflation is physically meaningful for *maneuvering* aircraft (high
    μ_ca) but misleading for cruise aircraft (high μ_cv) where it dominates
    the combined σ via the mixture formula.

    When μ_ca is significant (aircraft maneuvering), the full IMM mixture is
    used and the returned σ correctly widens, signalling reduced certainty.
    """
    mu = state.mu
    n_cv, e_cv = state.x_cv[0], state.x_cv[1]
    n_ca, e_ca = state.x_ca[0], state.x_ca[1]

    if mu[0] >= 0.7:
        # CV-dominant (cruise): position is essentially the CV estimate
        north_m = n_cv
        east_m = e_cv
        sigma_m = sqrt(max(state.P_cv[0, 0] + state.P_cv[1, 1], _P_FLOOR))
    else:
        # Maneuvering: use full IMM mixture
        north_m = mu[0] * n_cv + mu[1] * n_ca
        east_m = mu[0] * e_cv + mu[1] * e_ca
        sigma2_cv = (
            state.P_cv[0, 0]
            + state.P_cv[1, 1]
            + (n_cv - north_m) ** 2
            + (e_cv - east_m) ** 2
        )
        sigma2_ca = (
            state.P_ca[0, 0]
            + state.P_ca[1, 1]
            + (n_ca - north_m) ** 2
            + (e_ca - east_m) ** 2
        )
        sigma_m = sqrt(max(mu[0] * sigma2_cv + mu[1] * sigma2_ca, _P_FLOOR))

    return north_m, east_m, sigma_m


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def update_filter(
    icao24: str,
    flight: dict,
    obs_lat: float,
    obs_lon: float,
) -> _IMMState:
    """Update (or initialise) the per-aircraft IMM filter.

    Parameters
    ----------
    icao24 : str
        24-bit ICAO hex address used as the cache key.
    flight : dict
        Internal flight dict from _parse_opensky_flight / _parse_readsb_aircraft.
        Must contain 'latitude', 'longitude', 'speed' (km/h), 'direction' (°),
        and optionally 'position_source'.
    obs_lat, obs_lon : float
        Observer position (decimal degrees).

    Returns
    -------
    _IMMState
        Current filter state (ready for predict_position calls).
    """
    now = time.time()
    lat = float(flight["latitude"])
    lon = float(flight["longitude"])
    north_m, east_m = _to_enu(lat, lon, obs_lat, obs_lon)
    z = np.array([north_m, east_m], dtype=float)

    # Measurement noise by source
    src = (flight.get("position_source") or "").lower()
    if src == "adsb":
        r_var = _R_ADSB
    elif src in ("mlat", "other"):
        r_var = _R_OTHER
    else:
        r_var = _R_OPENSKY
    R = np.eye(2, dtype=float) * r_var

    existing: Optional[_IMMState] = _filters.get(icao24)

    if existing is None or (now - existing.last_update) > FILTER_TTL_S:
        # ── Cold start: initialise from reported speed + heading ─────────
        speed_ms = float(flight.get("speed") or 0) / 3.6  # km/h → m/s
        hdg_rad = radians(float(flight.get("direction") or 0))
        vn = speed_ms * cos(hdg_rad)
        ve = speed_ms * sin(hdg_rad)

        vel_var = (5.0) ** 2  # initial velocity uncertainty ±5 m/s ≈ ±18 km/h
        acc_var = (0.5) ** 2  # initial acceleration uncertainty ±0.5 m/s²

        x_cv = np.array([north_m, east_m, vn, ve], dtype=float)
        P_cv = np.diag([r_var, r_var, vel_var, vel_var])

        x_ca = np.array([north_m, east_m, vn, ve, 0.0, 0.0], dtype=float)
        P_ca = np.diag([r_var, r_var, vel_var, vel_var, acc_var, acc_var])

        state = _IMMState(
            x_cv=x_cv,
            P_cv=P_cv,
            x_ca=x_ca,
            P_ca=P_ca,
            mu=_MU_INIT.copy(),
            obs_lat=obs_lat,
            obs_lon=obs_lon,
            last_update=now,
        )
        _filters[icao24] = state
        return state

    # ── Warm update: run full IMM cycle ──────────────────────────────────
    dt = max(now - existing.last_update, 1.0)  # floor at 1 s
    state = _imm_step(existing, z, R, dt)
    _filters[icao24] = state
    return state


def extract_position(
    state: _IMMState,
    obs_lat: float,
    obs_lon: float,
) -> Tuple[float, float, float]:
    """Convert an _IMMState to (lat, lon, sigma_m) in geographic coordinates.

    Use after advance_state() to get the predicted position at the propagated
    time step.
    """
    north_m, east_m, sigma_m = state_position(state)
    lat_pred, lon_pred = _from_enu(north_m, east_m, obs_lat, obs_lon)
    return lat_pred, lon_pred, sigma_m


def angular_sigma(sigma_m: float, dist_m: float) -> float:
    """Convert 1-σ position uncertainty (metres) to angular uncertainty (degrees).

    Uses the small-angle approximation:  σ_angle = atan(σ_pos / dist).

    Parameters
    ----------
    sigma_m : float   1-σ position uncertainty in metres.
    dist_m  : float   Slant distance from observer to aircraft in metres.
    """
    if dist_m < 1.0:
        return 0.0
    return degrees(atan2(sigma_m, dist_m))


def cleanup_stale_filters(ttl: float = FILTER_TTL_S) -> int:
    """Remove cached filter states older than *ttl* seconds.  Returns count."""
    now = time.time()
    stale = [k for k, v in list(_filters.items()) if (now - v.last_update) > ttl]
    for k in stale:
        del _filters[k]
    if stale:
        logger.debug(f"[IMM] Cleaned {len(stale)} stale filter(s)")
    return len(stale)
