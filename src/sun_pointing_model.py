"""Session-adaptive pointing model for Sun centering.

Two-level learning
==================
Level 1 — within-session bias
    The Seestar's compass and level are hand-set each session and always wrong
    by some constant (delta_alt, delta_az).  Every successful centering event
    gives a clean observation of that offset.  We accumulate these with an
    online mean for the first few observations (equal weight, fast convergence),
    then switch to an EMA (alpha=0.15) to track slow mount drift (tripod
    creeping) without being destabilised by noise.

    The running estimate is applied pre-emptively to every GoTo command, so
    after two or three successful locks the Sun falls near frame-centre on the
    initial slew.

Level 2 — cross-session history
    At session end we record the final bias estimate in a rolling list of the
    last ten sessions.  On session start that history informs:
      * search_center_offset() — shifts the search grid toward where the Sun
        historically appears, so the first or second search step finds it.
      * search_radius_deg()    — sized to 2σ of historical spread; tight when
        the user always sets up the same way, wider when they don't.

    Crucially, the session bias is RESET TO ZERO at each session start.  We
    never carry last session's exact offset forward — the user repositioned
    the mount.  Only the Jacobian and session history survive.

Jacobian cache
==============
The 2×2 Jacobian (camera pixels per degree of GoTo) is a physical property
of the camera mounting and does not change between sessions.  After a
successful calibration we persist it to disk and reload it at the next
session start, validated against the current plate scale.  If the plate
scale has shifted more than 20% (different zoom / focus) we discard the
cache and recalibrate.

Disturbance detection
=====================
If a new correction disagrees with the running estimate by more than
disturbance_threshold_deg the mount was probably bumped.  We hard-reset the
session estimate to the new observation rather than polluting the running
average with stale data.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

from src import logger

# ── Tuning constants ──────────────────────────────────────────────────────────

_EMA_SWITCH_N: int = 5          # use running-mean weight for first N obs, then EMA
_EMA_ALPHA: float = 0.15        # EMA weight for new observations after switch
_HISTORY_MAX: int = 10          # rolling window of sessions to retain
_MIN_HISTORY_FOR_STATS: int = 3 # sessions needed before stats are meaningful
_PLATE_SCALE_TOLERANCE: float = 0.20  # 20% — Jacobian cache invalidation gate


# ── Helpers ───────────────────────────────────────────────────────────────────

def _population_std(values: List[float]) -> float:
    """Population standard deviation; returns 0.0 if fewer than 2 values."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _weighted_mean(values: List[float], weights: List[float]) -> float:
    total_w = sum(weights)
    if total_w == 0:
        return 0.0
    return sum(w * v for w, v in zip(weights, values)) / total_w


# ── PointingModel ─────────────────────────────────────────────────────────────

class PointingModel:
    """Adaptive pointing bias estimator with cross-session Jacobian cache."""

    def __init__(
        self,
        cache_path: str = "data/sun_pointing_model.json",
        disturbance_threshold_deg: float = 1.2,
    ) -> None:
        self._path = cache_path
        self._disturbance_threshold = float(disturbance_threshold_deg)
        self._lock = threading.Lock()

        # ── Session state (reset each session, never persisted) ───────────────
        self.session_bias_alt: float = 0.0
        self.session_bias_az: float = 0.0
        self.session_n_obs: int = 0

        # ── Persisted state ───────────────────────────────────────────────────
        self._jacobian: Optional[List[List[float]]] = None
        self._jacobian_inv: Optional[List[List[float]]] = None
        self._plate_scale: Optional[float] = None
        self._jacobian_saved_at: Optional[float] = None   # Unix timestamp

        # Each entry: {bias_alt, bias_az, n_obs, timestamp}
        self._sessions: List[Dict] = []

        self.load()

    # =========================================================================
    # Session lifecycle
    # =========================================================================

    def start_session(self) -> None:
        """Reset session-level state.  Call when the centering service starts."""
        with self._lock:
            self.session_bias_alt = 0.0
            self.session_bias_az = 0.0
            self.session_n_obs = 0
            mean_alt, mean_az = self._historical_mean()
            logger.info(
                "[PointingModel] Session started — %d historical session(s).  "
                "Expected search offset: (%.3f°, %.3f°)  radius: %.2f°",
                len(self._sessions),
                mean_alt,
                mean_az,
                self._search_radius_locked(),
            )

    def end_session(self) -> None:
        """Record bias to history and persist.  Call when the service stops."""
        with self._lock:
            if self.session_n_obs >= 2:
                record: Dict = {
                    "bias_alt": round(self.session_bias_alt, 5),
                    "bias_az": round(self.session_bias_az, 5),
                    "n_obs": self.session_n_obs,
                    "timestamp": time.time(),
                }
                self._sessions.append(record)
                self._sessions = self._sessions[-_HISTORY_MAX:]
                logger.info(
                    "[PointingModel] Session ended.  "
                    "Bias: (%.4f°, %.4f°) over %d obs.  "
                    "History: %d session(s).",
                    self.session_bias_alt,
                    self.session_bias_az,
                    self.session_n_obs,
                    len(self._sessions),
                )
                self._save()
            else:
                logger.info(
                    "[PointingModel] Session ended with <2 observations — "
                    "not appended to history."
                )

    # =========================================================================
    # Within-session bias
    # =========================================================================

    def biased_altaz(self, sun_alt: float, sun_az: float) -> Tuple[float, float]:
        """Return (alt, az) with current session bias applied.

        For fresh ephemeris GoTo commands (ACQUIRE, RECOVER).
        Correction GoTos inside CENTER/TRACK are relative to the current
        mount position, so they do NOT call this method.
        """
        with self._lock:
            return (
                float(sun_alt) + self.session_bias_alt,
                float(sun_az) + self.session_bias_az,
            )

    def record_correction(
        self,
        mount_alt: float,
        mount_az: float,
        ephem_alt: float,
        ephem_az: float,
    ) -> None:
        """Update session bias from a successful centering event.

        mount_alt/az: actual pointing of the mount when Sun is centred
                      (read from get_position() while disk is on-target).
        ephem_alt/az: Sun ephemeris at that same moment.

        Bias = mount_position − ephemeris (the mount's systematic pointing error).
        """
        new_alt = float(mount_alt) - float(ephem_alt)
        new_az = float(mount_az) - float(ephem_az)

        with self._lock:
            n = self.session_n_obs

            # ── Disturbance detection ─────────────────────────────────────────
            if n >= 2:
                dev = max(
                    abs(new_alt - self.session_bias_alt),
                    abs(new_az - self.session_bias_az),
                )
                if dev > self._disturbance_threshold:
                    logger.warning(
                        "[PointingModel] Pointing disturbance: deviation %.3f° > "
                        "threshold %.2f° — resetting session bias to new observation",
                        dev,
                        self._disturbance_threshold,
                    )
                    self.session_bias_alt = new_alt
                    self.session_bias_az = new_az
                    self.session_n_obs = 1
                    return

            # ── Update estimate ───────────────────────────────────────────────
            self.session_n_obs += 1
            n = self.session_n_obs

            if n <= _EMA_SWITCH_N:
                # Running mean: every observation has equal weight.
                self.session_bias_alt += (new_alt - self.session_bias_alt) / n
                self.session_bias_az += (new_az - self.session_bias_az) / n
            else:
                # EMA: tracks slow mount drift while damping noise.
                self.session_bias_alt = (
                    _EMA_ALPHA * new_alt
                    + (1.0 - _EMA_ALPHA) * self.session_bias_alt
                )
                self.session_bias_az = (
                    _EMA_ALPHA * new_az
                    + (1.0 - _EMA_ALPHA) * self.session_bias_az
                )

            logger.info(
                "[PointingModel] Correction obs #%d: "
                "raw=(%.4f°, %.4f°)  estimate=(%.4f°, %.4f°)",
                n,
                new_alt,
                new_az,
                self.session_bias_alt,
                self.session_bias_az,
            )

    # =========================================================================
    # Search geometry
    # =========================================================================

    def search_center_offset(self) -> Tuple[float, float]:
        """Residual offset to centre the search grid, relative to the biased GoTo.

        Logic
        -----
        The biased GoTo already adds session_bias to the ephemeris.
        The historical mean tells us where the bias usually falls.
        The residual = hist_mean − session_bias is where we expect to find
        the Sun *relative to where we just pointed*.

        At session start (session_bias=0): returns the historical mean.
          → Search starts where the Sun usually is.
        After first lock (session_bias ≈ hist_mean): returns ≈ (0, 0).
          → The GoTo already landed on target; no offset search needed.
        """
        with self._lock:
            mean_alt, mean_az = self._historical_mean()
            return (
                mean_alt - self.session_bias_alt,
                mean_az - self.session_bias_az,
            )

    def search_radius_deg(self) -> float:
        """Suggested search grid outer radius (degrees)."""
        with self._lock:
            return self._search_radius_locked()

    def _search_radius_locked(self) -> float:
        """search_radius_deg() implementation; must be called with _lock held."""
        if len(self._sessions) < _MIN_HISTORY_FOR_STATS:
            return 0.8  # safe default when history is sparse
        recent = self._sessions[-6:]
        std_alt = _population_std([s["bias_alt"] for s in recent])
        std_az = _population_std([s["bias_az"] for s in recent])
        return float(max(0.4, min(2.5, 2.0 * max(std_alt, std_az))))

    def session_summary(self) -> Dict:
        """Return all model state for get_status()."""
        with self._lock:
            mean_alt, mean_az = self._historical_mean()
            center_dalt = mean_alt - self.session_bias_alt
            center_daz = mean_az - self.session_bias_az
            return {
                "session_bias_alt": round(self.session_bias_alt, 5),
                "session_bias_az": round(self.session_bias_az, 5),
                "session_n_obs": self.session_n_obs,
                "historical_sessions": len(self._sessions),
                "historical_mean_alt": round(mean_alt, 5),
                "historical_mean_az": round(mean_az, 5),
                "search_center_dalt": round(center_dalt, 5),
                "search_center_daz": round(center_daz, 5),
                "search_radius_deg": round(self._search_radius_locked(), 3),
                "jacobian_cached": self._jacobian is not None,
                "jacobian_saved_at": self._jacobian_saved_at,
                "cached_plate_scale": self._plate_scale,
            }

    # =========================================================================
    # Jacobian cache
    # =========================================================================

    def save_jacobian(
        self,
        j: List[List[float]],
        j_inv: List[List[float]],
        plate_scale: float,
    ) -> None:
        """Persist Jacobian and plate scale to disk immediately."""
        with self._lock:
            self._jacobian = j
            self._jacobian_inv = j_inv
            self._plate_scale = float(plate_scale)
            self._jacobian_saved_at = time.time()
            self._save()
            logger.info(
                "[PointingModel] Jacobian saved (plate_scale=%.6f °/px)", plate_scale
            )

    def cached_jacobian(
        self,
        current_plate_scale: Optional[float] = None,
    ) -> Optional[Tuple[List[List[float]], List[List[float]], float]]:
        """Return (J, J_inv, plate_scale) if the cache is valid, else None.

        Validates against current_plate_scale (if provided) using a 20%
        tolerance.  A mismatch means the optical configuration changed
        (different zoom / focus), so we discard the cache.
        """
        with self._lock:
            if (
                self._jacobian is None
                or self._jacobian_inv is None
                or self._plate_scale is None
                or self._plate_scale <= 0
            ):
                logger.info("[PointingModel] No cached Jacobian.")
                return None

            if current_plate_scale is not None and current_plate_scale > 0:
                ratio = abs(current_plate_scale - self._plate_scale) / self._plate_scale
                if ratio > _PLATE_SCALE_TOLERANCE:
                    logger.info(
                        "[PointingModel] Cached Jacobian rejected: plate scale "
                        "changed %.1f%% (%.6f → %.6f °/px) — will recalibrate",
                        ratio * 100.0,
                        self._plate_scale,
                        current_plate_scale,
                    )
                    return None

            age_h = (
                (time.time() - self._jacobian_saved_at) / 3600.0
                if self._jacobian_saved_at
                else 0.0
            )
            logger.info(
                "[PointingModel] Cached Jacobian accepted (age %.1f h, "
                "plate_scale=%.6f °/px)",
                age_h,
                self._plate_scale,
            )
            return (self._jacobian, self._jacobian_inv, self._plate_scale)

    # =========================================================================
    # Persistence
    # =========================================================================

    def load(self) -> None:
        """Load persisted state from disk.  Non-fatal on missing / corrupt file."""
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._jacobian = data.get("jacobian")
            self._jacobian_inv = data.get("jacobian_inv")
            raw_ps = data.get("plate_scale")
            self._plate_scale = float(raw_ps) if raw_ps is not None else None
            self._jacobian_saved_at = data.get("jacobian_saved_at")
            self._sessions = data.get("sessions", [])[-_HISTORY_MAX:]
            logger.info(
                "[PointingModel] Loaded from %s: %d session(s), jacobian=%s",
                self._path,
                len(self._sessions),
                "present" if self._jacobian else "absent",
            )
        except FileNotFoundError:
            logger.info(
                "[PointingModel] No cache file at %s — starting fresh.", self._path
            )
        except Exception as exc:
            logger.warning(
                "[PointingModel] Failed to load %s: %s — starting fresh.",
                self._path,
                exc,
            )

    def _save(self) -> None:
        """Atomic JSON write (must be called with _lock held)."""
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        data = {
            "jacobian": self._jacobian,
            "jacobian_inv": self._jacobian_inv,
            "plate_scale": self._plate_scale,
            "jacobian_saved_at": self._jacobian_saved_at,
            "sessions": self._sessions,
        }
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            logger.warning("[PointingModel] Failed to save %s: %s", self._path, exc)

    # =========================================================================
    # Private
    # =========================================================================

    def _historical_mean(self) -> Tuple[float, float]:
        """Linearly-weighted mean bias from session history (recent = higher weight)."""
        sessions = self._sessions
        if not sessions:
            return 0.0, 0.0
        weights = [float(i + 1) for i in range(len(sessions))]
        alts = [s["bias_alt"] for s in sessions]
        azs = [s["bias_az"] for s in sessions]
        return _weighted_mean(alts, weights), _weighted_mean(azs, weights)
