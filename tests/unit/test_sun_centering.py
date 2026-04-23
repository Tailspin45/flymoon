"""Unit tests for SunCenteringService state machine.

Tests use a mock adapter so no telescope hardware is required.
"""

import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.sun_centering import (
    SunCenteringAdapter,
    SunCenteringService,
    SunCenteringSettings,
)


# ---------------------------------------------------------------------------
# Mock adapter factory
# ---------------------------------------------------------------------------

def _make_adapter(
    *,
    connected: bool = True,
    viewing_mode: str = "sun",
    sun_altaz: tuple = (45.0, 180.0),
    slewing: bool = False,
    goto_error: bool = False,
    disk_detected: bool = True,
    disk_cx: float = 90.0,
    disk_cy: float = 160.0,
    disk_radius: float = 56.0,
    disk_state_age_s: float = 0.5,
    mount_alt: float = 45.0,
    mount_az: float = 180.0,
) -> SunCenteringAdapter:
    goto_resp = {"ErrorNumber": 1, "ErrorMessage": "test error"} if goto_error else {"ErrorNumber": 0}
    disk_info = (
        {"cx": disk_cx, "cy": disk_cy, "radius": disk_radius}
        if disk_detected
        else None
    )
    detector_status = {
        "disk_detected": disk_detected,
        "disk_info": disk_info,
        "disk_state_age_s": disk_state_age_s,
        "analysis_resolution": "180x320@30fps",
        "center_flux": {"core_mean": 50.0, "ring_mean": 20.0},
    }

    return SunCenteringAdapter(
        is_scope_connected=lambda: connected,
        is_alpaca_connected=lambda: connected,
        get_viewing_mode=lambda: viewing_mode,
        get_sun_altaz=lambda: sun_altaz,
        goto_altaz=MagicMock(return_value=goto_resp),
        is_slewing=MagicMock(return_value=slewing),
        stop_axes=MagicMock(return_value={"ErrorNumber": 0}),
        get_detector_status=lambda: detector_status,
        get_position=lambda: {"alt": mount_alt, "az": mount_az},
        set_tracking=MagicMock(return_value={"ErrorNumber": 0}),
    )


def _fast_settings(**kwargs) -> SunCenteringSettings:
    """Settings with very short timeouts suitable for unit tests."""
    defaults = dict(
        tick_hz=100.0,
        acquire_settle_s=0.01,
        acquire_search_settle_s=0.01,
        probe_settle_s=0.01,
        center_settle_s=0.01,
        recover_settle_s=0.01,
        precheck_busy_timeout_s=0.2,
        acquire_slew_timeout_s=0.1,
        probe_slew_timeout_s=0.1,
        center_slew_timeout_s=0.1,
        track_refresh_s=1.0,
        lock_lost_grace_s=0.05,
    )
    defaults.update(kwargs)
    return SunCenteringSettings(**defaults)


# ---------------------------------------------------------------------------
# recenter() resets the precheck deadline (Bug 1)
# ---------------------------------------------------------------------------

class TestRecenterResetsDeadline:
    def test_precheck_deadline_refreshed(self):
        """recenter() must reset _precheck_busy_deadline_mono to now+timeout."""
        adapter = _make_adapter()
        svc = SunCenteringService(adapter=adapter, settings=_fast_settings())
        svc.start()
        try:
            # Burn down the original deadline.
            original_deadline = svc._precheck_busy_deadline_mono
            time.sleep(0.05)
            ok = svc.recenter()
            assert ok
            assert svc._precheck_busy_deadline_mono > original_deadline
            assert svc._precheck_busy_deadline_mono > time.monotonic()
        finally:
            svc.stop()

    def test_recenter_calls_stop_axes(self):
        """recenter() must call stop_axes() before re-entering ACQUIRE."""
        adapter = _make_adapter()
        svc = SunCenteringService(adapter=adapter, settings=_fast_settings())
        svc.start()
        try:
            svc.recenter()
            time.sleep(0.05)
            adapter.stop_axes.assert_called()
        finally:
            svc.stop()

    def test_recenter_on_stopped_service_returns_false(self):
        adapter = _make_adapter()
        svc = SunCenteringService(adapter=adapter, settings=_fast_settings())
        assert not svc.recenter()


# ---------------------------------------------------------------------------
# _refresh_disk_snapshot — frame dimensions and bounds checks (Bugs 3 & 2)
# ---------------------------------------------------------------------------

class TestRefreshDiskSnapshot:
    def _make_service(self, **adapter_kwargs):
        adapter = _make_adapter(**adapter_kwargs)
        svc = SunCenteringService(adapter=adapter, settings=_fast_settings())
        return svc

    def _run_snapshot(self, svc, det: dict) -> None:
        svc._refresh_disk_snapshot(det)

    def test_portrait_fallback_dimensions(self):
        """Fallback must be 180×320 (portrait), not 320×180."""
        svc = self._make_service()
        det = {
            "disk_detected": True,
            "disk_info": {"cx": 89.5, "cy": 159.5, "radius": 40.0},
            # no analysis_resolution → triggers fallback
        }
        svc._refresh_disk_snapshot(det)
        assert svc.disk_detected
        # With correct 180×320 fallback, center is (89.5, 159.5) → eu≈0, ev≈0.
        assert abs(svc.disk_eu_px) < 1.0
        assert abs(svc.disk_ev_px) < 1.0

    def test_out_of_bounds_disk_rejected(self):
        """Disk whose circle extends outside the frame must be rejected."""
        svc = self._make_service()
        # radius=92 in a 180px-wide frame: cx+r = 90+92 = 182 > 180 → reject
        det = {
            "disk_detected": True,
            "disk_info": {"cx": 90.0, "cy": 160.0, "radius": 92.0},
            "analysis_resolution": "180x320@30fps",
        }
        svc._refresh_disk_snapshot(det)
        assert not svc.disk_detected

    def test_in_bounds_disk_accepted(self):
        """Disk fully contained in frame must be accepted."""
        svc = self._make_service()
        det = {
            "disk_detected": True,
            "disk_info": {"cx": 90.0, "cy": 160.0, "radius": 50.0},
            "analysis_resolution": "180x320@30fps",
            "disk_state_age_s": 0.1,
        }
        svc._refresh_disk_snapshot(det)
        assert svc.disk_detected

    def test_stale_disk_state_rejected(self):
        """Disk state older than 5 s must be treated as no-disk."""
        svc = self._make_service()
        det = {
            "disk_detected": True,
            "disk_info": {"cx": 90.0, "cy": 160.0, "radius": 50.0},
            "analysis_resolution": "180x320@30fps",
            "disk_state_age_s": 6.0,  # too old
        }
        svc._refresh_disk_snapshot(det)
        assert not svc.disk_detected

    def test_fresh_disk_state_not_rejected(self):
        """Disk state within 5 s must not be rejected on freshness alone."""
        svc = self._make_service()
        det = {
            "disk_detected": True,
            "disk_info": {"cx": 90.0, "cy": 160.0, "radius": 50.0},
            "analysis_resolution": "180x320@30fps",
            "disk_state_age_s": 4.9,
        }
        svc._refresh_disk_snapshot(det)
        assert svc.disk_detected

    def test_implausible_radius_rejected(self):
        """Radius outside plausibility bounds [7%, 62%] of min_dim must be rejected."""
        svc = self._make_service()
        det = {
            "disk_detected": True,
            "disk_info": {"cx": 90.0, "cy": 160.0, "radius": 3.0},  # too small
            "analysis_resolution": "180x320@30fps",
            "disk_state_age_s": 0.1,
        }
        svc._refresh_disk_snapshot(det)
        assert not svc.disk_detected

    def test_error_vector_correct_with_portrait_frame(self):
        """Error vector eu/ev must be computed using the parsed portrait dimensions."""
        svc = self._make_service()
        det = {
            "disk_detected": True,
            "disk_info": {"cx": 100.0, "cy": 150.0, "radius": 50.0},
            "analysis_resolution": "180x320@30fps",
            "disk_state_age_s": 0.1,
        }
        svc._refresh_disk_snapshot(det)
        assert svc.disk_detected
        # img_cx = (180-1)/2 = 89.5, img_cy = (320-1)/2 = 159.5
        assert abs(svc.disk_eu_px - (100.0 - 89.5)) < 0.01
        assert abs(svc.disk_ev_px - (150.0 - 159.5)) < 0.01


# ---------------------------------------------------------------------------
# probe_deg default (Bug 4a)
# ---------------------------------------------------------------------------

class TestProbeDeg:
    def test_default_probe_deg_is_0_30(self):
        """Default probe_deg must be 0.30° for adequate SNR."""
        s = SunCenteringSettings()
        assert s.probe_deg == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# _compute_jacobian — column-magnitude validation (Bug 4b)
# ---------------------------------------------------------------------------

class TestJacobianValidation:
    def _make_svc_with_columns(self, col_alt, col_az, plate_scale=0.004):
        adapter = _make_adapter()
        svc = SunCenteringService(adapter=adapter, settings=_fast_settings())
        svc._cal_j_col_alt = col_alt
        svc._cal_j_col_az = col_az
        svc.plate_scale_deg_per_px = plate_scale
        return svc

    def test_plausible_jacobian_accepted(self):
        """A Jacobian with columns ≈ expected px/° must be accepted."""
        # At 0.004°/px, expected ≈ 250 px/°. Use 220 (within 4× of 250).
        svc = self._make_svc_with_columns([220.0, 0.0], [0.0, 220.0])
        svc._compute_jacobian()
        assert svc._jacobian_valid

    def test_implausible_alt_column_rejected(self):
        """Alt column 5× the expected px/° must force re-calibration."""
        # Expected 250 px/°; J[1][0]=1240 (from real failure) → 1240/250 ≈ 5× → reject
        svc = self._make_svc_with_columns([106.0, 1240.0], [-66.0, 893.0])
        svc._compute_jacobian()
        assert not svc._jacobian_valid
        assert svc.state == SunCenteringService.STATE_ACQUIRE

    def test_implausible_az_column_rejected(self):
        """Az column at 0.1× expected px/° (noise floor) must be rejected."""
        # Expected 250 px/°; column magnitude ≈ 25 → 0.1× → reject
        svc = self._make_svc_with_columns([220.0, 0.0], [0.0, 25.0])
        svc._compute_jacobian()
        assert not svc._jacobian_valid

    def test_no_plate_scale_uses_safe_default(self):
        """When plate_scale is unknown, validation uses a safe default (not crash)."""
        svc = self._make_svc_with_columns([220.0, 0.0], [0.0, 220.0])
        svc.plate_scale_deg_per_px = None
        svc._compute_jacobian()
        # Should not raise; result may be valid or invalid depending on default estimate


# ---------------------------------------------------------------------------
# _handle_center — step-size clamping (Bug 5)
# ---------------------------------------------------------------------------

class TestStepSizeClamping:
    def test_correction_clamped_to_1_degree(self):
        """No single correction step may exceed ±1° per axis."""
        adapter = _make_adapter(disk_cx=5.0, disk_cy=5.0, disk_radius=50.0)
        svc = SunCenteringService(adapter=adapter, settings=_fast_settings())
        # Install a wildly wrong Jacobian that would produce huge corrections.
        # J maps (dalt, daz) -> pixels; inverse maps pixels -> (dalt, daz).
        # Bad J_inv with large values → huge dalt/daz.
        svc._j_inv = [[100.0, 0.0], [0.0, 100.0]]  # 100 deg/px!
        svc._jacobian_valid = True
        svc.disk_detected = True
        svc.disk_eu_px = 80.0   # 80px error × 100 deg/px = 8° — must be clamped to 1°
        svc.disk_ev_px = -80.0
        svc.error_radii = 1.6
        svc.plate_scale_deg_per_px = 0.004

        svc._center_iter_count = 0
        svc._phase = "check"
        svc._center_no_disk_ticks = 0
        svc.state = SunCenteringService.STATE_CENTER

        svc._handle_center()

        # The GoTo was issued; check the requested alt/az are within ±1° of start.
        call_args = adapter.goto_altaz.call_args
        assert call_args is not None
        tgt_alt, tgt_az = call_args[0]
        ref_alt, ref_az = 45.0, 180.0
        assert abs(tgt_alt - ref_alt) <= 1.0 + 0.11  # 0.11 for min_sun_alt clamp
        assert abs(tgt_az - ref_az) <= 1.0


# ---------------------------------------------------------------------------
# _goto_clamped — ALPACA error checking (Bug 8)
# ---------------------------------------------------------------------------

class TestGotoError:
    def test_goto_alpaca_error_triggers_fail_safe(self):
        """ErrorNumber != 0 in GoTo response must enter FAIL_SAFE."""
        adapter = _make_adapter(goto_error=True)
        svc = SunCenteringService(adapter=adapter, settings=_fast_settings())
        svc.state = SunCenteringService.STATE_ACQUIRE
        svc._goto_clamped(45.0, 180.0)
        assert svc.state == SunCenteringService.STATE_FAIL_SAFE

    def test_goto_success_does_not_trigger_fail_safe(self):
        """ErrorNumber == 0 must not trigger FAIL_SAFE."""
        adapter = _make_adapter(goto_error=False)
        svc = SunCenteringService(adapter=adapter, settings=_fast_settings())
        svc.state = SunCenteringService.STATE_ACQUIRE
        svc._goto_clamped(45.0, 180.0)
        assert svc.state != SunCenteringService.STATE_FAIL_SAFE
