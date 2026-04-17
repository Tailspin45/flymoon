"""Unit tests for RTSP probe cooldown and recovery behavior."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import telescope_routes
from src.telescope import debug_log


def _fake_client(mode: str = "sun"):
    client = SimpleNamespace(host="192.168.4.36", _viewing_mode=mode)
    client.start_solar_mode = MagicMock(return_value=True)
    client.start_lunar_mode = MagicMock(return_value=True)
    return client


@pytest.fixture(autouse=True)
def _reset_rtsp_probe_state():
    debug_log._rtsp_recover_last_attempt_by_host_mode.clear()
    debug_log._rtsp_probe_fail_last_warn_by_host_mode.clear()
    yield
    debug_log._rtsp_recover_last_attempt_by_host_mode.clear()
    debug_log._rtsp_probe_fail_last_warn_by_host_mode.clear()


def test_ensure_rtsp_ready_does_not_reassert_in_passive_mode(monkeypatch):
    client = _fake_client(mode="sun")
    monkeypatch.setattr(
        telescope_routes,
        "_resolve_rtsp_stream_url",
        lambda *_args, **_kwargs: None,
    )

    with patch.object(telescope_routes.logger, "warning") as mock_warn:
        out = telescope_routes._ensure_rtsp_ready(
            client,
            allow_mode_reassert=False,
            warn_cooldown_seconds=0.0,
        )

    assert out is None
    client.start_solar_mode.assert_not_called()
    assert mock_warn.call_count == 1


def test_ensure_rtsp_ready_reasserts_in_active_mode(monkeypatch):
    client = _fake_client(mode="sun")
    monkeypatch.setattr(
        telescope_routes,
        "_resolve_rtsp_stream_url",
        lambda *_args, **_kwargs: None,
    )

    with patch.object(telescope_routes.time, "sleep", lambda *_args, **_kwargs: None):
        telescope_routes._ensure_rtsp_ready(
            client,
            allow_mode_reassert=True,
            warn_cooldown_seconds=0.0,
        )

    client.start_solar_mode.assert_called_once()


def test_ensure_rtsp_ready_throttles_repeat_probe_warning(monkeypatch):
    client = _fake_client(mode="sun")
    monkeypatch.setattr(
        telescope_routes,
        "_resolve_rtsp_stream_url",
        lambda *_args, **_kwargs: None,
    )

    with (
        patch.object(telescope_routes.time, "monotonic", side_effect=[100.0, 101.0]),
        patch.object(telescope_routes.logger, "warning") as mock_warn,
        patch.object(telescope_routes.logger, "debug") as mock_debug,
    ):
        telescope_routes._ensure_rtsp_ready(
            client,
            allow_mode_reassert=False,
            warn_cooldown_seconds=20.0,
        )
        telescope_routes._ensure_rtsp_ready(
            client,
            allow_mode_reassert=False,
            warn_cooldown_seconds=20.0,
        )

    assert mock_warn.call_count == 1
    assert mock_debug.call_count >= 1
