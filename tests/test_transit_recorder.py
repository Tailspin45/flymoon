"""
Tests for TransitRecorder scheduling and timing logic.
Uses a MockSeestarClient to avoid requiring real hardware.
"""
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.seestar_client import TransitRecorder


def _mock_client():
    """Return a mock SeestarClient with recording methods."""
    client = MagicMock()
    client.start_recording = MagicMock(return_value=True)
    client.stop_recording = MagicMock(return_value=True)
    return client


# ── schedule_transit_recording ─────────────────────────────────────────────


def test_schedule_returns_true():
    client = _mock_client()
    recorder = TransitRecorder(client, pre_buffer_seconds=1, post_buffer_seconds=1)
    result = recorder.schedule_transit_recording("FL001", eta_seconds=60)
    assert result is True
    recorder.cancel_all()


def test_schedule_duplicate_skips():
    """Scheduling the same flight_id twice while first is alive returns True without adding."""
    client = _mock_client()
    recorder = TransitRecorder(client, pre_buffer_seconds=1, post_buffer_seconds=1)
    recorder.schedule_transit_recording("FL001", eta_seconds=60)
    result = recorder.schedule_transit_recording("FL001", eta_seconds=60)
    assert result is True
    assert len(recorder._scheduled_recordings) == 1
    recorder.cancel_all()


def test_schedule_different_flights():
    """Two different flight IDs are both scheduled."""
    client = _mock_client()
    recorder = TransitRecorder(client, pre_buffer_seconds=1, post_buffer_seconds=1)
    recorder.schedule_transit_recording("FL001", eta_seconds=60)
    recorder.schedule_transit_recording("FL002", eta_seconds=60)
    assert len(recorder._scheduled_recordings) == 2
    recorder.cancel_all()


def test_cancel_all_clears_recordings():
    client = _mock_client()
    recorder = TransitRecorder(client, pre_buffer_seconds=1, post_buffer_seconds=1)
    recorder.schedule_transit_recording("FL001", eta_seconds=60)
    recorder.schedule_transit_recording("FL002", eta_seconds=60)
    recorder.cancel_all()
    assert len(recorder._scheduled_recordings) == 0


# ── recording actually fires ───────────────────────────────────────────────


def test_recording_starts_after_delay():
    """With very short buffers, recording should start within a second."""
    client = _mock_client()
    recorder = TransitRecorder(client, pre_buffer_seconds=0, post_buffer_seconds=0)

    # Patch OpenSky refinement so it doesn't try network calls
    with patch.object(recorder, "_opensky_refine"):
        recorder.schedule_transit_recording("FL001", eta_seconds=0.05,
                                            transit_duration_estimate=0.05)
        time.sleep(0.5)  # wait for timer to fire

    client.start_recording.assert_called_once()


def test_recording_stops_after_duration():
    """Recording stop is called after start + duration."""
    client = _mock_client()
    recorder = TransitRecorder(client, pre_buffer_seconds=0, post_buffer_seconds=0)

    with patch.object(recorder, "_opensky_refine"):
        recorder.schedule_transit_recording("FL001", eta_seconds=0.05,
                                            transit_duration_estimate=0.1)
        time.sleep(0.8)  # wait for both start and stop

    client.start_recording.assert_called_once()
    client.stop_recording.assert_called_once()


# ── timing calculation ─────────────────────────────────────────────────────


def test_start_delay_equals_eta_minus_pre_buffer():
    """The start timer delay = max(0, eta - pre_buffer)."""
    client = _mock_client()
    recorder = TransitRecorder(client, pre_buffer_seconds=5, post_buffer_seconds=5)

    captured = {}
    original_timer = threading.Timer

    def mock_timer(delay, fn, args=()):
        captured["delay"] = delay
        t = original_timer(9999, lambda: None)  # never fires
        t.cancel()
        t.start = lambda: None
        return t

    with patch("src.seestar_client.threading.Timer", side_effect=mock_timer):
        recorder.schedule_transit_recording("FL001", eta_seconds=20)

    assert captured.get("delay") == pytest.approx(15.0)  # 20 - 5
    recorder.cancel_all()


def test_start_delay_clamped_to_zero_when_eta_lt_pre_buffer():
    """If ETA < pre_buffer, start_delay = 0 (never negative)."""
    client = _mock_client()
    recorder = TransitRecorder(client, pre_buffer_seconds=10, post_buffer_seconds=5)

    captured = {}
    original_timer = threading.Timer

    def mock_timer(delay, fn, args=()):
        captured["delay"] = delay
        t = original_timer(9999, lambda: None)
        t.cancel()
        t.start = lambda: None
        return t

    with patch("src.seestar_client.threading.Timer", side_effect=mock_timer):
        recorder.schedule_transit_recording("FL001", eta_seconds=3)

    assert captured.get("delay") == 0
    recorder.cancel_all()


# ── cleanup_stale_timers ───────────────────────────────────────────────────


def test_cleanup_stale_timers_removes_finished():
    """Timers that have already fired are cleaned up."""
    client = _mock_client()
    recorder = TransitRecorder(client, pre_buffer_seconds=0, post_buffer_seconds=0)

    with patch.object(recorder, "_opensky_refine"):
        recorder.schedule_transit_recording("FL001", eta_seconds=0.02,
                                            transit_duration_estimate=0.02)
        time.sleep(0.2)  # let timer fire and finish

    recorder.cleanup_stale_timers()
    assert "FL001" not in recorder._scheduled_recordings
