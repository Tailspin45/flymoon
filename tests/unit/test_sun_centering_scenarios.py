"""Integration-style scenario tests for the Sun-centering service.

These tests exercise multi-step sequences without the background thread —
ticks are driven manually so timing is deterministic.  Two scenarios are
covered:

1. Start / stop lifecycle: service transitions through ACQUIRE → FAIL_SAFE
   (immediate, because the fake adapter simulates scope not in solar mode),
   and stop() returns the service to STOPPED with get_status() reporting the
   expected shape.

2. Disc-loss and recovery path: a service already in TRACK loses the disk
   past the grace window, moves to RECOVER, re-acquires, and returns to
   CENTER / TRACK.
"""

import time

import pytest

from src.sun_centering import SunCenteringAdapter, SunCenteringService, SunCenteringSettings


# ---------------------------------------------------------------------------
# Shared fake adapter (same contract as test_sun_centering.py fixture)
# ---------------------------------------------------------------------------

class _FakeAdapter:
    def __init__(self):
        self.connected_scope = True
        self.connected_alpaca = True
        self.mode = "sun"
        self.sun_altaz = (45.0, 180.0)
        self.slewing = False
        self.position = {"alt": 45.0, "az": 180.0, "ra": 0.0, "dec": 0.0}
        self.detector_status = {
            "disk_detected": True,
            "disk_info": {"cx": 159.5, "cy": 89.5, "radius": 25.0},
            "analysis_resolution": "320x180@30fps",
        }
        self.goto_calls: list = []
        self.stop_calls: int = 0
        self.tracking_calls: list = []

    def as_adapter(self) -> SunCenteringAdapter:
        return SunCenteringAdapter(
            is_scope_connected=lambda: self.connected_scope,
            is_alpaca_connected=lambda: self.connected_alpaca,
            get_viewing_mode=lambda: self.mode,
            get_sun_altaz=lambda: self.sun_altaz,
            goto_altaz=self._goto_altaz,
            is_slewing=lambda: self.slewing,
            stop_axes=self._stop_axes,
            get_detector_status=lambda: dict(self.detector_status),
            get_position=lambda: dict(self.position),
            set_tracking=self._set_tracking,
        )

    def _goto_altaz(self, alt, az):
        self.goto_calls.append((alt, az))
        return {"success": True}

    def _stop_axes(self):
        self.stop_calls += 1
        return {"success": True}

    def _set_tracking(self, enabled):
        self.tracking_calls.append(enabled)
        return {"success": True}


# ---------------------------------------------------------------------------
# Scenario 1: Start / Stop lifecycle
# ---------------------------------------------------------------------------

class TestStartStopLifecycle:
    """Verify that start()/stop() and get_status() behave correctly without
    actually running the background thread."""

    def test_initial_state_is_stopped(self):
        fake = _FakeAdapter()
        svc = SunCenteringService(adapter=fake.as_adapter())
        status = svc.get_status()
        assert status["running"] is False
        assert status["state"] == SunCenteringService.STATE_STOPPED

    def test_get_status_payload_shape_when_idle(self):
        fake = _FakeAdapter()
        svc = SunCenteringService(adapter=fake.as_adapter())
        status = svc.get_status()
        required = {
            "running", "state", "phase", "message", "uptime_s",
            "disk_detected", "error_radii", "error_u_px", "error_v_px",
            "jacobian_valid", "jacobian_age_s",
            "recovery_attempts", "acquisition_attempts", "center_iter_count",
            "last_command", "last_error", "tick_age_s",
        }
        missing = required - set(status.keys())
        assert not missing, f"Status missing keys: {missing}"

    def test_running_flag_set_after_start(self):
        fake = _FakeAdapter()
        svc = SunCenteringService(adapter=fake.as_adapter())
        svc.start()
        try:
            assert svc.is_running() is True
            assert svc.get_status()["running"] is True
        finally:
            svc.stop()

    def test_state_is_stopped_after_stop(self):
        fake = _FakeAdapter()
        svc = SunCenteringService(adapter=fake.as_adapter())
        svc.start()
        svc.stop()
        status = svc.get_status()
        assert status["running"] is False
        assert status["state"] == SunCenteringService.STATE_STOPPED

    def test_stop_when_not_running_is_idempotent(self):
        fake = _FakeAdapter()
        svc = SunCenteringService(adapter=fake.as_adapter())
        svc.stop()  # should not raise
        assert svc.is_running() is False

    def test_recenter_returns_false_when_stopped(self):
        fake = _FakeAdapter()
        svc = SunCenteringService(adapter=fake.as_adapter())
        assert svc.recenter() is False

    def test_started_at_is_set_on_start(self):
        fake = _FakeAdapter()
        svc = SunCenteringService(adapter=fake.as_adapter())
        before = time.time()
        svc.start()
        try:
            assert svc.started_at is not None
            assert svc.started_at >= before
        finally:
            svc.stop()

    def test_counters_reset_on_start(self):
        fake = _FakeAdapter()
        svc = SunCenteringService(adapter=fake.as_adapter())
        # Pollute counters as if a prior run had happened.
        svc.recovery_attempts = 7
        svc.acquisition_attempts = 3
        svc.start()
        try:
            assert svc.recovery_attempts == 0
            assert svc.acquisition_attempts == 0
        finally:
            svc.stop()

    def test_uptime_increases_while_running(self):
        fake = _FakeAdapter()
        svc = SunCenteringService(adapter=fake.as_adapter())
        svc.start()
        try:
            time.sleep(0.1)
            assert svc.get_status()["uptime_s"] > 0.0
        finally:
            svc.stop()


# ---------------------------------------------------------------------------
# Scenario 2: Disc-loss and recovery path (tick-driven, no thread)
# ---------------------------------------------------------------------------

class TestDiscLossRecoveryPath:
    """Drive the service tick-by-tick through a TRACK → disc-loss → RECOVER →
    re-acquire → CENTER sequence."""

    def _make_tracking_service(self, fake: _FakeAdapter) -> SunCenteringService:
        """Return a service already in TRACK with a valid Jacobian."""
        settings = SunCenteringSettings(
            lock_lost_grace_s=0.0,   # no grace, transitions immediately
            recover_settle_s=0.0,
            center_settle_s=0.0,
        )
        svc = SunCenteringService(adapter=fake.as_adapter(), settings=settings)
        svc._running = True
        svc.state = SunCenteringService.STATE_TRACK
        svc._phase = "idle"
        svc._jacobian_valid = True
        svc._j = [[10.0, 0.0], [0.0, 10.0]]
        svc._j_inv = [[0.1, 0.0], [0.0, 0.1]]
        # Grace window already expired so first disc-loss tick trips RECOVER.
        svc._lock_lost_until_mono = time.monotonic() - 0.1
        svc._next_refresh_mono = time.monotonic() + 100.0
        return svc

    # ── 2a. Tracking → disc lost → RECOVER ──────────────────────────────────

    def test_track_to_recover_on_disc_loss(self):
        fake = _FakeAdapter()
        fake.detector_status = {
            "disk_detected": False,
            "center_flux": {"core_mean": 0.5, "core_to_ring": 1.0, "core_to_frame": 1.0},
        }
        svc = self._make_tracking_service(fake)

        svc._tick_once()

        assert svc.state == SunCenteringService.STATE_RECOVER
        assert fake.stop_calls >= 1

    # ── 2b. RECOVER issues goto to ephemeris ────────────────────────────────

    def test_recover_initial_issues_goto(self):
        fake = _FakeAdapter()
        fake.detector_status = {
            "disk_detected": False,
            "center_flux": {"core_mean": 0.5, "core_to_ring": 1.0, "core_to_frame": 1.0},
        }
        svc = self._make_tracking_service(fake)

        # Tick 1: disc lost → RECOVER (initial phase).
        svc._tick_once()
        assert svc.state == SunCenteringService.STATE_RECOVER

        # Tick 2: RECOVER initial → goto issued.
        svc._tick_once()
        assert len(fake.goto_calls) >= 1
        assert svc._phase == "slewing"

    # ── 2c. RECOVER assess: disc found → back to CENTER ─────────────────────

    def test_recover_assess_returns_to_center_when_disk_found(self):
        fake = _FakeAdapter()
        # Disc present and centred.
        fake.detector_status = {
            "disk_detected": True,
            "analysis_resolution": "320x180@30fps",
            "disk_info": {"cx": 159.5, "cy": 89.5, "radius": 25.0},
        }
        svc = self._make_tracking_service(fake)
        svc.state = SunCenteringService.STATE_RECOVER
        svc._phase = "assess"

        svc._tick_once()

        assert svc.state == SunCenteringService.STATE_CENTER
        assert svc._phase == "check"
        assert svc._center_iter_count == 0

    # ── 2d. RECOVER assess: flux but no disk → ACQUIRE + counter ────────────

    def test_recover_assess_pointing_error_increments_attempts(self):
        fake = _FakeAdapter()
        fake.detector_status = {
            "disk_detected": False,
            "center_flux": {"core_mean": 80.0, "core_to_ring": 1.0, "core_to_frame": 1.0},
        }
        svc = self._make_tracking_service(fake)
        svc.state = SunCenteringService.STATE_RECOVER
        svc._phase = "assess"
        initial_recovery = svc.recovery_attempts

        svc._tick_once()

        assert svc.state == SunCenteringService.STATE_ACQUIRE
        assert svc.recovery_attempts == initial_recovery + 1
        assert svc.acquisition_attempts >= 1

    # ── 2e. Full TRACK → RECOVER → ACQUIRE → CENTER chain ───────────────────

    def test_full_disc_loss_recovery_chain(self):
        """Multi-tick chain: TRACK loses disc, enters RECOVER, re-acquires."""
        fake = _FakeAdapter()

        # Phase A: disc missing.
        fake.detector_status = {
            "disk_detected": False,
            "center_flux": {"core_mean": 80.0, "core_to_ring": 1.0, "core_to_frame": 1.0},
        }
        svc = self._make_tracking_service(fake)

        # Tick 1: TRACK sees disc loss past grace → RECOVER.
        svc._tick_once()
        assert svc.state == SunCenteringService.STATE_RECOVER

        # Tick 2: RECOVER initial → issues goto to ephemeris.
        svc._tick_once()
        assert svc._phase == "slewing"

        # Simulate slew complete.
        svc.adapter.is_slewing = lambda: False  # type: ignore[method-assign]
        svc._slew_deadline_mono = time.monotonic() + 10.0

        # Tick 3: RECOVER slewing → settling (settle_s=0 → goes straight to assess).
        svc._tick_once()
        assert svc._phase in {"settling", "assess"}

        # Fast-forward past any settle delay.
        svc._settle_until_mono = time.monotonic() - 0.1
        if svc._phase == "settling":
            svc._tick_once()
        assert svc._phase == "assess"

        # Phase B: disc re-appears at centre.
        fake.detector_status = {
            "disk_detected": True,
            "analysis_resolution": "320x180@30fps",
            "disk_info": {"cx": 159.5, "cy": 89.5, "radius": 25.0},
        }

        # Tick: RECOVER assess → disc found → CENTER.
        svc._tick_once()
        assert svc.state == SunCenteringService.STATE_CENTER
        assert svc._phase == "check"

        # Tick: CENTER check → error within tolerance → TRACK.
        svc._tick_once()
        assert svc.state == SunCenteringService.STATE_TRACK

    # ── 2f. Cloud: RECOVER enters cloud_wait on dark frame ──────────────────

    def test_recover_enters_cloud_wait_on_dark_frame(self):
        fake = _FakeAdapter()
        fake.detector_status = {
            "disk_detected": False,
            "center_flux": {"core_mean": 0.5, "core_to_ring": 1.0, "core_to_frame": 1.0},
        }
        svc = self._make_tracking_service(fake)
        svc.state = SunCenteringService.STATE_RECOVER
        svc._phase = "assess"

        svc._tick_once()

        assert svc.state == SunCenteringService.STATE_RECOVER
        assert svc._phase == "cloud_wait"
        assert "cloud" in svc.state_message.lower()

    # ── 2g. Cloud: disc returns after cloud → CENTER ─────────────────────────

    def test_cloud_wait_transitions_to_center_when_disk_returns(self):
        fake = _FakeAdapter()
        fake.detector_status = {
            "disk_detected": True,
            "analysis_resolution": "320x180@30fps",
            "disk_info": {"cx": 159.5, "cy": 89.5, "radius": 25.0},
        }
        svc = self._make_tracking_service(fake)
        svc.state = SunCenteringService.STATE_RECOVER
        svc._phase = "cloud_wait"
        svc._cloud_start_mono = time.monotonic() - 10.0

        svc._tick_once()

        assert svc.state == SunCenteringService.STATE_CENTER

    # ── 2h. Cloud timeout → FAIL_SAFE ────────────────────────────────────────

    def test_cloud_wait_timeout_restarts_acquire(self):
        """After cloud_wait_max, service re-enters ACQUIRE (not FAIL_SAFE) so it
        self-heals when the sky eventually clears."""
        fake = _FakeAdapter()
        fake.detector_status = {
            "disk_detected": False,
            "center_flux": {"core_mean": 0.5, "core_to_ring": 1.0, "core_to_frame": 1.0},
        }
        settings = SunCenteringSettings(cloud_wait_max_s=1.0)
        svc = SunCenteringService(adapter=fake.as_adapter(), settings=settings)
        svc._running = True
        svc._jacobian_valid = True
        svc.state = SunCenteringService.STATE_RECOVER
        svc._phase = "cloud_wait"
        # Cloud started well past the max wait.
        svc._cloud_start_mono = time.monotonic() - 5.0

        svc._tick_once()

        assert svc.state == SunCenteringService.STATE_ACQUIRE
        assert svc.recovery_attempts >= 1
