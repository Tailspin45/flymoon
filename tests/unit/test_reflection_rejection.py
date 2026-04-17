"""Regression tests for reflection-resistant Sun disk handling."""

import cv2
import numpy as np

from src.sun_centering import SunCenteringAdapter, SunCenteringService
from src.transit_detector import _detect_disk


def test_detect_disk_prefers_larger_true_disk_over_small_reflection():
    gray = np.zeros((320, 180), dtype=np.uint8)

    # Simulated true Sun disk (larger, dimmer but still detectable)
    cv2.circle(gray, (95, 160), 42, 215, -1)
    # Simulated internal reflection (smaller, brighter)
    cv2.circle(gray, (42, 78), 16, 255, -1)

    found = _detect_disk(gray)
    assert found is not None
    cx, cy, rr = found

    # We expect the larger real disk candidate, not the small bright ghost.
    assert rr >= 30
    assert abs(cx - 95) <= 12
    assert abs(cy - 160) <= 12


def _make_adapter():
    return SunCenteringAdapter(
        is_scope_connected=lambda: True,
        is_alpaca_connected=lambda: True,
        get_viewing_mode=lambda: "sun",
        get_sun_altaz=lambda: (45.0, 180.0),
        goto_altaz=lambda alt, az: {"success": True},
        is_slewing=lambda: False,
        stop_axes=lambda: {"success": True},
        get_detector_status=lambda: {},
        get_position=lambda: {"alt": 45.0, "az": 180.0, "ra": 0.0, "dec": 0.0},
        set_tracking=lambda enabled: {"success": True},
    )


def test_sun_center_rejects_implausibly_small_disk_radius():
    svc = SunCenteringService(adapter=_make_adapter())

    # 10 px is implausibly small on 180x320 for the true solar disk and often
    # corresponds to internal reflections / bright artifacts.
    det = {
        "disk_detected": True,
        "analysis_resolution": "180x320@30fps",
        "disk_info": {"cx": 90.0, "cy": 160.0, "radius": 10.0},
    }

    svc._refresh_disk_snapshot(det)

    assert svc.disk_detected is False
    assert svc.disk_info is None
    assert svc.error_radii is None


def test_sun_center_accepts_plausible_disk_radius():
    svc = SunCenteringService(adapter=_make_adapter())
    det = {
        "disk_detected": True,
        "analysis_resolution": "180x320@30fps",
        "disk_info": {"cx": 120.0, "cy": 140.0, "radius": 34.0},
    }

    svc._refresh_disk_snapshot(det)

    assert svc.disk_detected is True
    assert svc.disk_info is not None
    assert svc.error_radii is not None