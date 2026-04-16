"""Sun acquisition, centering, and tracking service — v2.

GoTo-correction model (replaces v1 rate-command PI controller):
  - Coarse GoTo from ephemeris.
  - 2×2 Jacobian calibration (px/deg) via two reference GoTo probes.
  - Iterative Jacobian GoTo correction loop (no PI gains, no rate commands,
    no axis-direction probing).
  - Sidereal tracking enabled in TRACK; periodic GoTo refresh handles solar
    drift relative to sidereal (~0.0015°/min).
  - Cloud-aware RECOVER: follows ephemeris silently without grid search when
    the frame is dark; resumes CENTER (not full re-calibration) when flux
    returns, because the Jacobian is mount-camera geometry and remains valid.

State machine:
  ACQUIRE → CALIBRATE → CENTER → TRACK
     ↑                               ↓ (drift or disk loss)
     └──────────── RECOVER ──────────┘
     (FAIL_SAFE on unrecoverable errors)

Adapter interface changes from v1:
  Added:   get_position()  — reads live mount alt/az
           set_tracking()  — enables/disables sidereal tracking
  Removed: move_axis()     — no longer used (GoTo only)
           get_max_move_rate() — no longer used
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from src import logger

# Mean solar angular radius (degrees).  Used to derive plate scale from the
# detected disk radius in pixels.
SUN_ANGULAR_RADIUS_DEG: float = 0.2655


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

@dataclass
class SunCenteringAdapter:
    """Callbacks that isolate this service from Flask route globals."""

    is_scope_connected: Callable[[], bool]
    is_alpaca_connected: Callable[[], bool]
    get_viewing_mode: Callable[[], Optional[str]]
    get_sun_altaz: Callable[[], Optional[Tuple[float, float]]]
    goto_altaz: Callable[[float, float], Dict[str, Any]]
    is_slewing: Callable[[], bool]
    stop_axes: Callable[[], Dict[str, Any]]
    get_detector_status: Callable[[], Dict[str, Any]]
    get_position: Callable[[], Dict[str, float]]   # → {alt, az, ra, dec}
    set_tracking: Callable[[bool], Dict[str, Any]]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class SunCenteringSettings:
    tick_hz: float = 4.0
    min_sun_alt_deg: float = 8.0

    # ── Acquire ─────────────────────────────────────────────────────────────
    acquire_slew_timeout_s: float = 30.0
    acquire_settle_s: float = 2.0
    # Rings of alt/az offsets (degrees) for the fallback search grid.
    # Each radius produces 4 cardinal points; default = 8 search positions.
    acquire_search_radii_deg: Tuple[float, ...] = (0.4, 0.8)
    acquire_search_settle_s: float = 1.5
    precheck_busy_timeout_s: float = 60.0

    # ── Calibrate ───────────────────────────────────────────────────────────
    probe_deg: float = 0.10          # GoTo offset for each Jacobian probe
    probe_slew_timeout_s: float = 15.0
    probe_settle_s: float = 1.5
    min_jacobian_det: float = 0.01   # determinant gate; below this → retry
    max_cal_retries: int = 1

    # ── Center ──────────────────────────────────────────────────────────────
    tolerance_radii: float = 0.12    # converged when error_radii < this
    max_center_iters: int = 5
    center_slew_timeout_s: float = 15.0
    center_settle_s: float = 1.5

    # ── Track ───────────────────────────────────────────────────────────────
    track_refresh_s: float = 15.0
    drift_threshold_radii: float = 0.08
    lock_lost_grace_s: float = 4.0
    track_correction_slew_timeout_s: float = 10.0
    track_correction_settle_s: float = 1.0

    # ── Recover ─────────────────────────────────────────────────────────────
    recover_slew_timeout_s: float = 25.0
    recover_settle_s: float = 2.0
    cloud_floor_mean: float = 4.0        # flux below this → cloud suspected
    cloud_track_interval_s: float = 30.0  # ephemeris GoTo cadence during cloud
    cloud_wait_max_s: float = 300.0      # give up after this long in cloud


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class SunCenteringService:
    """Background service that acquires, calibrates, centers, and tracks the Sun."""

    STATE_ACQUIRE = "acquire"
    STATE_CALIBRATE = "calibrate"
    STATE_CENTER = "center"
    STATE_TRACK = "track"
    STATE_RECOVER = "recover"
    STATE_FAIL_SAFE = "fail_safe"
    STATE_STOPPED = "stopped"

    def __init__(
        self,
        adapter: SunCenteringAdapter,
        settings: Optional[SunCenteringSettings] = None,
    ) -> None:
        self.adapter = adapter
        self.settings = settings or SunCenteringSettings()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

        # ── State ────────────────────────────────────────────────────────────
        self.state = self.STATE_STOPPED
        self.state_message = "Initializing"
        self.state_changed_at = time.time()
        self.started_at: Optional[float] = None
        self._phase: str = "initial"

        # ── Timing helpers ───────────────────────────────────────────────────
        self._slew_deadline_mono: float = 0.0
        self._settle_until_mono: float = 0.0
        self._precheck_busy_deadline_mono: float = 0.0

        # ── Detector snapshot (refreshed every tick) ─────────────────────────
        self.disk_detected: bool = False
        self.disk_cx: Optional[float] = None   # centroid x in pixels
        self.disk_cy: Optional[float] = None   # centroid y in pixels
        self.disk_radius: Optional[float] = None
        self.disk_eu_px: Optional[float] = None  # cx − image_cx
        self.disk_ev_px: Optional[float] = None  # cy − image_cy
        self.error_radii: Optional[float] = None  # hypot(eu,ev)/radius
        self.disk_info: Optional[Dict[str, Any]] = None
        self.center_flux_core_mean: Optional[float] = None
        self.plate_scale_deg_per_px: Optional[float] = None  # derived on first lock

        # ── Calibration (Jacobian) ───────────────────────────────────────────
        # J maps (dalt, daz) in degrees to (du, dv) in pixels.
        # J_inv maps (du, dv) in pixels to (dalt, daz) in degrees.
        self._jacobian_valid: bool = False
        self._j: Optional[List[List[float]]] = None     # [[du/dalt, du/daz],[dv/dalt,dv/daz]]
        self._j_inv: Optional[List[List[float]]] = None
        self._jacobian_age_s: float = 0.0  # seconds since last calibration
        self._jacobian_at: Optional[float] = None  # monotonic timestamp

        # Calibration working variables
        self._cal_ref_alt: float = 0.0
        self._cal_ref_az: float = 0.0
        self._cal_ref_cx: float = 0.0
        self._cal_ref_cy: float = 0.0
        self._cal_j_col_alt: Optional[List[float]] = None
        self._cal_j_col_az: Optional[List[float]] = None
        self._cal_retry: int = 0

        # ── Acquire search ───────────────────────────────────────────────────
        self._search_offsets: List[Tuple[float, float]] = []
        self._search_idx: int = 0
        self._search_sun_alt: float = 0.0   # ephemeris snapshot at search start
        self._search_sun_az: float = 0.0

        # ── Center ───────────────────────────────────────────────────────────
        self._center_iter_count: int = 0
        self._center_no_disk_ticks: int = 0

        # ── Track ────────────────────────────────────────────────────────────
        self._next_refresh_mono: float = 0.0
        self._lock_lost_until_mono: float = 0.0

        # ── Recover / cloud ──────────────────────────────────────────────────
        self._cloud_start_mono: float = 0.0
        self._cloud_next_goto_mono: float = 0.0
        self.recovery_attempts: int = 0

        # ── Diagnostics ──────────────────────────────────────────────────────
        self.last_command: Dict[str, Any] = {}
        self.last_error: Optional[str] = None

    # =========================================================================
    # Public API
    # =========================================================================

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self.started_at = time.time()
            self._precheck_busy_deadline_mono = (
                time.monotonic() + float(self.settings.precheck_busy_timeout_s)
            )
            self.state = self.STATE_ACQUIRE
            self._phase = "initial"
            self.state_message = "Starting"
            self.state_changed_at = time.time()
            self._thread = threading.Thread(
                target=self._run_loop, name="sun-centering-v2", daemon=True
            )
            self._thread.start()
            logger.info("[SunCenter] Service started (v2 GoTo-correction model)")

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop_event.set()
        try:
            self.adapter.stop_axes()
        except Exception as exc:
            logger.debug("[SunCenter] stop_axes on stop failed: %s", exc)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        with self._lock:
            self.state = self.STATE_STOPPED
            self.state_message = "Stopped"
            self.state_changed_at = time.time()
            logger.info("[SunCenter] Service stopped")

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def recenter(self) -> bool:
        """Re-enter ACQUIRE, keeping existing Jacobian if valid (skip CALIBRATE)."""
        with self._lock:
            if not self._running:
                return False
            self.recovery_attempts = 0
            self._center_iter_count = 0
            self._transition(self.STATE_ACQUIRE, "Manual recenter requested")
            self._phase = "initial"
            return True

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        s = self.settings
        _float_clamp = lambda k, lo, hi: setattr(
            s, k, float(max(lo, min(hi, patch[k])))
        ) if k in patch else None
        _int_clamp = lambda k, lo, hi: setattr(
            s, k, int(max(lo, min(hi, int(patch[k]))))
        ) if k in patch else None

        _float_clamp("min_sun_alt_deg", 0.0, 45.0)
        _float_clamp("acquire_settle_s", 0.5, 10.0)
        _float_clamp("acquire_search_settle_s", 0.5, 10.0)
        _float_clamp("precheck_busy_timeout_s", 10.0, 300.0)
        _float_clamp("probe_deg", 0.03, 0.5)
        _float_clamp("probe_settle_s", 0.5, 5.0)
        _float_clamp("min_jacobian_det", 0.001, 1.0)
        _float_clamp("tolerance_radii", 0.02, 0.8)
        _int_clamp("max_center_iters", 2, 20)
        _float_clamp("center_settle_s", 0.5, 5.0)
        _float_clamp("track_refresh_s", 5.0, 120.0)
        _float_clamp("drift_threshold_radii", 0.02, 0.5)
        _float_clamp("lock_lost_grace_s", 1.0, 30.0)
        _float_clamp("recover_settle_s", 0.5, 10.0)
        _float_clamp("cloud_floor_mean", 0.0, 50.0)
        _float_clamp("cloud_track_interval_s", 5.0, 120.0)
        _float_clamp("cloud_wait_max_s", 30.0, 1800.0)

        return {
            "min_sun_alt_deg": s.min_sun_alt_deg,
            "acquire_settle_s": s.acquire_settle_s,
            "probe_deg": s.probe_deg,
            "probe_settle_s": s.probe_settle_s,
            "tolerance_radii": s.tolerance_radii,
            "max_center_iters": s.max_center_iters,
            "center_settle_s": s.center_settle_s,
            "track_refresh_s": s.track_refresh_s,
            "drift_threshold_radii": s.drift_threshold_radii,
            "lock_lost_grace_s": s.lock_lost_grace_s,
            "cloud_floor_mean": s.cloud_floor_mean,
            "cloud_track_interval_s": s.cloud_track_interval_s,
            "cloud_wait_max_s": s.cloud_wait_max_s,
        }

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            age = round(max(0.0, time.time() - self.started_at), 1) if self.started_at else 0.0
            j_age = None
            if self._jacobian_at is not None:
                j_age = round(time.monotonic() - self._jacobian_at, 1)
            return {
                # Core
                "running": self._running,
                "state": self.state,
                "phase": self._phase,
                "message": self.state_message,
                "state_changed_at": self.state_changed_at,
                "uptime_s": age,
                # Disk / error
                "disk_detected": bool(self.disk_detected),
                "disk_info": self.disk_info,
                "error_radii": (
                    None if self.error_radii is None else round(self.error_radii, 4)
                ),
                "error_u_px": (
                    None if self.disk_eu_px is None else round(self.disk_eu_px, 2)
                ),
                "error_v_px": (
                    None if self.disk_ev_px is None else round(self.disk_ev_px, 2)
                ),
                "plate_scale_deg_per_px": (
                    None
                    if self.plate_scale_deg_per_px is None
                    else round(self.plate_scale_deg_per_px, 6)
                ),
                # Calibration
                "jacobian_valid": bool(self._jacobian_valid),
                "jacobian_age_s": j_age,
                "jacobian": self._j,
                "jacobian_inv": self._j_inv,
                # Settings snapshot
                "tolerance_radii": self.settings.tolerance_radii,
                "track_refresh_s": self.settings.track_refresh_s,
                "cloud_floor_mean": self.settings.cloud_floor_mean,
                # Counters
                "recovery_attempts": int(self.recovery_attempts),
                "center_iter_count": int(self._center_iter_count),
                "search_index": int(self._search_idx),
                # Diagnostics
                "last_command": dict(self.last_command),
                "last_error": self.last_error,
                # Flux
                "center_flux_core_mean": (
                    None
                    if self.center_flux_core_mean is None
                    else round(self.center_flux_core_mean, 2)
                ),
            }

    # =========================================================================
    # Main loop
    # =========================================================================

    def _run_loop(self) -> None:
        period = 1.0 / max(0.5, float(self.settings.tick_hz))
        while not self._stop_event.is_set():
            t0 = time.monotonic()
            try:
                self._tick_once()
            except Exception as exc:
                self.last_error = str(exc)
                logger.error("[SunCenter] Tick failed: %s", exc, exc_info=True)
                self._enter_fail_safe("Controller exception")
            elapsed = time.monotonic() - t0
            self._stop_event.wait(max(0.02, period - elapsed))

    def _tick_once(self) -> None:
        if not self._running:
            return

        # Refresh detector snapshot every tick.
        det = self._safe_detector_status()
        self._refresh_disk_snapshot(det)

        # Hard connection guard.
        if not self.adapter.is_scope_connected() or not self.adapter.is_alpaca_connected():
            self._enter_fail_safe("Scope/ALPACA disconnected")
            return

        # Solar-mode guard.
        mode = (self.adapter.get_viewing_mode() or "").strip().lower()
        if mode not in {"sun", "solar"}:
            self._enter_fail_safe("Solar mode required")
            return

        state = self.state
        if state == self.STATE_ACQUIRE:
            self._handle_acquire()
        elif state == self.STATE_CALIBRATE:
            self._handle_calibrate()
        elif state == self.STATE_CENTER:
            self._handle_center()
        elif state == self.STATE_TRACK:
            self._handle_track()
        elif state == self.STATE_RECOVER:
            self._handle_recover()
        elif state == self.STATE_FAIL_SAFE:
            pass  # inert until manual recenter()

    # =========================================================================
    # ACQUIRE
    # =========================================================================

    def _build_search_offsets(self) -> List[Tuple[float, float]]:
        offsets: List[Tuple[float, float]] = []
        for r in self.settings.acquire_search_radii_deg:
            offsets += [(r, 0.0), (-r, 0.0), (0.0, r), (0.0, -r)]
        return offsets

    def _handle_acquire(self) -> None:
        now = time.monotonic()

        if self._phase == "initial":
            sun = self.adapter.get_sun_altaz()
            if not sun:
                self._enter_fail_safe("Unable to compute Sun coordinates")
                return
            sun_alt, sun_az = sun
            if float(sun_alt) < float(self.settings.min_sun_alt_deg):
                self._enter_fail_safe(
                    f"Sun altitude {sun_alt:.1f}° below minimum {self.settings.min_sun_alt_deg:.1f}°"
                )
                return

            # Busy-mount guard: if mount is slewing before we even issue a goto,
            # wait up to precheck_busy_timeout_s before giving up.
            if self.adapter.is_slewing():
                if now >= self._precheck_busy_deadline_mono:
                    self._enter_fail_safe(
                        f"Mount stuck slewing at startup for "
                        f">{self.settings.precheck_busy_timeout_s:.0f}s"
                    )
                else:
                    self.state_message = "Waiting for mount to stop slewing before GoTo"
                return

            self._goto_clamped(sun_alt, sun_az)
            self._slew_deadline_mono = now + self.settings.acquire_slew_timeout_s
            self._phase = "slewing"
            self.state_message = "Coarse GoTo to Sun ephemeris"

        elif self._phase == "slewing":
            if self._wait_slew("acquire: coarse slew"):
                return
            self._settle_until_mono = now + self.settings.acquire_settle_s
            self._phase = "settling"
            self.state_message = "Coarse GoTo complete; settling"

        elif self._phase == "settling":
            if now < self._settle_until_mono:
                return
            self._phase = "assess"

        elif self._phase == "assess":
            if self.disk_detected:
                self._on_disk_found_in_acquire()
                return
            # Disk not found at ephemeris — start grid search.
            logger.info("[SunCenter] Disk not found at ephemeris; starting search grid")
            sun = self.adapter.get_sun_altaz()
            if not sun:
                self._enter_fail_safe("Sun coordinates unavailable for search")
                return
            self._search_sun_alt, self._search_sun_az = sun
            self._search_offsets = self._build_search_offsets()
            self._search_idx = 0
            self._phase = "search_step"

        elif self._phase == "search_step":
            if self._search_idx >= len(self._search_offsets):
                self._enter_fail_safe(
                    f"Sun not found after {len(self._search_offsets)}-point grid search"
                )
                return
            dalt, daz = self._search_offsets[self._search_idx]
            tgt_alt = self._search_sun_alt + dalt
            tgt_az = self._search_sun_az + daz
            self._goto_clamped(tgt_alt, tgt_az)
            self._slew_deadline_mono = now + self.settings.acquire_slew_timeout_s
            self._phase = "search_slewing"
            self.state_message = (
                f"Grid search step {self._search_idx + 1}/{len(self._search_offsets)} "
                f"(Δalt={dalt:+.2f}°, Δaz={daz:+.2f}°)"
            )

        elif self._phase == "search_slewing":
            if self._wait_slew("acquire: search slew"):
                return
            self._settle_until_mono = now + self.settings.acquire_search_settle_s
            self._phase = "search_settling"

        elif self._phase == "search_settling":
            if now < self._settle_until_mono:
                return
            self._phase = "search_assess"

        elif self._phase == "search_assess":
            if self.disk_detected:
                self._on_disk_found_in_acquire()
                return
            self._search_idx += 1
            self._phase = "search_step"

    def _on_disk_found_in_acquire(self) -> None:
        if self._jacobian_valid:
            logger.info(
                "[SunCenter] Disk found in ACQUIRE; Jacobian valid — skipping CALIBRATE"
            )
            self._center_iter_count = 0
            self._center_no_disk_ticks = 0
            self._transition(self.STATE_CENTER, "Disk acquired; using cached Jacobian")
            self._phase = "check"
        else:
            logger.info("[SunCenter] Disk found in ACQUIRE — entering CALIBRATE")
            self._transition(self.STATE_CALIBRATE, "Disk acquired; calibrating Jacobian")
            self._phase = "record_ref"

    # =========================================================================
    # CALIBRATE
    # =========================================================================

    def _handle_calibrate(self) -> None:
        now = time.monotonic()

        if self._phase == "record_ref":
            if not self.disk_detected:
                # Disk was there a moment ago (we just came from ACQUIRE).
                # Give it one grace tick before bailing.
                self._phase = "record_ref_retry"
                return

            pos = self._safe_get_position()
            self._cal_ref_alt = pos.get("alt") or self._last_sun_alt()
            self._cal_ref_az = pos.get("az") or self._last_sun_az()
            self._cal_ref_cx = float(self.disk_cx or 0.0)
            self._cal_ref_cy = float(self.disk_cy or 0.0)
            self._cal_j_col_alt = None
            self._cal_j_col_az = None
            self._cal_retry = 0
            logger.info(
                "[SunCenter] Calibrate: ref alt=%.4f az=%.4f cx=%.1f cy=%.1f",
                self._cal_ref_alt, self._cal_ref_az, self._cal_ref_cx, self._cal_ref_cy,
            )
            self._phase = "alt_probe"

        elif self._phase == "record_ref_retry":
            if not self.disk_detected:
                self._transition(self.STATE_ACQUIRE, "Disk lost before calibration start")
                self._phase = "initial"
                return
            self._phase = "record_ref"

        # ── Altitude probe ───────────────────────────────────────────────────
        elif self._phase == "alt_probe":
            self.state_message = "Calibrate: altitude probe (+{:.2f}°)".format(
                self.settings.probe_deg
            )
            self._goto_clamped(
                self._cal_ref_alt + self.settings.probe_deg, self._cal_ref_az
            )
            self._slew_deadline_mono = now + self.settings.probe_slew_timeout_s
            self._phase = "alt_probe_slew"

        elif self._phase == "alt_probe_slew":
            if self._wait_slew("calibrate: alt probe"):
                return
            self._settle_until_mono = now + self.settings.probe_settle_s
            self._phase = "alt_probe_settle"

        elif self._phase == "alt_probe_settle":
            if now < self._settle_until_mono:
                return
            self._phase = "alt_probe_sample"

        elif self._phase == "alt_probe_sample":
            if not self.disk_detected:
                if self._cal_retry < self.settings.max_cal_retries:
                    self._cal_retry += 1
                    logger.warning(
                        "[SunCenter] Disk lost on alt probe sample; returning to ref (retry %d)",
                        self._cal_retry,
                    )
                    self._goto_clamped(self._cal_ref_alt, self._cal_ref_az)
                    self._slew_deadline_mono = now + self.settings.probe_slew_timeout_s
                    self._phase = "alt_probe_retry_slew"
                else:
                    logger.warning("[SunCenter] Alt probe: disk lost after retries → ACQUIRE")
                    self._jacobian_valid = False
                    self._transition(self.STATE_ACQUIRE, "Disk lost during alt calibration probe")
                    self._phase = "initial"
                return
            self._cal_j_col_alt = [
                (float(self.disk_cx) - self._cal_ref_cx) / self.settings.probe_deg,
                (float(self.disk_cy) - self._cal_ref_cy) / self.settings.probe_deg,
            ]
            logger.debug(
                "[SunCenter] Alt probe: J_col=[%.3f, %.3f]",
                *self._cal_j_col_alt,
            )
            self._phase = "alt_return"

        elif self._phase == "alt_probe_retry_slew":
            if self._wait_slew("calibrate: alt probe retry return"):
                return
            self._settle_until_mono = now + self.settings.probe_settle_s
            self._phase = "alt_probe_retry_settle"

        elif self._phase == "alt_probe_retry_settle":
            if now < self._settle_until_mono:
                return
            self._phase = "alt_probe"   # try probe again

        # ── Return from altitude probe ────────────────────────────────────────
        elif self._phase == "alt_return":
            self._goto_clamped(self._cal_ref_alt, self._cal_ref_az)
            self._slew_deadline_mono = now + self.settings.probe_slew_timeout_s
            self._phase = "alt_return_slew"

        elif self._phase == "alt_return_slew":
            if self._wait_slew("calibrate: alt return"):
                return
            self._settle_until_mono = now + self.settings.probe_settle_s
            self._phase = "alt_return_settle"

        elif self._phase == "alt_return_settle":
            if now < self._settle_until_mono:
                return
            self._phase = "az_probe"

        # ── Azimuth probe ─────────────────────────────────────────────────────
        elif self._phase == "az_probe":
            self.state_message = "Calibrate: azimuth probe (+{:.2f}°)".format(
                self.settings.probe_deg
            )
            self._goto_clamped(
                self._cal_ref_alt, self._cal_ref_az + self.settings.probe_deg
            )
            self._slew_deadline_mono = now + self.settings.probe_slew_timeout_s
            self._phase = "az_probe_slew"

        elif self._phase == "az_probe_slew":
            if self._wait_slew("calibrate: az probe"):
                return
            self._settle_until_mono = now + self.settings.probe_settle_s
            self._phase = "az_probe_settle"

        elif self._phase == "az_probe_settle":
            if now < self._settle_until_mono:
                return
            self._phase = "az_probe_sample"

        elif self._phase == "az_probe_sample":
            if not self.disk_detected:
                logger.warning("[SunCenter] Disk lost on az probe; returning to ref → ACQUIRE")
                self._jacobian_valid = False
                self._goto_clamped(self._cal_ref_alt, self._cal_ref_az)
                self._transition(self.STATE_ACQUIRE, "Disk lost during az calibration probe")
                self._phase = "initial"
                return
            self._cal_j_col_az = [
                (float(self.disk_cx) - self._cal_ref_cx) / self.settings.probe_deg,
                (float(self.disk_cy) - self._cal_ref_cy) / self.settings.probe_deg,
            ]
            logger.debug(
                "[SunCenter] Az probe: J_col=[%.3f, %.3f]",
                *self._cal_j_col_az,
            )
            self._phase = "az_return"

        # ── Return from azimuth probe ─────────────────────────────────────────
        elif self._phase == "az_return":
            self._goto_clamped(self._cal_ref_alt, self._cal_ref_az)
            self._slew_deadline_mono = now + self.settings.probe_slew_timeout_s
            self._phase = "az_return_slew"

        elif self._phase == "az_return_slew":
            if self._wait_slew("calibrate: az return"):
                return
            self._settle_until_mono = now + self.settings.probe_settle_s
            self._phase = "az_return_settle"

        elif self._phase == "az_return_settle":
            if now < self._settle_until_mono:
                return
            self._phase = "compute"

        # ── Compute Jacobian ──────────────────────────────────────────────────
        elif self._phase == "compute":
            self._compute_jacobian()

    def _compute_jacobian(self) -> None:
        col_alt = self._cal_j_col_alt
        col_az = self._cal_j_col_az
        if col_alt is None or col_az is None:
            self._transition(self.STATE_ACQUIRE, "Jacobian columns missing — restart")
            self._phase = "initial"
            return

        # J = [[du/dalt, du/daz],
        #      [dv/dalt, dv/daz]]
        j00, j10 = col_alt[0], col_alt[1]  # alt column
        j01, j11 = col_az[0],  col_az[1]   # az column
        det = j00 * j11 - j01 * j10

        logger.info(
            "[SunCenter] Jacobian: [[%.3f, %.3f], [%.3f, %.3f]]  det=%.4f",
            j00, j01, j10, j11, det,
        )

        if abs(det) < float(self.settings.min_jacobian_det):
            logger.warning(
                "[SunCenter] Jacobian determinant %.4f below threshold %.4f — "
                "axes may be near-parallel in image; returning to ACQUIRE",
                det, self.settings.min_jacobian_det,
            )
            self._jacobian_valid = False
            self._transition(self.STATE_ACQUIRE, "Jacobian determinant too small — recalibrate")
            self._phase = "initial"
            return

        # Invert 2×2: J_inv = (1/det)*[[j11,-j01],[-j10,j00]]
        j_inv = [
            [ j11 / det, -j01 / det],
            [-j10 / det,  j00 / det],
        ]

        # Cross-coupling diagnostic
        primary_alt = abs(j00)
        cross_alt = abs(j10)
        coupling_alt = (cross_alt / primary_alt * 100.0) if primary_alt > 0 else 0.0
        primary_az = abs(j11)
        cross_az = abs(j01)
        coupling_az = (cross_az / primary_az * 100.0) if primary_az > 0 else 0.0
        rotation_deg = math.degrees(math.atan2(abs(j10), abs(j00)))
        logger.info(
            "[SunCenter] Jacobian valid: coupling_alt=%.1f%% coupling_az=%.1f%% "
            "image_rotation_hint=%.1f°",
            coupling_alt, coupling_az, rotation_deg,
        )
        if max(coupling_alt, coupling_az) > 30.0:
            logger.warning(
                "[SunCenter] Cross-coupling %.0f%% > 30%% — camera axes rotated ~%.0f° "
                "from mount axes; Jacobian handles this correctly, noting for diagnostics",
                max(coupling_alt, coupling_az), rotation_deg,
            )

        self._j = [[j00, j01], [j10, j11]]
        self._j_inv = j_inv
        self._jacobian_valid = True
        self._jacobian_at = time.monotonic()

        # Update plate scale from current disk measurement.
        if self.disk_radius and self.disk_radius > 0:
            self.plate_scale_deg_per_px = SUN_ANGULAR_RADIUS_DEG / float(self.disk_radius)
            logger.info(
                "[SunCenter] Plate scale: %.6f °/px (disk radius %.1fpx)",
                self.plate_scale_deg_per_px, self.disk_radius,
            )

        self._center_iter_count = 0
        self._center_no_disk_ticks = 0
        self._transition(self.STATE_CENTER, "Jacobian calibrated — entering CENTER")
        self._phase = "check"

    # =========================================================================
    # CENTER
    # =========================================================================

    def _handle_center(self) -> None:
        now = time.monotonic()

        if self._phase == "check":
            if not self.disk_detected:
                self._center_no_disk_ticks += 1
                if self._center_no_disk_ticks >= 3:
                    self._transition_recover("Disk lost in CENTER")
                return
            self._center_no_disk_ticks = 0

            if self.error_radii is None:
                return

            if self.error_radii <= float(self.settings.tolerance_radii):
                logger.info(
                    "[SunCenter] Centered: error=%.4f radii — entering TRACK", self.error_radii
                )
                self._transition(self.STATE_TRACK, f"Centered (err={self.error_radii:.3f}r)")
                self._phase = "idle"
                self._next_refresh_mono = (
                    time.monotonic() + float(self.settings.track_refresh_s)
                )
                self._lock_lost_until_mono = (
                    time.monotonic() + float(self.settings.lock_lost_grace_s)
                )
                self._enable_tracking()
                return

            self._center_iter_count += 1
            if self._center_iter_count > self.settings.max_center_iters:
                logger.warning(
                    "[SunCenter] Max center iterations (%d) exceeded; entering RECOVER",
                    self.settings.max_center_iters,
                )
                self._transition_recover("Max center iterations exceeded")
                return

            dalt, daz = self._jacobian_correction()
            pos = self._safe_get_position()
            ref_alt = pos.get("alt") or self._last_sun_alt()
            ref_az = pos.get("az") or self._last_sun_az()

            tgt_alt = ref_alt - dalt
            tgt_az = ref_az - daz
            self.state_message = (
                f"CENTER iter {self._center_iter_count}/{self.settings.max_center_iters}: "
                f"err={self.error_radii:.3f}r  Δalt={dalt:+.4f}°  Δaz={daz:+.4f}°"
            )
            logger.debug("[SunCenter] %s", self.state_message)
            self._goto_clamped(tgt_alt, tgt_az)
            self._slew_deadline_mono = now + self.settings.center_slew_timeout_s
            self._phase = "slewing"

        elif self._phase == "slewing":
            if self._wait_slew("center: correction slew"):
                return
            self._settle_until_mono = now + self.settings.center_settle_s
            self._phase = "settling"

        elif self._phase == "settling":
            if now < self._settle_until_mono:
                return
            self._phase = "check"

    def _jacobian_correction(self) -> Tuple[float, float]:
        """Compute (dalt, daz) in degrees to apply to the current mount position.

        Solves J @ [dalt, daz] = [-eu, -ev]  (shift disk to image centre).
        With J⁻¹: [dalt, daz] = -J_inv @ [eu, ev].
        """
        eu = float(self.disk_eu_px or 0.0)
        ev = float(self.disk_ev_px or 0.0)

        if self._jacobian_valid and self._j_inv is not None:
            j = self._j_inv
            dalt = j[0][0] * eu + j[0][1] * ev
            daz = j[1][0] * eu + j[1][1] * ev
        elif self.plate_scale_deg_per_px is not None:
            # Plate-scale fallback: assumes U≈az, V≈alt (no cross-coupling).
            dalt = -ev * self.plate_scale_deg_per_px
            daz = -eu * self.plate_scale_deg_per_px
        else:
            # Last resort: fixed estimate (0.004°/px, typical Seestar S50 solar).
            dalt = -ev * 0.004
            daz = -eu * 0.004

        return dalt, daz

    # =========================================================================
    # TRACK
    # =========================================================================

    def _handle_track(self) -> None:
        now = time.monotonic()

        if self._phase == "idle":
            if not self.disk_detected:
                if now >= self._lock_lost_until_mono:
                    self._transition_recover("Disk lost while tracking")
                else:
                    remaining = self._lock_lost_until_mono - now
                    self.state_message = (
                        f"Tracking: disk lost — grace {remaining:.1f}s remaining"
                    )
                return

            # Disk present — refresh grace timer.
            self._lock_lost_until_mono = now + float(self.settings.lock_lost_grace_s)

            if now < self._next_refresh_mono:
                self.state_message = (
                    f"Tracking: err={self.error_radii:.3f}r  "
                    f"next refresh in {self._next_refresh_mono - now:.0f}s"
                    if self.error_radii is not None
                    else "Tracking"
                )
                return

            # Refresh tick.
            self._next_refresh_mono = now + float(self.settings.track_refresh_s)

            if (
                self.error_radii is not None
                and self.error_radii > float(self.settings.drift_threshold_radii)
            ):
                dalt, daz = self._jacobian_correction()
                pos = self._safe_get_position()
                ref_alt = pos.get("alt") or self._last_sun_alt()
                ref_az = pos.get("az") or self._last_sun_az()
                self.state_message = (
                    f"Track correction: err={self.error_radii:.3f}r "
                    f"Δalt={dalt:+.4f}° Δaz={daz:+.4f}°"
                )
                logger.debug("[SunCenter] %s", self.state_message)
                self._goto_clamped(ref_alt - dalt, ref_az - daz)
                self._slew_deadline_mono = (
                    now + float(self.settings.track_correction_slew_timeout_s)
                )
                self._phase = "correction_slewing"
            else:
                self.state_message = (
                    f"Tracking: err={self.error_radii:.3f}r — within drift threshold"
                    if self.error_radii is not None
                    else "Tracking: waiting for disk"
                )

        elif self._phase == "correction_slewing":
            if self._wait_slew("track: correction slew"):
                return
            self._settle_until_mono = (
                time.monotonic() + float(self.settings.track_correction_settle_s)
            )
            self._phase = "correction_settling"

        elif self._phase == "correction_settling":
            if time.monotonic() < self._settle_until_mono:
                return
            self._enable_tracking()   # re-enable in case GoTo toggled it
            self._phase = "idle"

    # =========================================================================
    # RECOVER
    # =========================================================================

    def _handle_recover(self) -> None:
        now = time.monotonic()

        if self._phase == "initial":
            sun = self.adapter.get_sun_altaz()
            if not sun:
                self._enter_fail_safe("Cannot get Sun coordinates for recovery")
                return
            self._goto_clamped(*sun)
            self._slew_deadline_mono = now + float(self.settings.recover_slew_timeout_s)
            self._phase = "slewing"
            self.state_message = "Recovery: GoTo current ephemeris"

        elif self._phase == "slewing":
            if self._wait_slew("recover: ephemeris slew"):
                return
            self._settle_until_mono = now + float(self.settings.recover_settle_s)
            self._phase = "settling"

        elif self._phase == "settling":
            if now < self._settle_until_mono:
                return
            self._phase = "assess"

        elif self._phase == "assess":
            if self.disk_detected:
                logger.info("[SunCenter] Disk re-acquired in RECOVER → CENTER")
                self._center_iter_count = 0
                self._center_no_disk_ticks = 0
                self._transition(self.STATE_CENTER, "Disk re-acquired after recovery")
                self._phase = "check"
                return

            if self._is_cloud():
                logger.info(
                    "[SunCenter] Frame dark in RECOVER — cloud suspected; "
                    "following ephemeris silently"
                )
                self._cloud_start_mono = now
                # First cloud-track GoTo after cloud_track_interval_s.
                self._cloud_next_goto_mono = now + float(self.settings.cloud_track_interval_s)
                self._phase = "cloud_wait"
                self.state_message = (
                    f"Cloud wait: following ephemeris every "
                    f"{self.settings.cloud_track_interval_s:.0f}s; "
                    f"timeout in {self.settings.cloud_wait_max_s:.0f}s"
                )
            else:
                # Flux present but no disk → pointing error → full grid search.
                self.recovery_attempts += 1
                logger.info(
                    "[SunCenter] Flux present but no disk in RECOVER → ACQUIRE "
                    "(attempt #%d)",
                    self.recovery_attempts,
                )
                self._transition(
                    self.STATE_ACQUIRE,
                    f"Recovery #{self.recovery_attempts}: pointing error, reacquiring",
                )
                self._phase = "initial"

        elif self._phase == "cloud_wait":
            if self.disk_detected:
                logger.info("[SunCenter] Disk recovered after cloud — entering CENTER")
                self._center_iter_count = 0
                self._center_no_disk_ticks = 0
                self._transition(self.STATE_CENTER, "Disk re-acquired after cloud cleared")
                self._phase = "check"
                return

            cloud_elapsed = now - self._cloud_start_mono
            if cloud_elapsed >= float(self.settings.cloud_wait_max_s):
                self._enter_fail_safe(
                    f"Cloud cover for >{self.settings.cloud_wait_max_s:.0f}s; "
                    "manual restart required"
                )
                return

            if not self._is_cloud():
                # Flux returned but disk not detected → pointing error crept in.
                self.recovery_attempts += 1
                logger.info(
                    "[SunCenter] Flux returned after cloud but no disk → ACQUIRE "
                    "(attempt #%d)",
                    self.recovery_attempts,
                )
                self._transition(
                    self.STATE_ACQUIRE,
                    f"Post-cloud recovery #{self.recovery_attempts}",
                )
                self._phase = "initial"
                return

            remaining = self.settings.cloud_wait_max_s - cloud_elapsed
            self.state_message = (
                f"Cloud wait: {cloud_elapsed:.0f}s elapsed, {remaining:.0f}s until timeout"
            )

            if now >= self._cloud_next_goto_mono:
                sun = self.adapter.get_sun_altaz()
                if sun:
                    self._goto_clamped(*sun)
                    self._slew_deadline_mono = (
                        now + float(self.settings.recover_slew_timeout_s)
                    )
                    self._cloud_next_goto_mono = (
                        now + float(self.settings.cloud_track_interval_s)
                    )
                    self._phase = "cloud_slewing"
                    logger.debug("[SunCenter] Cloud-track GoTo issued")

        elif self._phase == "cloud_slewing":
            if self._wait_slew("recover: cloud-track slew"):
                return
            # Brief settle, then back to cloud_wait to check for disk.
            self._settle_until_mono = now + 1.0
            self._phase = "cloud_settling"

        elif self._phase == "cloud_settling":
            if now < self._settle_until_mono:
                return
            self._phase = "cloud_wait"

    # =========================================================================
    # Helpers
    # =========================================================================

    def _transition(self, new_state: str, message: str) -> None:
        if self.state != new_state:
            logger.info("[SunCenter] %s → %s  (%s)", self.state, new_state, message)
        self.state = new_state
        self.state_message = message
        self.state_changed_at = time.time()

    def _transition_recover(self, reason: str) -> None:
        try:
            self.adapter.stop_axes()
        except Exception:
            pass
        self._transition(self.STATE_RECOVER, reason)
        self._phase = "initial"

    def _enter_fail_safe(self, reason: str) -> None:
        if self.state == self.STATE_FAIL_SAFE and self.state_message == reason:
            return
        try:
            self.adapter.stop_axes()
        except Exception:
            pass
        self._transition(self.STATE_FAIL_SAFE, reason)
        logger.warning("[SunCenter] FAIL_SAFE: %s", reason)

    def _wait_slew(self, context: str = "") -> bool:
        """Return True if still slewing (caller should return and wait)."""
        if self.adapter.is_slewing():
            if time.monotonic() >= self._slew_deadline_mono:
                self._enter_fail_safe(f"Slew timed out ({context})")
            else:
                self.state_message = f"Slewing… ({context})"
            return True
        return False

    def _goto_clamped(self, alt: float, az: float) -> Dict[str, Any]:
        alt = float(max(self.settings.min_sun_alt_deg + 0.10, min(88.0, alt)))
        az = float(az) % 360.0
        resp = self.adapter.goto_altaz(alt, az)
        self.last_command = {
            "type": "goto_altaz",
            "alt": round(alt, 4),
            "az": round(az, 4),
            "response": resp,
        }
        return resp

    def _enable_tracking(self) -> None:
        try:
            self.adapter.set_tracking(True)
        except Exception as exc:
            logger.debug("[SunCenter] set_tracking(True) failed: %s", exc)

    def _is_cloud(self) -> bool:
        cm = self.center_flux_core_mean
        return cm is None or float(cm) < float(self.settings.cloud_floor_mean)

    def _safe_get_position(self) -> Dict[str, float]:
        try:
            pos = self.adapter.get_position()
            return pos if isinstance(pos, dict) else {}
        except Exception as exc:
            logger.debug("[SunCenter] get_position failed: %s", exc)
            return {}

    def _safe_detector_status(self) -> Dict[str, Any]:
        try:
            data = self.adapter.get_detector_status()
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("[SunCenter] detector status read failed: %s", exc)
            return {}

    def _last_sun_alt(self) -> float:
        sun = self.adapter.get_sun_altaz()
        return float(sun[0]) if sun else 45.0

    def _last_sun_az(self) -> float:
        sun = self.adapter.get_sun_altaz()
        return float(sun[1]) if sun else 180.0

    # ─── Disk snapshot ───────────────────────────────────────────────────────

    def _refresh_disk_snapshot(self, det: Dict[str, Any]) -> None:
        """Extract disk centroid, error, and flux from detector status dict."""
        self.disk_detected = False
        self.disk_cx = None
        self.disk_cy = None
        self.disk_radius = None
        self.disk_eu_px = None
        self.disk_ev_px = None
        self.error_radii = None
        self.disk_info = None
        self.center_flux_core_mean = None

        # Flux (used for cloud detection; available even without disk).
        center_flux = det.get("center_flux")
        if isinstance(center_flux, dict):
            try:
                self.center_flux_core_mean = float(center_flux.get("core_mean"))
            except Exception:
                pass

        if not det.get("disk_detected"):
            return

        disk_info = det.get("disk_info")
        if not isinstance(disk_info, dict):
            return

        try:
            cx = float(disk_info["cx"])
            cy = float(disk_info["cy"])
            rr = float(disk_info["radius"])
            if rr <= 0:
                return

            # Resolve image dimensions from analysis_resolution string.
            # Seestar S50 solar analysis is landscape (320×180).
            width, height = 320.0, 180.0
            rez = str(det.get("analysis_resolution", ""))
            if "x" in rez:
                try:
                    dim = rez.split("@", 1)[0]
                    w_s, h_s = dim.split("x", 1)
                    width = max(10.0, float(w_s))
                    height = max(10.0, float(h_s))
                except Exception:
                    logger.warning(
                        "[SunCenter] Cannot parse analysis_resolution %r; "
                        "using 320×180 fallback",
                        rez,
                    )
            elif rez:
                logger.warning(
                    "[SunCenter] analysis_resolution %r has no 'x'; using 320×180 fallback",
                    rez,
                )

            # Plausibility gate on disk radius.
            min_dim = min(width, height)
            hard_min = max(6.0, min_dim * 0.07)
            hard_max = min(min_dim * 0.95, min_dim * 0.62)
            if rr < hard_min or rr > hard_max:
                logger.debug(
                    "[SunCenter] Rejecting implausible disk radius %.2fpx (hard bounds [%.1f, %.1f])",
                    rr, hard_min, hard_max,
                )
                return

            img_cx = (width - 1.0) / 2.0
            img_cy = (height - 1.0) / 2.0
            eu = cx - img_cx
            ev = cy - img_cy

            self.disk_detected = True
            self.disk_cx = cx
            self.disk_cy = cy
            self.disk_radius = rr
            self.disk_eu_px = eu
            self.disk_ev_px = ev
            self.error_radii = math.hypot(eu, ev) / rr
            self.disk_info = dict(disk_info)

            # Update plate scale whenever we have a good lock.
            self.plate_scale_deg_per_px = SUN_ANGULAR_RADIUS_DEG / rr

        except Exception:
            pass  # leave all fields None/False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_service: Optional[SunCenteringService] = None
_service_lock = threading.Lock()


def get_sun_center_service() -> Optional[SunCenteringService]:
    return _service


def start_sun_center_service(
    adapter: SunCenteringAdapter,
    settings: Optional[SunCenteringSettings] = None,
) -> SunCenteringService:
    global _service
    with _service_lock:
        if _service:
            _service.stop()
        _service = SunCenteringService(adapter=adapter, settings=settings)
        _service.start()
        return _service


def stop_sun_center_service() -> None:
    global _service
    with _service_lock:
        if _service:
            _service.stop()
            _service = None
