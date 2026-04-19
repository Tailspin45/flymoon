"""Unit tests for Sun-centering service v2 (GoTo-correction / Jacobian model)."""

import math
import time

import pytest

from src.sun_centering import SunCenteringAdapter, SunCenteringService, SunCenteringSettings


# ---------------------------------------------------------------------------
# Fake adapter fixture
# ---------------------------------------------------------------------------

class _FakeAdapter:
    """Minimal controllable stand-in for the real adapter callbacks."""

    def __init__(self):
        self.connected_scope = True
        self.connected_alpaca = True
        self.mode = "sun"
        self.sun_altaz = (45.0, 180.0)
        self.slewing = False
        self.position = {"alt": 45.0, "az": 180.0, "ra": 0.0, "dec": 0.0}
        self.detector_status = {
            "disk_detected": True,
            "disk_info": {"cx": 179.5, "cy": 89.5, "radius": 25.0},
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

    def _set_tracking(self, enabled: bool):
        self.tracking_calls.append(enabled)
        return {"success": True}


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------

def test_adapter_fields_are_callable():
    fake = _FakeAdapter()
    adapter = fake.as_adapter()
    assert adapter.is_scope_connected() is True
    assert adapter.is_alpaca_connected() is True
    assert adapter.get_viewing_mode() == "sun"
    assert adapter.get_sun_altaz() == (45.0, 180.0)
    assert adapter.is_slewing() is False
    pos = adapter.get_position()
    assert "alt" in pos and "az" in pos


# ---------------------------------------------------------------------------
# Disk snapshot parsing
# ---------------------------------------------------------------------------

def test_refresh_disk_snapshot_parses_centered_disk():
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    # Disk perfectly at image centre (320x180 → cx=159.5, cy=89.5).
    det = {
        "disk_detected": True,
        "analysis_resolution": "320x180@30fps",
        "disk_info": {"cx": 159.5, "cy": 89.5, "radius": 25.0},
    }
    service._refresh_disk_snapshot(det)
    assert service.disk_detected is True
    assert service.disk_eu_px == pytest.approx(0.0, abs=1e-6)
    assert service.disk_ev_px == pytest.approx(0.0, abs=1e-6)
    assert service.error_radii == pytest.approx(0.0, abs=1e-6)


def test_refresh_disk_snapshot_computes_error_radii_for_offset_disk():
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    # Disk shifted 20 px right from centre.
    det = {
        "disk_detected": True,
        "analysis_resolution": "320x180@30fps",
        "disk_info": {"cx": 179.5, "cy": 89.5, "radius": 25.0},
    }
    service._refresh_disk_snapshot(det)
    assert service.disk_detected is True
    assert service.disk_eu_px == pytest.approx(20.0, abs=1e-6)
    assert service.disk_ev_px == pytest.approx(0.0, abs=1e-6)
    assert service.error_radii == pytest.approx(20.0 / 25.0, abs=1e-6)


def test_refresh_disk_snapshot_no_disk_clears_fields():
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    det = {"disk_detected": False}
    service._refresh_disk_snapshot(det)
    assert service.disk_detected is False
    assert service.error_radii is None
    assert service.disk_eu_px is None


def test_refresh_disk_snapshot_rejects_implausible_radius_too_small():
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    # 320x180: min_dim=180, hard_min=max(6, 180*0.07)=12.6 → radius=5 rejected.
    det = {
        "disk_detected": True,
        "analysis_resolution": "320x180@30fps",
        "disk_info": {"cx": 159.5, "cy": 89.5, "radius": 5.0},
    }
    service._refresh_disk_snapshot(det)
    assert service.disk_detected is False


def test_refresh_disk_snapshot_rejects_implausible_radius_too_large():
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    # 320x180: min_dim=180, hard_max=min(171, 111.6)=111.6 → radius=115 rejected.
    det = {
        "disk_detected": True,
        "analysis_resolution": "320x180@30fps",
        "disk_info": {"cx": 159.5, "cy": 89.5, "radius": 115.0},
    }
    service._refresh_disk_snapshot(det)
    assert service.disk_detected is False


def test_refresh_disk_snapshot_records_flux_even_without_disk():
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    det = {
        "disk_detected": False,
        "center_flux": {"core_mean": 12.5, "core_to_ring": 1.0, "core_to_frame": 1.0},
    }
    service._refresh_disk_snapshot(det)
    assert service.disk_detected is False
    assert service.center_flux_core_mean == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# Connection / mode guards in _tick_once
# ---------------------------------------------------------------------------

def test_tick_once_enters_fail_safe_when_scope_disconnected():
    fake = _FakeAdapter()
    fake.connected_scope = False
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    service._phase = "initial"
    service._tick_once()
    assert service.state == SunCenteringService.STATE_FAIL_SAFE


def test_tick_once_enters_fail_safe_when_alpaca_disconnected():
    fake = _FakeAdapter()
    fake.connected_alpaca = False
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    service._phase = "initial"
    service._tick_once()
    assert service.state == SunCenteringService.STATE_FAIL_SAFE


def test_tick_once_enters_fail_safe_when_not_solar_mode():
    fake = _FakeAdapter()
    fake.mode = "deep_sky"
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    service._phase = "initial"
    service._tick_once()
    assert service.state == SunCenteringService.STATE_FAIL_SAFE
    assert "solar mode" in service.state_message.lower()


def test_tick_once_accepts_solar_mode_variant():
    """'solar' as well as 'sun' should be accepted."""
    fake = _FakeAdapter()
    fake.mode = "solar"
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    service._phase = "initial"
    service._tick_once()
    assert service.state != SunCenteringService.STATE_FAIL_SAFE


# ---------------------------------------------------------------------------
# ACQUIRE state
# ---------------------------------------------------------------------------

def test_acquire_initial_enters_fail_safe_when_sun_too_low():
    fake = _FakeAdapter()
    fake.sun_altaz = (5.0, 180.0)  # below default min_sun_alt_deg = 8.0
    fake.detector_status = {"disk_detected": False}
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    service._phase = "initial"
    service._tick_once()
    assert service.state == SunCenteringService.STATE_FAIL_SAFE


def test_acquire_initial_waits_when_mount_is_slewing():
    fake = _FakeAdapter()
    fake.slewing = True
    fake.detector_status = {"disk_detected": False}
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    service._phase = "initial"
    # Simulate start() having set the busy-timeout deadline into the future.
    service._precheck_busy_deadline_mono = time.monotonic() + 60.0
    service._tick_once()
    assert service.state == SunCenteringService.STATE_ACQUIRE
    assert fake.goto_calls == []
    assert "slewing" in service.state_message.lower()


def test_acquire_initial_issues_goto_to_ephemeris():
    fake = _FakeAdapter()
    fake.slewing = False
    fake.detector_status = {"disk_detected": False}
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    service._phase = "initial"
    service._tick_once()
    assert len(fake.goto_calls) == 1
    assert fake.goto_calls[0] == pytest.approx((45.0, 180.0), abs=0.01)
    assert service._phase == "slewing"


def test_acquire_assess_enters_calibrate_when_disk_found():
    """Disc found with no valid Jacobian → enter CALIBRATE to measure Jacobian first."""
    fake = _FakeAdapter()
    fake.detector_status = {
        "disk_detected": True,
        "analysis_resolution": "320x180@30fps",
        "disk_info": {"cx": 159.5, "cy": 89.5, "radius": 25.0},
    }
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    service._phase = "assess"
    service._jacobian_valid = False
    service._tick_once()
    assert service.state == SunCenteringService.STATE_CALIBRATE




def test_acquire_assess_skips_calibrate_when_jacobian_cached():
    fake = _FakeAdapter()
    fake.detector_status = {
        "disk_detected": True,
        "analysis_resolution": "320x180@30fps",
        "disk_info": {"cx": 159.5, "cy": 89.5, "radius": 25.0},
    }
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    service._phase = "assess"
    service._jacobian_valid = True
    service._j = [[10.0, 0.0], [0.0, 10.0]]
    service._j_inv = [[0.1, 0.0], [0.0, 0.1]]
    service._tick_once()
    assert service.state == SunCenteringService.STATE_CENTER


def test_build_search_offsets_count_matches_radii_config():
    fake = _FakeAdapter()
    settings = SunCenteringSettings(acquire_search_radii_deg=(0.4, 0.8))
    service = SunCenteringService(adapter=fake.as_adapter(), settings=settings)
    offsets = service._build_search_offsets()
    # Each radius produces 4 cardinal + 4 diagonal points.
    assert len(offsets) == 2 * 8
    assert (0.4, 0.0) in offsets
    assert (-0.4, 0.0) in offsets
    assert (0.0, 0.4) in offsets
    assert (0.0, -0.4) in offsets


def test_build_search_offsets_widens_on_retry():
    """acquisition_attempts > 0 should switch to the wider retry radii."""
    fake = _FakeAdapter()
    settings = SunCenteringSettings(
        acquire_search_radii_deg=(0.4, 0.8),
        acquire_search_radii_retry_deg=(0.4, 0.8, 1.2),
    )
    service = SunCenteringService(adapter=fake.as_adapter(), settings=settings)

    # First attempt uses narrow grid.
    service.acquisition_attempts = 0
    first = service._build_search_offsets()
    assert len(first) == 2 * 8

    # After one failed attempt, uses wider grid.
    service.acquisition_attempts = 1
    retry = service._build_search_offsets()
    assert len(retry) == 3 * 8
    assert (1.2, 0.0) in retry


def test_acquire_grid_exhausted_rests_then_retries_instead_of_fail_safe():
    """When the search grid is fully exhausted the service should enter a rest
    phase and increment acquisition_attempts, NOT enter FAIL_SAFE."""
    fake = _FakeAdapter()
    fake.detector_status = {"disk_detected": False}
    settings = SunCenteringSettings(acquire_retry_rest_s=0.0)
    service = SunCenteringService(adapter=fake.as_adapter(), settings=settings)
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    # Put it at the end of a 2-point search to keep the test short.
    service._search_offsets = [(0.4, 0.0), (-0.4, 0.0)]
    service._search_idx = 2   # past end
    service._phase = "search_step"
    service._search_sun_alt = 45.0
    service._search_sun_az = 180.0

    service._tick_once()

    assert service.state == SunCenteringService.STATE_ACQUIRE
    assert service._phase == "resting"
    assert service.acquisition_attempts == 1
    assert "retrying" in service.state_message.lower()


def test_acquire_initial_fail_safe_when_sun_below_horizon_floor():
    """Quadrant horizon floor higher than min_sun_alt_deg should also block start."""
    fake = _FakeAdapter()
    fake.sun_altaz = (12.0, 180.0)   # south quadrant, 12° alt
    fake.detector_status = {"disk_detected": False}
    adapter = fake.as_adapter()
    # Override with a horizon floor of 20° for the south quadrant (az≈180°).
    adapter.get_horizon_min_alt = lambda az: 20.0
    service = SunCenteringService(adapter=adapter)
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    service._phase = "initial"
    service._tick_once()
    assert service.state == SunCenteringService.STATE_FAIL_SAFE
    assert "floor" in service.state_message.lower()


def test_acquire_search_step_skips_positions_below_horizon_floor():
    """Grid search points below the quadrant floor must be skipped without a GoTo."""
    fake = _FakeAdapter()
    fake.detector_status = {"disk_detected": False}
    adapter = fake.as_adapter()
    # Set a high floor (40°) so the offsets (which lower alt) fall below it.
    adapter.get_horizon_min_alt = lambda az: 40.0
    service = SunCenteringService(adapter=adapter)
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    # Position sun near the floor so negative alt offsets dip below it.
    service._search_sun_alt = 41.0
    service._search_sun_az = 180.0
    # One offset that dips below floor, one that stays above.
    service._search_offsets = [(-3.0, 0.0), (2.0, 0.0)]
    service._search_idx = 0
    service._phase = "search_step"

    # Tick 1: -3° offset → alt=38° < floor 40° → skipped, index advances.
    service._tick_once()
    assert len(fake.goto_calls) == 0
    assert service._search_idx == 1

    # Tick 2: +2° offset → alt=43° > floor 40° → GoTo issued.
    service._tick_once()
    assert len(fake.goto_calls) == 1
    assert service._phase == "search_slewing"


def test_goto_clamped_respects_horizon_floor():
    """_goto_clamped must not issue a GoTo below the quadrant floor."""
    fake = _FakeAdapter()
    adapter = fake.as_adapter()
    adapter.get_horizon_min_alt = lambda az: 25.0
    service = SunCenteringService(adapter=adapter)
    service._running = True

    # Request an altitude below the floor.
    service._goto_clamped(10.0, 180.0)

    assert len(fake.goto_calls) == 1
    issued_alt = fake.goto_calls[0][0]
    assert issued_alt >= 25.0   # clamped up to floor + 0.10


def test_acquire_resting_phase_restarts_from_initial_after_rest():
    """After the rest period expires the phase resets to 'initial' for a fresh
    ephemeris GoTo."""
    fake = _FakeAdapter()
    fake.detector_status = {"disk_detected": False}
    settings = SunCenteringSettings(acquire_retry_rest_s=0.0)
    service = SunCenteringService(adapter=fake.as_adapter(), settings=settings)
    service._running = True
    service.state = SunCenteringService.STATE_ACQUIRE
    service._phase = "resting"
    service._acquire_rest_until_mono = time.monotonic() - 0.1  # already expired

    service._tick_once()

    assert service._phase == "initial"


# ---------------------------------------------------------------------------
# recenter()
# ---------------------------------------------------------------------------

def test_recenter_resets_state_to_acquire():
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_TRACK
    service._phase = "idle"
    service.recovery_attempts = 3
    service._center_iter_count = 5

    ok = service.recenter()

    assert ok is True
    assert service.state == SunCenteringService.STATE_ACQUIRE
    assert service._phase == "initial"
    assert service.recovery_attempts == 0
    assert service._center_iter_count == 0


def test_recenter_when_not_running_returns_false():
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    assert service.recenter() is False
    assert service.state == SunCenteringService.STATE_STOPPED


def test_recenter_preserves_jacobian_when_valid():
    """recenter() should keep a valid Jacobian so CALIBRATE is skipped on re-acquire."""
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service._jacobian_valid = True
    service.state = SunCenteringService.STATE_TRACK

    service.recenter()

    assert service._jacobian_valid is True


# ---------------------------------------------------------------------------
# CENTER state
# ---------------------------------------------------------------------------

def test_center_transitions_to_track_when_within_tolerance():
    fake = _FakeAdapter()
    # Disk close to centre: error_radii will be ~0.04 < default tolerance of 0.12.
    fake.detector_status = {
        "disk_detected": True,
        "analysis_resolution": "320x180@30fps",
        "disk_info": {"cx": 160.5, "cy": 89.5, "radius": 25.0},  # 1 px off
    }
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_CENTER
    service._phase = "check"
    service._jacobian_valid = True
    service._j = [[10.0, 0.0], [0.0, 10.0]]
    service._j_inv = [[0.1, 0.0], [0.0, 0.1]]
    service._tick_once()
    assert service.state == SunCenteringService.STATE_TRACK


def test_center_issues_goto_correction_when_outside_tolerance():
    fake = _FakeAdapter()
    # Disk 30 px right of centre → error_radii = 30/25 = 1.2 >> tolerance.
    fake.detector_status = {
        "disk_detected": True,
        "analysis_resolution": "320x180@30fps",
        "disk_info": {"cx": 189.5, "cy": 89.5, "radius": 25.0},
    }
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_CENTER
    service._phase = "check"
    service._jacobian_valid = True
    service._j = [[10.0, 0.0], [0.0, 10.0]]
    service._j_inv = [[0.1, 0.0], [0.0, 0.1]]
    service._tick_once()
    assert len(fake.goto_calls) == 1
    assert service._phase == "slewing"


def test_center_enters_recover_after_max_iterations():
    fake = _FakeAdapter()
    # Disk permanently far off centre.
    fake.detector_status = {
        "disk_detected": True,
        "analysis_resolution": "320x180@30fps",
        "disk_info": {"cx": 189.5, "cy": 89.5, "radius": 25.0},
    }
    settings = SunCenteringSettings(max_center_iters=2, center_settle_s=0.0)
    service = SunCenteringService(adapter=fake.as_adapter(), settings=settings)
    service._running = True
    service.state = SunCenteringService.STATE_CENTER
    service._phase = "check"
    service._jacobian_valid = True
    service._j = [[10.0, 0.0], [0.0, 10.0]]
    service._j_inv = [[0.1, 0.0], [0.0, 0.1]]
    service._center_iter_count = 2  # already at limit

    service._tick_once()

    assert service.state == SunCenteringService.STATE_RECOVER
    assert fake.stop_calls >= 1


def test_center_enters_recover_after_sustained_disk_loss():
    fake = _FakeAdapter()
    fake.detector_status = {"disk_detected": False}
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_CENTER
    service._phase = "check"
    service._center_no_disk_ticks = 2  # next tick is the 3rd → triggers recover

    service._tick_once()

    assert service.state == SunCenteringService.STATE_RECOVER


# ---------------------------------------------------------------------------
# TRACK state
# ---------------------------------------------------------------------------

def test_track_stays_in_track_within_grace_after_disk_loss():
    fake = _FakeAdapter()
    fake.detector_status = {"disk_detected": False}
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_TRACK
    service._phase = "idle"
    service._lock_lost_until_mono = time.monotonic() + 5.0  # grace window open

    service._tick_once()

    assert service.state == SunCenteringService.STATE_TRACK
    assert "grace" in service.state_message.lower()


def test_track_enters_recover_after_grace_expires():
    fake = _FakeAdapter()
    fake.detector_status = {"disk_detected": False}
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_TRACK
    service._phase = "idle"
    service._lock_lost_until_mono = time.monotonic() - 0.1  # grace already expired

    service._tick_once()

    assert service.state == SunCenteringService.STATE_RECOVER
    assert fake.stop_calls >= 1


def test_track_refreshes_grace_timer_when_disk_present():
    fake = _FakeAdapter()
    # Disk near centre so no correction is issued.
    fake.detector_status = {
        "disk_detected": True,
        "analysis_resolution": "320x180@30fps",
        "disk_info": {"cx": 160.0, "cy": 89.5, "radius": 25.0},
    }
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_TRACK
    service._phase = "idle"
    service._lock_lost_until_mono = time.monotonic() + 0.1  # almost expired
    service._next_refresh_mono = time.monotonic() + 100.0  # no refresh yet

    service._tick_once()

    assert service._lock_lost_until_mono > time.monotonic() + 1.0
    assert service.state == SunCenteringService.STATE_TRACK


# ---------------------------------------------------------------------------
# RECOVER state
# ---------------------------------------------------------------------------

def test_recover_issues_goto_to_ephemeris_on_initial_phase():
    fake = _FakeAdapter()
    fake.detector_status = {"disk_detected": False}
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_RECOVER
    service._phase = "initial"

    service._tick_once()

    assert len(fake.goto_calls) == 1
    assert fake.goto_calls[0] == pytest.approx((45.0, 180.0), abs=0.01)
    assert service._phase == "slewing"


def test_recover_assess_goes_to_center_when_disk_found():
    fake = _FakeAdapter()
    fake.detector_status = {
        "disk_detected": True,
        "analysis_resolution": "320x180@30fps",
        "disk_info": {"cx": 159.5, "cy": 89.5, "radius": 25.0},
    }
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_RECOVER
    service._phase = "assess"

    service._tick_once()

    assert service.state == SunCenteringService.STATE_CENTER
    assert service._center_iter_count == 0


def test_recover_assess_increments_attempts_and_goes_to_acquire_on_pointing_error():
    """Flux present (no cloud) but no disk → pointing error → ACQUIRE."""
    fake = _FakeAdapter()
    fake.detector_status = {
        "disk_detected": False,
        "center_flux": {
            "core_mean": 80.0,  # above cloud_floor_mean (4.0) → not cloud
            "core_to_ring": 1.0,
            "core_to_frame": 1.0,
        },
    }
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_RECOVER
    service._phase = "assess"
    assert service.recovery_attempts == 0

    service._tick_once()

    assert service.state == SunCenteringService.STATE_ACQUIRE
    assert service.recovery_attempts == 1


def test_recover_assess_enters_cloud_wait_when_frame_dark():
    """Dark frame (flux below cloud_floor_mean) → cloud wait mode."""
    fake = _FakeAdapter()
    fake.detector_status = {
        "disk_detected": False,
        "center_flux": {
            "core_mean": 1.0,  # below cloud_floor_mean (4.0) → cloud
            "core_to_ring": 1.0,
            "core_to_frame": 1.0,
        },
    }
    service = SunCenteringService(adapter=fake.as_adapter())
    service._running = True
    service.state = SunCenteringService.STATE_RECOVER
    service._phase = "assess"

    service._tick_once()

    assert service.state == SunCenteringService.STATE_RECOVER
    assert service._phase == "cloud_wait"
    assert "cloud" in service.state_message.lower()


def test_recover_is_infinite_retry_style():
    """recovery_attempts should keep incrementing with no cap → no FAIL_SAFE from count alone."""
    fake = _FakeAdapter()
    fake.detector_status = {
        "disk_detected": False,
        "center_flux": {"core_mean": 80.0, "core_to_ring": 1.0, "core_to_frame": 1.0},
    }
    settings = SunCenteringSettings(recover_settle_s=0.0)
    service = SunCenteringService(adapter=fake.as_adapter(), settings=settings)
    service._running = True

    for _ in range(5):
        service.state = SunCenteringService.STATE_RECOVER
        service._phase = "assess"
        service._tick_once()

    assert service.recovery_attempts == 5
    assert service.state == SunCenteringService.STATE_ACQUIRE
    assert service.state != SunCenteringService.STATE_FAIL_SAFE


# ---------------------------------------------------------------------------
# Jacobian correction math
# ---------------------------------------------------------------------------

def test_jacobian_correction_with_identity_inverse():
    """J_inv = I → correction equals pixel error in degrees (1 deg/px scale)."""
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    service._jacobian_valid = True
    service._j_inv = [[1.0, 0.0], [0.0, 1.0]]
    service.disk_eu_px = 10.0
    service.disk_ev_px = -5.0

    dalt, daz = service._jacobian_correction()

    assert dalt == pytest.approx(10.0, abs=1e-9)
    assert daz == pytest.approx(-5.0, abs=1e-9)


def test_jacobian_correction_with_scale_matrix():
    """J_inv = diag(0.1, 0.2) → correction scaled by respective gains."""
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    service._jacobian_valid = True
    service._j_inv = [[0.1, 0.0], [0.0, 0.2]]
    service.disk_eu_px = 20.0
    service.disk_ev_px = 10.0

    dalt, daz = service._jacobian_correction()

    assert dalt == pytest.approx(2.0, abs=1e-9)
    assert daz == pytest.approx(2.0, abs=1e-9)


def test_jacobian_correction_falls_back_to_plate_scale_when_no_jacobian():
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    service._jacobian_valid = False
    service._j_inv = None
    service.plate_scale_deg_per_px = 0.005
    service.disk_eu_px = 0.0
    service.disk_ev_px = 10.0  # disk below centre → negative dalt correction

    dalt, daz = service._jacobian_correction()

    assert dalt == pytest.approx(-0.05, abs=1e-9)
    assert daz == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# get_status payload
# ---------------------------------------------------------------------------

def test_get_status_includes_required_keys():
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    status = service.get_status()
    for key in (
        "running", "state", "phase", "message", "disk_detected", "error_radii",
        "jacobian_valid", "recovery_attempts", "last_command",
    ):
        assert key in status, f"Missing status key: {key}"


def test_get_status_running_false_when_not_started():
    fake = _FakeAdapter()
    service = SunCenteringService(adapter=fake.as_adapter())
    assert service.get_status()["running"] is False
    assert service.get_status()["state"] == SunCenteringService.STATE_STOPPED
