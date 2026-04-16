"""Sun acquisition and centering service.

Hybrid controller:
1. Coarse astronomical goto to Sun alt/az.
2. Expanding local acquisition search.
3. Closed-loop image centering using detector disc centroid.

The service is intentionally conservative and designed for operational safety:
- Requires Solar mode (validated by route layer before start).
- Uses infinite recoveries with rest periods when disc is lost.
- Starts in strict centering and downgrades to conservative if flailing.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from src import logger


@dataclass
class SunCenteringAdapter:
    """Callbacks that isolate this service from Flask route globals."""

    is_scope_connected: Callable[[], bool]
    is_alpaca_connected: Callable[[], bool]
    get_viewing_mode: Callable[[], Optional[str]]
    get_sun_altaz: Callable[[], Optional[Tuple[float, float]]]
    goto_altaz: Callable[[float, float], Dict[str, Any]]
    is_slewing: Callable[[], bool]
    move_axis: Callable[[int, float], Dict[str, Any]]
    stop_axes: Callable[[], Dict[str, Any]]
    get_max_move_rate: Callable[[int], float]
    get_detector_status: Callable[[], Dict[str, Any]]


@dataclass
class SunCenteringSettings:
    tick_hz: float = 4.0
    min_sun_alt_deg: float = 8.0

    # Strict-first strategy (user preference)
    strict_tolerance_radii: float = 0.10
    conservative_tolerance_radii: float = 0.18

    hold_seconds: float = 2.0
    drift_recenter_factor: float = 1.25
    acquire_lock_radii: float = 0.30
    acquire_flux_core_to_frame: float = 1.20  # SC-07: raised from 1.08
    acquire_flux_core_to_ring: float = 1.15   # SC-07: raised from 1.03
    acquire_flux_min_core_mean: float = 14.0
    acquire_flux_confirm_frames: int = 1
    acquire_flux_hold_seconds: float = 2.0
    lock_lost_grace_seconds: float = 4.0

    # Flail detection -> conservative fallback
    flail_cycles: int = 10
    flail_min_improvement: float = 0.01

    # Recovery behavior (infinite retries with rest)
    recover_rest_seconds: float = 6.0

    # Coarse and search behavior
    coarse_timeout_seconds: float = 25.0
    coarse_settle_seconds: float = 3.0
    search_step_deg: float = 0.30
    search_max_ring: int = 5
    search_settle_seconds: float = 2.5
    busy_retry_seconds: float = 1.0
    precheck_busy_timeout_seconds: float = 60.0  # SC-04: fail-safe if mount stuck slewing
    # Search strategy: spiral | raster | random_walk | adaptive
    # adaptive = spiral first pass, raster second pass, random-walk thereafter.
    search_pattern_mode: str = "adaptive"
    random_walk_points: int = 72
    random_walk_seed: int = 4242

    # Closed-loop controller gains/rates
    kp: float = 0.90
    ki: float = 0.12
    integral_limit: float = 2.0
    max_rate_fraction: float = 0.22
    min_rate_deg_s: float = 0.08

    # One-time axis sign probing
    probe_rate_deg_s: float = 0.35
    probe_pulse_seconds: float = 0.25
    probe_settle_seconds: float = 0.70
    probe_min_effect: float = 0.008

    # Disk sanity gate to reject internal reflections / implausible circles.
    # Fractions are relative to min(analysis_width, analysis_height).
    min_valid_radius_frac: float = 0.12
    max_valid_radius_frac: float = 0.58
    out_of_band_reject_radii: float = 1.80


class SunCenteringService:
    """Background service that acquires and centers the Sun."""

    STATE_PRECHECK = "precheck"
    STATE_COARSE_POINT = "coarse_point"
    STATE_ACQUIRE_SEARCH = "acquire_search"
    STATE_FINE_CENTER = "fine_center"
    STATE_LOCK_MONITOR = "lock_monitor"
    STATE_RECOVER_REST = "recover_rest"
    STATE_FAIL_SAFE = "fail_safe"
    STATE_STOPPED = "stopped"

    MODE_STRICT = "strict"
    MODE_CONSERVATIVE = "conservative"

    def __init__(
        self,
        adapter: SunCenteringAdapter,
        settings: Optional[SunCenteringSettings] = None,
    ):
        self.adapter = adapter
        self.settings = settings or SunCenteringSettings()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

        self.state = self.STATE_PRECHECK
        self.state_message = "Initializing"
        self.state_changed_at = time.time()
        self.started_at: Optional[float] = None

        self.tolerance_mode = self.MODE_STRICT

        self.error_u: Optional[float] = None
        self.error_v: Optional[float] = None
        self.error_norm: Optional[float] = None
        self.center_flux_core_mean: Optional[float] = None
        self.center_flux_core_to_ring: Optional[float] = None
        self.center_flux_core_to_frame: Optional[float] = None
        self.disk_detected = False
        self.disk_info: Optional[Dict[str, Any]] = None

        self._coarse_deadline_mono = 0.0
        self._coarse_settle_until_mono: float = 0.0  # SC-01
        self._next_action_mono = 0.0
        self._recover_until_mono = 0.0
        self._precheck_busy_deadline_mono: float = 0.0  # SC-04

        self._search_pattern = self.build_search_pattern(
            self.settings.search_step_deg,
            self.settings.search_max_ring,
        )
        self._search_pattern_kind = "spiral"
        self._search_pattern_cycle = 0
        self._search_index = 0
        self.search_cycles = 0

        self._set_search_pattern_for_cycle(0)

        self._axis_sign: Dict[int, int] = {0: 1, 1: 1}
        self._axis_probe_done: Dict[int, bool] = {0: False, 1: False}

        # Non-blocking axis probe state (SC-02)
        self._probe_state: str = "idle"  # "idle" | "pulsing" | "settling"
        self._probe_deadline_mono: float = 0.0
        self._probe_before_u: float = 0.0
        self._probe_before_v: float = 0.0
        self._probe_axis0_delta_u: float = 0.0  # saved for SC-06 cross-coupling
        self._probe_axis0_delta_v: float = 0.0

        self._int_u = 0.0
        self._int_v = 0.0
        self._hold_start_mono: Optional[float] = None
        self._prev_err_norm: Optional[float] = None
        self._flail_counter = 0
        self._center_flux_hit_count = 0
        self._center_flux_hold_until_mono = 0.0
        self._lock_lost_grace_until_mono = 0.0

        self.recovery_attempts = 0
        self.last_command: Dict[str, Any] = {}
        self.last_error: Optional[str] = None

    @staticmethod
    def build_search_pattern(step_deg: float, max_ring: int) -> List[Tuple[float, float]]:
        """Return expanding search offsets (dalt, daz) in degrees."""
        pattern: List[Tuple[float, float]] = [(0.0, 0.0)]
        for ring in range(1, max(1, int(max_ring)) + 1):
            d = float(ring) * float(step_deg)
            pattern.extend(
                [
                    (+d, 0.0),
                    (-d, 0.0),
                    (0.0, +d),
                    (0.0, -d),
                    (+d, +d),
                    (+d, -d),
                    (-d, +d),
                    (-d, -d),
                ]
            )
        return pattern

    @staticmethod
    def build_raster_pattern(step_deg: float, max_ring: int) -> List[Tuple[float, float]]:
        """Return center-out boustrophedon raster offsets over a square box.

        Starts near the center and expands outward, avoiding an immediate
        origin->corner jump on the second move.
        """
        ring = max(1, int(max_ring))
        step = float(step_deg)

        seen = set()
        pattern: List[Tuple[float, float]] = []

        def _center_out_indices(r: int) -> List[int]:
            seq = [0]
            for d in range(1, r + 1):
                seq.extend([-d, +d])
            return seq

        # Start at origin, then scan from center rows outward to keep early
        # motions local around ephemeris target.
        def _append(a: float, b: float) -> None:
            key = (round(a, 9), round(b, 9))
            if key in seen:
                return
            seen.add(key)
            pattern.append((a, b))

        _append(0.0, 0.0)
        y_values = _center_out_indices(ring)
        x_base = _center_out_indices(ring)
        for row_i, yi in enumerate(y_values):
            daz_values = list(x_base)
            if row_i % 2 == 1:
                daz_values.reverse()
            dalt = yi * step
            for xi in daz_values:
                _append(dalt, xi * step)

        return pattern

    @staticmethod
    def build_random_walk_pattern(
        step_deg: float,
        max_ring: int,
        points: int,
        seed: int,
    ) -> List[Tuple[float, float]]:
        """Return bounded random-walk offsets over the local search box."""
        ring = max(1, int(max_ring))
        n_points = max(8, int(points))
        step = float(step_deg)
        rng = random.Random(int(seed))

        max_off = ring * step
        pattern: List[Tuple[float, float]] = [(0.0, 0.0)]

        pos_alt = 0.0
        pos_az = 0.0
        moves = [
            (+step, 0.0),
            (-step, 0.0),
            (0.0, +step),
            (0.0, -step),
            (+step, +step),
            (+step, -step),
            (-step, +step),
            (-step, -step),
        ]

        for _ in range(n_points - 1):
            dalt, daz = rng.choice(moves)
            nxt_alt = max(-max_off, min(max_off, pos_alt + dalt))
            nxt_az = max(-max_off, min(max_off, pos_az + daz))
            pos_alt, pos_az = nxt_alt, nxt_az
            pattern.append((pos_alt, pos_az))

        return pattern

    def _normalized_search_mode(self) -> str:
        mode = (self.settings.search_pattern_mode or "adaptive").strip().lower()
        if mode in {"spiral", "raster", "random_walk", "adaptive"}:
            return mode
        return "adaptive"

    def _pattern_for_cycle(self, cycle: int) -> Tuple[List[Tuple[float, float]], str]:
        mode = self._normalized_search_mode()
        cycle_i = max(0, int(cycle))

        if mode == "spiral":
            return (
                self.build_search_pattern(
                    self.settings.search_step_deg,
                    self.settings.search_max_ring,
                ),
                "spiral",
            )

        if mode == "raster":
            return (
                self.build_raster_pattern(
                    self.settings.search_step_deg,
                    self.settings.search_max_ring,
                ),
                "raster",
            )

        if mode == "random_walk":
            return (
                self.build_random_walk_pattern(
                    self.settings.search_step_deg,
                    self.settings.search_max_ring,
                    self.settings.random_walk_points,
                    self.settings.random_walk_seed + cycle_i,
                ),
                "random_walk",
            )

        # Adaptive default: spiral -> raster -> random-walk.
        if cycle_i <= 0:
            return (
                self.build_search_pattern(
                    self.settings.search_step_deg,
                    self.settings.search_max_ring,
                ),
                "spiral",
            )
        if cycle_i == 1:
            return (
                self.build_raster_pattern(
                    self.settings.search_step_deg,
                    self.settings.search_max_ring,
                ),
                "raster",
            )

        return (
            self.build_random_walk_pattern(
                self.settings.search_step_deg,
                self.settings.search_max_ring,
                self.settings.random_walk_points,
                self.settings.random_walk_seed + cycle_i,
            ),
            "random_walk",
        )

    def _set_search_pattern_for_cycle(self, cycle: int) -> None:
        pattern, kind = self._pattern_for_cycle(cycle)
        self._search_pattern = pattern if pattern else [(0.0, 0.0)]
        self._search_pattern_kind = kind
        self._search_pattern_cycle = max(0, int(cycle))
        self._search_index = 0

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self.started_at = time.time()
            self.state = self.STATE_PRECHECK
            self.state_message = "Precheck"
            self.state_changed_at = time.time()
            self._precheck_busy_deadline_mono = (
                time.monotonic() + float(self.settings.precheck_busy_timeout_seconds)
            )
            self._thread = threading.Thread(
                target=self._run_loop,
                name="sun-centering",
                daemon=True,
            )
            self._thread.start()
            logger.info("[SunCenter] Service started")

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
        with self._lock:
            if not self._running:
                return False
            self.search_cycles = 0
            self._set_search_pattern_for_cycle(0)
            self._reset_controller(full=True)
            self._transition(
                self.STATE_ACQUIRE_SEARCH,
                "Manual recenter requested; restarting acquisition",
            )
            self._next_action_mono = 0.0
            return True

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        s = self.settings
        if "strict_tolerance_radii" in patch:
            s.strict_tolerance_radii = float(max(0.02, min(0.8, patch["strict_tolerance_radii"])))
        if "conservative_tolerance_radii" in patch:
            s.conservative_tolerance_radii = float(
                max(0.04, min(1.2, patch["conservative_tolerance_radii"]))
            )
        if "recover_rest_seconds" in patch:
            s.recover_rest_seconds = float(max(0.5, min(120.0, patch["recover_rest_seconds"])))
        if "hold_seconds" in patch:
            s.hold_seconds = float(max(0.2, min(20.0, patch["hold_seconds"])))
        if "acquire_lock_radii" in patch:
            s.acquire_lock_radii = float(max(0.05, min(1.5, patch["acquire_lock_radii"])))
        if "acquire_flux_core_to_frame" in patch:
            s.acquire_flux_core_to_frame = float(
                max(1.0, min(3.0, patch["acquire_flux_core_to_frame"]))
            )
        if "acquire_flux_core_to_ring" in patch:
            s.acquire_flux_core_to_ring = float(
                max(1.0, min(3.0, patch["acquire_flux_core_to_ring"]))
            )
        if "acquire_flux_min_core_mean" in patch:
            s.acquire_flux_min_core_mean = float(
                max(0.0, min(255.0, patch["acquire_flux_min_core_mean"]))
            )
        if "acquire_flux_confirm_frames" in patch:
            s.acquire_flux_confirm_frames = int(
                max(1, min(12, int(patch["acquire_flux_confirm_frames"])))
            )
        if "acquire_flux_hold_seconds" in patch:
            s.acquire_flux_hold_seconds = float(
                max(0.2, min(8.0, patch["acquire_flux_hold_seconds"]))
            )
        if "lock_lost_grace_seconds" in patch:
            s.lock_lost_grace_seconds = float(
                max(0.5, min(30.0, patch["lock_lost_grace_seconds"]))
            )
        if "search_step_deg" in patch:
            s.search_step_deg = float(max(0.05, min(3.0, patch["search_step_deg"])))
        if "search_max_ring" in patch:
            s.search_max_ring = int(max(1, min(20, int(patch["search_max_ring"]))))
        if "busy_retry_seconds" in patch:
            s.busy_retry_seconds = float(max(0.2, min(10.0, patch["busy_retry_seconds"])))
        if "search_pattern_mode" in patch:
            mode = str(patch["search_pattern_mode"]).strip().lower()
            if mode in {"spiral", "raster", "random_walk", "adaptive"}:
                s.search_pattern_mode = mode
        if "random_walk_points" in patch:
            s.random_walk_points = int(max(8, min(400, int(patch["random_walk_points"]))))
        if "random_walk_seed" in patch:
            s.random_walk_seed = int(patch["random_walk_seed"])
        if "min_valid_radius_frac" in patch:
            s.min_valid_radius_frac = float(
                max(0.02, min(0.45, patch["min_valid_radius_frac"]))
            )
        if "max_valid_radius_frac" in patch:
            s.max_valid_radius_frac = float(
                max(0.15, min(0.95, patch["max_valid_radius_frac"]))
            )
        if "out_of_band_reject_radii" in patch:
            s.out_of_band_reject_radii = float(
                max(0.4, min(4.0, patch["out_of_band_reject_radii"]))
            )

        # Keep bounds coherent even if user patches them out of order.
        if s.max_valid_radius_frac <= s.min_valid_radius_frac:
            s.max_valid_radius_frac = min(0.95, s.min_valid_radius_frac + 0.08)

        self._set_search_pattern_for_cycle(self.search_cycles)

        return {
            "strict_tolerance_radii": s.strict_tolerance_radii,
            "conservative_tolerance_radii": s.conservative_tolerance_radii,
            "recover_rest_seconds": s.recover_rest_seconds,
            "hold_seconds": s.hold_seconds,
            "acquire_lock_radii": s.acquire_lock_radii,
            "acquire_flux_core_to_frame": s.acquire_flux_core_to_frame,
            "acquire_flux_core_to_ring": s.acquire_flux_core_to_ring,
            "acquire_flux_min_core_mean": s.acquire_flux_min_core_mean,
            "acquire_flux_confirm_frames": s.acquire_flux_confirm_frames,
            "acquire_flux_hold_seconds": s.acquire_flux_hold_seconds,
            "lock_lost_grace_seconds": s.lock_lost_grace_seconds,
            "search_step_deg": s.search_step_deg,
            "search_max_ring": s.search_max_ring,
            "busy_retry_seconds": s.busy_retry_seconds,
            "search_pattern_mode": s.search_pattern_mode,
            "random_walk_points": s.random_walk_points,
            "random_walk_seed": s.random_walk_seed,
            "min_valid_radius_frac": s.min_valid_radius_frac,
            "max_valid_radius_frac": s.max_valid_radius_frac,
            "out_of_band_reject_radii": s.out_of_band_reject_radii,
        }

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            age = 0.0
            if self.started_at:
                age = max(0.0, time.time() - self.started_at)
            return {
                "running": self._running,
                "state": self.state,
                "message": self.state_message,
                "state_changed_at": self.state_changed_at,
                "uptime_s": round(age, 1),
                "tolerance_mode": self.tolerance_mode,
                "strict_tolerance_radii": self.settings.strict_tolerance_radii,
                "conservative_tolerance_radii": self.settings.conservative_tolerance_radii,
                "acquire_lock_radii": self.settings.acquire_lock_radii,
                "acquire_flux_core_to_frame": self.settings.acquire_flux_core_to_frame,
                "acquire_flux_core_to_ring": self.settings.acquire_flux_core_to_ring,
                "acquire_flux_min_core_mean": self.settings.acquire_flux_min_core_mean,
                "acquire_flux_confirm_frames": self.settings.acquire_flux_confirm_frames,
                "acquire_flux_hold_seconds": self.settings.acquire_flux_hold_seconds,
                "lock_lost_grace_seconds": self.settings.lock_lost_grace_seconds,
                "search_pattern_mode": self.settings.search_pattern_mode,
                "search_pattern_kind": self._search_pattern_kind,
                "busy_retry_seconds": self.settings.busy_retry_seconds,
                "min_valid_radius_frac": self.settings.min_valid_radius_frac,
                "max_valid_radius_frac": self.settings.max_valid_radius_frac,
                "out_of_band_reject_radii": self.settings.out_of_band_reject_radii,
                "error_u": None if self.error_u is None else round(self.error_u, 4),
                "error_v": None if self.error_v is None else round(self.error_v, 4),
                "error_norm": None if self.error_norm is None else round(self.error_norm, 4),
                "center_flux_core_mean": (
                    None
                    if self.center_flux_core_mean is None
                    else round(self.center_flux_core_mean, 3)
                ),
                "center_flux_core_to_ring": (
                    None
                    if self.center_flux_core_to_ring is None
                    else round(self.center_flux_core_to_ring, 4)
                ),
                "center_flux_core_to_frame": (
                    None
                    if self.center_flux_core_to_frame is None
                    else round(self.center_flux_core_to_frame, 4)
                ),
                "disk_detected": bool(self.disk_detected),
                "disk_info": self.disk_info,
                "recovery_attempts": int(self.recovery_attempts),
                "recover_rest_seconds": self.settings.recover_rest_seconds,
                "recovering_until": (
                    None
                    if self.state != self.STATE_RECOVER_REST
                    else round(max(0.0, self._recover_until_mono - time.monotonic()), 2)
                ),
                "lock_hold_remaining": (
                    None
                    if self.state != self.STATE_LOCK_MONITOR
                    else round(max(0.0, self._lock_lost_grace_until_mono - time.monotonic()), 2)
                ),
                "search_index": int(self._search_index),
                "search_cycles": int(self.search_cycles),
                "axis_sign": dict(self._axis_sign),
                "axis_probe_done": dict(self._axis_probe_done),
                "flail_counter": int(self._flail_counter),
                "last_command": dict(self.last_command),
                "last_error": self.last_error,
            }

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
            sleep_s = max(0.02, period - elapsed)
            self._stop_event.wait(sleep_s)

    def _tick_once(self) -> None:
        if not self._running:
            return

        # Always refresh detector snapshot first.
        det = self._safe_detector_status()
        self._refresh_error_snapshot(det)

        # Hard connection guard.
        if not self.adapter.is_scope_connected() or not self.adapter.is_alpaca_connected():
            self._enter_fail_safe("Scope/ALPACA disconnected")
            return

        # Mode guard: this service is Solar-only for now.
        mode = (self.adapter.get_viewing_mode() or "").strip().lower()
        if mode not in {"sun", "solar"}:
            self._enter_fail_safe("Solar mode required (experimental feature)")
            return

        state = self.state
        if state == self.STATE_PRECHECK:
            self._handle_precheck()
        elif state == self.STATE_COARSE_POINT:
            self._handle_coarse_point()
        elif state == self.STATE_ACQUIRE_SEARCH:
            self._handle_acquire_search()
        elif state == self.STATE_FINE_CENTER:
            self._handle_fine_center()
        elif state == self.STATE_LOCK_MONITOR:
            self._handle_lock_monitor()
        elif state == self.STATE_RECOVER_REST:
            self._handle_recover_rest()
        elif state == self.STATE_FAIL_SAFE:
            # Stay inert until operator issues manual recenter.
            pass

    def _handle_precheck(self) -> None:
        now = time.monotonic()
        if now < self._next_action_mono:
            return

        if self.adapter.is_slewing():
            # SC-04: fail-safe if mount never stops slewing.
            if now >= self._precheck_busy_deadline_mono:
                self._enter_fail_safe(
                    f"Mount stuck slewing for >{self.settings.precheck_busy_timeout_seconds:.0f}s"
                )
                return
            self.state_message = "Mount slewing; waiting before coarse point"
            self._next_action_mono = now + float(self.settings.busy_retry_seconds)
            return

        sun_altaz = self.adapter.get_sun_altaz()
        if not sun_altaz:
            self._enter_fail_safe("Unable to compute Sun coordinates")
            return
        sun_alt, sun_az = sun_altaz
        if float(sun_alt) < float(self.settings.min_sun_alt_deg):
            self._enter_fail_safe(
                f"Sun altitude {sun_alt:.2f} below minimum {self.settings.min_sun_alt_deg:.2f}"
            )
            return

        self._issue_coarse_point(float(sun_alt), float(sun_az))

    def _issue_coarse_point(self, sun_alt: float, sun_az: float) -> None:
        resp = self.adapter.goto_altaz(sun_alt, sun_az)
        self.last_command = {
            "type": "goto_altaz",
            "alt": round(sun_alt, 4),
            "az": round(sun_az % 360.0, 4),
            "response": resp,
        }

        if self._is_equipment_moving_response(resp):
            self.state_message = "Mount busy; deferring coarse point retry"
            self._next_action_mono = time.monotonic() + float(self.settings.busy_retry_seconds)
            return

        _now = time.monotonic()
        self._coarse_settle_until_mono = _now + float(self.settings.coarse_settle_seconds)  # SC-01
        self._coarse_deadline_mono = _now + float(self.settings.coarse_timeout_seconds)
        self._next_action_mono = _now + float(self.settings.coarse_settle_seconds)
        self._transition(self.STATE_COARSE_POINT, "Coarse pointing to ephemeris target")

    def _handle_coarse_point(self) -> None:
        now = time.monotonic()

        # SC-01: enforce settle delay before acting on any detector reading.
        if now < self._coarse_settle_until_mono:
            self.state_message = "Coarse point: waiting for mount to settle"
            return

        if self.disk_detected and self.error_norm is not None:
            self._enter_fine_center("Disc acquired after coarse point")
            return

        if now >= self._coarse_deadline_mono:
            self._transition(self.STATE_ACQUIRE_SEARCH, "Coarse timeout; starting acquisition search")
            self._next_action_mono = 0.0
            self.search_cycles = 0
            self._set_search_pattern_for_cycle(0)

    def _handle_acquire_search(self) -> None:
        if self.disk_detected and self.error_norm is not None:
            lock_tol = max(float(self.settings.acquire_lock_radii), self._current_tolerance())
            if float(self.error_norm) <= lock_tol:
                try:
                    self.adapter.stop_axes()
                except Exception:
                    pass
                self._enter_lock_monitor("Disc centered during search; holding lock")
            else:
                self._enter_fine_center("Disc acquired during search")
            return

        now = time.monotonic()
        if now < self._next_action_mono:
            return

        if (not self.disk_detected) and self._center_flux_indicates_center_hint():
            self._center_flux_hit_count += 1
        else:
            self._center_flux_hit_count = max(0, self._center_flux_hit_count - 1)

        if (
            not self.disk_detected
            and self._center_flux_hit_count >= int(self.settings.acquire_flux_confirm_frames)
        ):
            try:
                self.adapter.stop_axes()
            except Exception:
                pass
            self._center_flux_hold_until_mono = now + float(self.settings.acquire_flux_hold_seconds)
            self._center_flux_hit_count = 0
            self._enter_fine_center("Center flux peak detected; pausing search for lock")
            return

        if self.adapter.is_slewing():
            self.state_message = (
                f"Waiting for slew settle before {self._search_pattern_kind} search step"
            )
            self._next_action_mono = now + float(self.settings.busy_retry_seconds)
            return

        sun_altaz = self.adapter.get_sun_altaz()
        if not sun_altaz:
            self._enter_fail_safe("Sun coordinates unavailable during search")
            return
        sun_alt, sun_az = sun_altaz

        if (not self._search_pattern) or (self._search_pattern_cycle != self.search_cycles):
            self._set_search_pattern_for_cycle(self.search_cycles)

        if self._search_index >= len(self._search_pattern):
            self.search_cycles += 1
            self._set_search_pattern_for_cycle(self.search_cycles)

        idx = self._search_index
        dalt, daz = self._search_pattern[idx]

        # SC-05: clamp altitude so search offsets never send the mount below the
        # safety floor or above the gimbal limit.
        tgt_alt = max(
            float(self.settings.min_sun_alt_deg) + 0.10,
            min(88.0, float(sun_alt) + float(dalt)),
        )
        tgt_az = (float(sun_az) + float(daz)) % 360.0

        resp = self.adapter.goto_altaz(tgt_alt, tgt_az)
        if self._is_equipment_moving_response(resp):
            self.last_command = {
                "type": "search_goto_busy",
                "alt": round(tgt_alt, 4),
                "az": round(tgt_az, 4),
                "offset": [round(dalt, 4), round(daz, 4)],
                "response": resp,
            }
            self.state_message = (
                f"Mount busy; retrying {self._search_pattern_kind} step {self._search_index + 1}"
            )
            self._next_action_mono = now + float(self.settings.busy_retry_seconds)
            return

        self._search_index += 1
        self.last_command = {
            "type": "search_goto",
            "alt": round(tgt_alt, 4),
            "az": round(tgt_az, 4),
            "offset": [round(dalt, 4), round(daz, 4)],
            "response": resp,
        }
        self.state_message = (
            f"Searching ({self._search_pattern_kind} step {self._search_index}/"
            f"{len(self._search_pattern)}, cycle {self.search_cycles})"
        )
        self._next_action_mono = now + float(self.settings.search_settle_seconds)

    @staticmethod
    def _is_equipment_moving_response(resp: Dict[str, Any]) -> bool:
        if not isinstance(resp, dict):
            return False

        err_num_raw = resp.get("ErrorNumber", 0)
        try:
            err_num = int(err_num_raw)
        except Exception:
            err_num = 0

        msg = f"{resp.get('ErrorMessage', '')} {resp.get('error', '')}".strip().lower()

        if "equipment is moving" in msg:
            return True
        if err_num == 1279 and ("moving" in msg or "slew" in msg):
            return True
        return False

    def _enter_fine_center(self, message: str) -> None:
        self._reset_controller(full=False)
        self._axis_probe_done = {0: False, 1: False}
        self._transition(self.STATE_FINE_CENTER, message)

    def _enter_lock_monitor(self, message: str) -> None:
        self._lock_lost_grace_until_mono = (
            time.monotonic() + float(self.settings.lock_lost_grace_seconds)
        )
        self._transition(self.STATE_LOCK_MONITOR, message)

    def _handle_fine_center(self) -> None:
        now = time.monotonic()

        if not self.disk_detected or self.error_norm is None:
            # SC-02: abort any in-progress probe; axis motion will be stopped below.
            if self._probe_state != "idle":
                self._probe_state = "idle"

            if self._center_flux_indicates_center_hint():
                self._center_flux_hold_until_mono = max(
                    self._center_flux_hold_until_mono,
                    now + float(self.settings.acquire_flux_hold_seconds),
                )

            if now < self._center_flux_hold_until_mono:
                try:
                    self.adapter.stop_axes()
                except Exception:
                    pass
                self.state_message = "Center hint active; holding position for lock"
                return

            self._enter_recover_rest("Disc lost during centering")
            return

        self._center_flux_hold_until_mono = 0.0

        eu = float(self.error_u or 0.0)
        ev = float(self.error_v or 0.0)
        en = float(self.error_norm)

        # One-time sign probing — non-blocking, one phase per tick (SC-02).
        if not self._axis_probe_done[0]:
            self._advance_axis_probe(0, eu, ev)
            return
        if not self._axis_probe_done[1]:
            self._advance_axis_probe(1, eu, ev)
            return

        tol = self._current_tolerance()
        lock_tol = max(float(self.settings.acquire_lock_radii), tol)
        if en <= lock_tol:
            try:
                self.adapter.stop_axes()
            except Exception:
                pass
            self._hold_start_mono = None
            self._enter_lock_monitor("Centered; holding lock")
            return

        self._hold_start_mono = None

        self._int_u = max(
            -self.settings.integral_limit,
            min(self.settings.integral_limit, self._int_u + eu),
        )
        self._int_v = max(
            -self.settings.integral_limit,
            min(self.settings.integral_limit, self._int_v + ev),
        )

        max0 = max(0.2, abs(float(self.adapter.get_max_move_rate(0))) * self.settings.max_rate_fraction)
        max1 = max(0.2, abs(float(self.adapter.get_max_move_rate(1))) * self.settings.max_rate_fraction)

        ctrl0 = self.settings.kp * eu + self.settings.ki * self._int_u
        ctrl1 = self.settings.kp * ev + self.settings.ki * self._int_v

        rate0 = self._axis_sign[0] * ctrl0
        rate1 = self._axis_sign[1] * ctrl1

        rate0 = max(-max0, min(max0, rate0))
        rate1 = max(-max1, min(max1, rate1))

        if abs(rate0) < self.settings.min_rate_deg_s and abs(eu) > tol:
            rate0 = math.copysign(self.settings.min_rate_deg_s, rate0 if rate0 != 0 else eu)
        if abs(rate1) < self.settings.min_rate_deg_s and abs(ev) > tol:
            rate1 = math.copysign(self.settings.min_rate_deg_s, rate1 if rate1 != 0 else ev)

        r0 = self.adapter.move_axis(0, float(rate0))
        r1 = self.adapter.move_axis(1, float(rate1))
        self.last_command = {
            "type": "move_axis",
            "axis0_rate": round(float(rate0), 4),
            "axis1_rate": round(float(rate1), 4),
            "resp0": r0,
            "resp1": r1,
        }

        self._update_flail(en, abs(rate0) >= 0.95 * max0 or abs(rate1) >= 0.95 * max1)

    def _handle_lock_monitor(self) -> None:
        now = time.monotonic()

        if not self.disk_detected or self.error_norm is None:
            if self._center_flux_indicates_center_hint():
                self._lock_lost_grace_until_mono = max(
                    self._lock_lost_grace_until_mono,
                    now + float(self.settings.lock_lost_grace_seconds),
                )

            if now < self._lock_lost_grace_until_mono:
                try:
                    self.adapter.stop_axes()
                except Exception:
                    pass
                self.state_message = "Lock hold; waiting for disk reacquire"
                return

            self._enter_recover_rest("Disc lost while locked")
            return

        self._lock_lost_grace_until_mono = now + float(self.settings.lock_lost_grace_seconds)

        en = float(self.error_norm)
        tol = self._current_tolerance()
        if en > tol * float(self.settings.drift_recenter_factor):
            self._enter_fine_center("Drift exceeded tolerance; recentering")

    def _handle_recover_rest(self) -> None:
        if self.disk_detected and self.error_norm is not None:
            self._enter_fine_center("Disc re-acquired during recovery rest")
            return

        now = time.monotonic()
        if now < self._recover_until_mono:
            return

        self.recovery_attempts += 1
        self._reset_controller(full=False)
        self._set_search_pattern_for_cycle(self.search_cycles)
        self._next_action_mono = 0.0
        self._transition(
            self.STATE_ACQUIRE_SEARCH,
            f"Recovery attempt #{self.recovery_attempts}",
        )

    def _enter_recover_rest(self, reason: str) -> None:
        try:
            self.adapter.stop_axes()
        except Exception:
            pass
        self._recover_until_mono = time.monotonic() + float(self.settings.recover_rest_seconds)
        self._transition(
            self.STATE_RECOVER_REST,
            f"{reason}; resting {self.settings.recover_rest_seconds:.1f}s before retry",
        )

    def _enter_fail_safe(self, reason: str) -> None:
        if self.state == self.STATE_FAIL_SAFE and self.state_message == reason:
            return
        try:
            self.adapter.stop_axes()
        except Exception:
            pass
        self._transition(self.STATE_FAIL_SAFE, reason)

    def _refresh_error_snapshot(self, det: Dict[str, Any]) -> None:
        disk_detected = bool(det.get("disk_detected"))
        disk_info = det.get("disk_info") if isinstance(det.get("disk_info"), dict) else None
        center_flux = det.get("center_flux") if isinstance(det.get("center_flux"), dict) else None

        self.disk_detected = False
        self.disk_info = None
        self.error_u = None
        self.error_v = None
        self.error_norm = None
        self.center_flux_core_mean = None
        self.center_flux_core_to_ring = None
        self.center_flux_core_to_frame = None

        if center_flux:
            try:
                self.center_flux_core_mean = float(center_flux.get("core_mean"))
            except Exception:
                self.center_flux_core_mean = None
            try:
                self.center_flux_core_to_ring = float(center_flux.get("core_to_ring"))
            except Exception:
                self.center_flux_core_to_ring = None
            try:
                self.center_flux_core_to_frame = float(center_flux.get("core_to_frame"))
            except Exception:
                self.center_flux_core_to_frame = None

        if not disk_detected or not disk_info:
            return

        try:
            cx = float(disk_info.get("cx"))
            cy = float(disk_info.get("cy"))
            rr = float(disk_info.get("radius"))
            if rr <= 0:
                return

            # SC-03: Seestar S50 solar analysis is landscape (320×180).
            # The old fallback of 180×320 was portrait — wrong image centre.
            width = 320.0
            height = 180.0
            rez = str(det.get("analysis_resolution", ""))
            if "x" in rez:
                try:
                    dim = rez.split("@", 1)[0]
                    w_s, h_s = dim.split("x", 1)
                    width = max(10.0, float(w_s))
                    height = max(10.0, float(h_s))
                except Exception:
                    logger.warning(
                        "[SunCenter] Could not parse analysis_resolution %r; "
                        "using landscape fallback 320×180 — verify detector output",
                        rez,
                    )
            elif rez:
                logger.warning(
                    "[SunCenter] analysis_resolution %r contains no 'x' separator; "
                    "using landscape fallback 320×180 — verify detector output",
                    rez,
                )

            min_dim = min(width, height)
            pref_min_rr = max(4.0, min_dim * float(self.settings.min_valid_radius_frac))
            pref_max_rr = max(pref_min_rr + 1.0, min_dim * float(self.settings.max_valid_radius_frac))
            hard_min_rr = max(6.0, pref_min_rr * 0.60)
            hard_max_rr = min(min_dim * 0.95, pref_max_rr * 1.35)
            if rr < hard_min_rr or rr > hard_max_rr:
                logger.debug(
                    "[SunCenter] Ignoring implausible disk radius %.2fpx outside hard [%.2f, %.2f]",
                    rr,
                    hard_min_rr,
                    hard_max_rr,
                )
                return

            u0 = (width - 1.0) / 2.0
            v0 = (height - 1.0) / 2.0

            eu = (cx - u0) / rr
            ev = (cy - v0) / rr
            en = math.hypot(eu, ev)

            # Radius just outside preferred band can still be the true disk
            # when scale/zoom shifts; accept only if already near frame center.
            if rr < pref_min_rr or rr > pref_max_rr:
                if en > float(self.settings.out_of_band_reject_radii):
                    logger.debug(
                        "[SunCenter] Ignoring off-center out-of-band disk radius %.2fpx (err=%.3f)",
                        rr,
                        en,
                    )
                    return
                logger.debug(
                    "[SunCenter] Accepting near-center disk radius %.2fpx outside preferred [%.2f, %.2f]",
                    rr,
                    pref_min_rr,
                    pref_max_rr,
                )

            self.disk_detected = True
            self.disk_info = dict(disk_info)
            self.error_u = float(eu)
            self.error_v = float(ev)
            self.error_norm = float(en)
        except Exception:
            self.disk_detected = False
            self.disk_info = None
            self.error_u = None
            self.error_v = None
            self.error_norm = None

    def _center_flux_indicates_center_hint(self) -> bool:
        core_mean = self.center_flux_core_mean
        core_to_ring = self.center_flux_core_to_ring
        core_to_frame = self.center_flux_core_to_frame

        if core_mean is None or core_to_ring is None or core_to_frame is None:
            return False
        if core_mean < float(self.settings.acquire_flux_min_core_mean):
            return False
        # SC-07: both ratios must pass to avoid false triggers from clouds /
        # glare gradients / partial disk passages.
        frame_ok = core_to_frame >= float(self.settings.acquire_flux_core_to_frame)
        ring_ok = core_to_ring >= float(self.settings.acquire_flux_core_to_ring)
        return bool(frame_ok and ring_ok)

    def _safe_detector_status(self) -> Dict[str, Any]:
        try:
            data = self.adapter.get_detector_status()
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("[SunCenter] detector status read failed: %s", exc)
            return {}

    def _advance_axis_probe(self, axis: int, cur_u: float, cur_v: float) -> None:
        """Advance one phase of the non-blocking axis sign probe (SC-02 / SC-06).

        Called once per tick from _handle_fine_center.  Each invocation moves the
        probe forward by one phase and returns immediately — no time.sleep().

        Phases: idle → pulsing → settling → (idle, probe done)
        """
        now = time.monotonic()

        if self._probe_state == "idle":
            # Snapshot both components before the pulse (for cross-coupling, SC-06).
            self._probe_before_u = cur_u
            self._probe_before_v = cur_v
            try:
                self.adapter.move_axis(axis, +float(self.settings.probe_rate_deg_s))
            except Exception as exc:
                logger.debug("[SunCenter] probe move_axis failed (axis=%d): %s", axis, exc)
                self._axis_probe_done[axis] = True
                return
            self._probe_deadline_mono = now + float(self.settings.probe_pulse_seconds)
            self._probe_state = "pulsing"
            self.state_message = f"Probing axis {axis} sign — pulsing"

        elif self._probe_state == "pulsing":
            if now < self._probe_deadline_mono:
                self.state_message = f"Probing axis {axis} sign — pulsing"
                return
            try:
                self.adapter.stop_axes()
            except Exception:
                pass
            self._probe_deadline_mono = now + float(self.settings.probe_settle_seconds)
            self._probe_state = "settling"
            self.state_message = f"Probing axis {axis} sign — settling"

        elif self._probe_state == "settling":
            if now < self._probe_deadline_mono:
                self.state_message = f"Probing axis {axis} sign — settling"
                return

            # Re-read detector after settle and determine sign.
            det = self._safe_detector_status()
            self._refresh_error_snapshot(det)

            sign = self._axis_sign.get(axis, 1)
            if self.error_norm is not None:
                after_u = float(self.error_u or 0.0)
                after_v = float(self.error_v or 0.0)
                delta_u = after_u - self._probe_before_u
                delta_v = after_v - self._probe_before_v
                primary_delta = delta_u if axis == 0 else delta_v
                cross_delta = delta_v if axis == 0 else delta_u

                if abs(primary_delta) >= float(self.settings.probe_min_effect):
                    sign = +1 if primary_delta < 0 else -1

                    # SC-06: compute and log cross-axis coupling.
                    coupling_ratio = (
                        abs(cross_delta) / abs(primary_delta) if abs(primary_delta) > 0 else 0.0
                    )
                    rotation_deg = math.degrees(math.atan2(abs(cross_delta), abs(primary_delta)))
                    logger.info(
                        "[SunCenter] axis %d probe: primary_Δ=%.4f cross_Δ=%.4f "
                        "coupling=%.0f%% image_rotation_hint=%.1f° → sign=%+d",
                        axis,
                        primary_delta,
                        cross_delta,
                        coupling_ratio * 100.0,
                        rotation_deg,
                        sign,
                    )
                    if coupling_ratio > 0.30:
                        logger.warning(
                            "[SunCenter] axis %d cross-coupling %.0f%% exceeds 30%% — "
                            "image axes rotated ~%.0f° from mount axes; "
                            "U/V decomposition may produce a spiralling rather than converging trajectory",
                            axis,
                            coupling_ratio * 100.0,
                            rotation_deg,
                        )

                    # Save axis-0 deltas so axis-1 probe can compare (future matrix fix).
                    if axis == 0:
                        self._probe_axis0_delta_u = delta_u
                        self._probe_axis0_delta_v = delta_v
                else:
                    logger.debug(
                        "[SunCenter] axis %d probe: |primary_Δ|=%.4f < min_effect=%.4f; "
                        "keeping sign=%+d",
                        axis,
                        abs(primary_delta),
                        self.settings.probe_min_effect,
                        sign,
                    )
            else:
                logger.debug(
                    "[SunCenter] axis %d probe: disk lost during settle; keeping sign=%+d",
                    axis,
                    sign,
                )

            self._axis_sign[axis] = sign
            self._axis_probe_done[axis] = True
            self._probe_state = "idle"
            self.state_message = f"Axis {axis} probe complete: sign={sign:+d}"

    def _update_flail(self, current_err_norm: float, saturated: bool) -> None:
        prev = self._prev_err_norm
        if prev is None:
            self._prev_err_norm = current_err_norm
            return

        improved = (prev - current_err_norm) >= float(self.settings.flail_min_improvement)
        if (not improved) and saturated:
            self._flail_counter += 1
        else:
            self._flail_counter = max(0, self._flail_counter - 1)

        self._prev_err_norm = current_err_norm

        if (
            self.tolerance_mode == self.MODE_STRICT
            and self._flail_counter >= int(self.settings.flail_cycles)
        ):
            self.tolerance_mode = self.MODE_CONSERVATIVE
            self._flail_counter = 0
            self.state_message = "Flailing detected; switched to conservative tolerance"
            logger.warning("[SunCenter] Strict -> conservative fallback due to flail")

    def _current_tolerance(self) -> float:
        if self.tolerance_mode == self.MODE_CONSERVATIVE:
            return float(self.settings.conservative_tolerance_radii)
        return float(self.settings.strict_tolerance_radii)

    def _reset_controller(self, full: bool) -> None:
        self._int_u = 0.0
        self._int_v = 0.0
        self._hold_start_mono = None
        self._prev_err_norm = None
        self._flail_counter = 0
        self._center_flux_hit_count = 0
        self._center_flux_hold_until_mono = 0.0
        self._lock_lost_grace_until_mono = 0.0
        self._probe_state = "idle"  # SC-02: always clear mid-probe state on reset
        if full:
            self.tolerance_mode = self.MODE_STRICT
            self._axis_probe_done = {0: False, 1: False}
            self._axis_sign = {0: 1, 1: 1}

    def _transition(self, new_state: str, message: str) -> None:
        if self.state != new_state:
            logger.info("[SunCenter] %s -> %s (%s)", self.state, new_state, message)
        self.state = new_state
        self.state_message = message
        self.state_changed_at = time.time()


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
