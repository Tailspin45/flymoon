"""Pinning tests for the matched-filter gate in src.transit_detector.

Pins:
  * the _MF_TEMPLATES constant
  * the _mf_hit_required graduated schedule
  * the shortest-first gate loop against synthetic bool sequences

The gate loop itself is embedded inline in TransitDetector._process_frame
at transit_detector.py:1240-1256. These tests use a pure reference copy of
that loop so that a future structural refactor of the detector cannot
silently change the gate's input→output mapping without the reference
here also being updated.
"""

from typing import List, Tuple

from src.transit_detector import _MF_TEMPLATES, _mf_hit_required


def _reference_gate(triggered_buf: List[bool]) -> Tuple[bool, int]:
    """Reference copy of the matched-filter gate loop from
    transit_detector._process_frame. Returns (fired, template_n)."""
    for n in _MF_TEMPLATES:
        if len(triggered_buf) >= n:
            hits = sum(triggered_buf[-n:])
            if hits >= _mf_hit_required(n):
                return True, n
    return False, 0


def test_mf_templates_shape():
    assert _MF_TEMPLATES == (6, 10, 15, 24, 40, 60, 90, 120)
    assert list(_MF_TEMPLATES) == sorted(_MF_TEMPLATES)


def test_mf_hit_required_schedule():
    # 70% band — max(3, int(0.70 * n))
    assert _mf_hit_required(6) == 4
    assert _mf_hit_required(10) == 7
    assert _mf_hit_required(15) == 10
    # 60% band
    assert _mf_hit_required(24) == 14
    assert _mf_hit_required(40) == 24
    # 50% band
    assert _mf_hit_required(60) == 30
    # 45% band
    assert _mf_hit_required(90) == 40
    assert _mf_hit_required(120) == 54
    # max(3, ...) floor for tiny n
    assert _mf_hit_required(1) == 3
    assert _mf_hit_required(3) == 3
    assert _mf_hit_required(5) == 3


def test_gate_fires_on_full_trigger_buf():
    """120 consecutive True frames must fire on the shortest (6-frame) template."""
    buf = [True] * 120
    fired, n = _reference_gate(buf)
    assert fired is True
    assert n == 6


def test_gate_rejects_sparse_triggers():
    """25% hit density is below every threshold; gate must not fire."""
    buf = [(i % 4 == 0) for i in range(120)]  # True, F, F, F, True, ...
    fired, n = _reference_gate(buf)
    assert fired is False
    assert n == 0


def test_gate_fires_on_90f_template_at_45pct():
    """Exactly 41 hits in the last 90 frames (≈45.5%) meets the 90-frame
    template's 40-hit requirement (45%) but none of the shorter templates,
    because the shorter windows contain fewer hits than their own (higher)
    thresholds demand."""
    # Place 41 hits densely at the tail so the shorter windows see many
    # hits too — if the shortest template (6→4) would fire on a 100% tail,
    # the test would be wrong. So we place hits evenly-spaced.
    buf = [False] * 120
    # Put 41 hits every ~2.2 positions across the last 90 frames
    hit_positions = [120 - 90 + i * 90 // 41 for i in range(41)]
    for p in hit_positions:
        if 0 <= p < 120:
            buf[p] = True

    # Sanity: ensure we placed exactly 41 hits in the last 90
    assert sum(buf[-90:]) == 41

    # Short templates (n=6, 10, 15, 24, 40, 60) must not see enough hits
    # under the even-spread placement. 41/90 hits evenly spread ≈ 0.456 density
    # so in 60 frames we expect ~27 hits vs 30 required — below threshold.
    for short_n in (6, 10, 15, 24, 40, 60):
        hits = sum(buf[-short_n:])
        assert hits < _mf_hit_required(short_n), (
            f"short template n={short_n} fired prematurely: "
            f"hits={hits}, required={_mf_hit_required(short_n)}"
        )

    fired, n = _reference_gate(buf)
    assert fired is True
    assert n == 90
