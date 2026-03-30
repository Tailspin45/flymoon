"""
Tests for the auto-start detector (Phase 4, missed-transit fix).

Covers:
  1. _maybe_auto_start_detector() starts the detector when not running.
  2. _maybe_auto_start_detector() is a no-op when detector already running.
  3. _maybe_auto_start_detector() is a no-op when scope is not connected.
  4. get_armed_status() returns warning when solar mode + detector off.
  5. get_armed_status() returns armed=True when solar mode + detector running.
  6. transit_check() returns 200 and triggers recording for a HIGH flight.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as flask_app  # noqa: E402  — must come after sys.path insert


def _app_ctx():
    """Return a usable Flask app context for tests that call jsonify()."""
    flask_app.app.config["TESTING"] = True
    return flask_app.app.app_context()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client(connected=True, viewing_mode="sun"):
    client = MagicMock()
    client.is_connected.return_value = connected
    client._viewing_mode = viewing_mode
    client.host = "192.168.1.1"
    return client


def _make_mock_detector(running=False):
    det = MagicMock()
    det.is_running = running
    return det


# ---------------------------------------------------------------------------
# 1–3: _maybe_auto_start_detector
# ---------------------------------------------------------------------------

class TestMaybeAutoStartDetector:
    def _run(self, client, detector):
        from src import telescope_routes
        with patch.object(telescope_routes, "get_telescope_client", return_value=client), \
             patch("src.transit_detector.get_detector", return_value=detector), \
             patch("src.transit_detector.start_detector") as mock_start:
            telescope_routes._maybe_auto_start_detector("sun")
            return mock_start

    def test_starts_when_not_running(self):
        """Detector should be started when not running and scope is connected."""
        client = _make_mock_client(connected=True, viewing_mode="sun")
        detector = _make_mock_detector(running=False)
        mock_start = self._run(client, detector)
        mock_start.assert_called_once()
        args, kwargs = mock_start.call_args
        assert "rtsp://" in args[0]
        assert kwargs.get("record_on_detect") is True

    def test_noop_when_already_running(self):
        """Should not start a second detector instance."""
        client = _make_mock_client(connected=True, viewing_mode="sun")
        detector = _make_mock_detector(running=True)
        mock_start = self._run(client, detector)
        mock_start.assert_not_called()

    def test_noop_when_scope_disconnected(self):
        """Should not attempt to start when scope is offline."""
        client = _make_mock_client(connected=False)
        detector = _make_mock_detector(running=False)
        mock_start = self._run(client, detector)
        mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# 4–5: get_armed_status endpoint
# ---------------------------------------------------------------------------

class TestGetArmedStatus:
    def _call(self, viewing_mode, detector_running):
        from src import telescope_routes
        import json

        client = _make_mock_client(connected=True, viewing_mode=viewing_mode)
        detector = _make_mock_detector(running=detector_running)

        with _app_ctx(), \
             patch.object(telescope_routes, "get_telescope_client", return_value=client), \
             patch("src.transit_detector.get_detector", return_value=detector):
            response, status = telescope_routes.get_armed_status()
            data = json.loads(response.get_data(as_text=True))
            return data, status

    def test_warning_when_solar_mode_detector_off(self):
        data, status = self._call("sun", detector_running=False)
        assert status == 200
        assert data["armed"] is False
        assert data["warning"] is not None
        assert "detection is OFF" in data["warning"]
        assert data["scope_mode"] == "sun"

    def test_armed_when_solar_mode_detector_running(self):
        data, status = self._call("sun", detector_running=True)
        assert status == 200
        assert data["armed"] is True
        assert data["warning"] is None

    def test_no_warning_scenery_mode(self):
        """Scenery mode is not a transit target — no warning expected."""
        data, status = self._call("scenery", detector_running=False)
        assert status == 200
        assert data["armed"] is False
        assert data["warning"] is None

    def test_lunar_mode_warning(self):
        data, status = self._call("moon", detector_running=False)
        assert status == 200
        assert data["warning"] is not None
        assert "lunar" in data["warning"]


# ---------------------------------------------------------------------------
# 6: transit_check schedules recording for HIGH flights
# ---------------------------------------------------------------------------

class TestTransitCheck:
    def test_schedules_recording_for_high_flight(self):
        from src import telescope_routes
        from src.constants import PossibilityLevel
        import json

        client = _make_mock_client(connected=True, viewing_mode="sun")
        recorder = MagicMock()

        fake_high_flight = {
            "ident": "TST123",
            "id": "TST123",
            "possibility_level": PossibilityLevel.HIGH.value,
            "time": 2.5,          # 2.5 minutes ETA
            "angular_separation": 1.5,
            "alt_diff": 1.2,
            "az_diff": 0.8,
        }
        transit_data = {"flights": [fake_high_flight]}

        with _app_ctx(), \
             patch.object(telescope_routes, "get_telescope_client", return_value=client), \
             patch("src.transit.get_transits", return_value=transit_data), \
             patch.object(telescope_routes, "get_observer_coordinates",
                          return_value=(33.11, -117.31, 0.0)), \
             patch("app.get_transit_recorder", return_value=recorder, create=True):
            response, status = telescope_routes.transit_check()
            data = json.loads(response.get_data(as_text=True))

        assert status == 200
        assert data["checked"] is True
        assert len(data["high_transits"]) == 1
        assert data["high_transits"][0]["id"] == "TST123"
        recorder.schedule_transit_recording.assert_called_once()
        call_kwargs = recorder.schedule_transit_recording.call_args[1] \
                      if recorder.schedule_transit_recording.call_args[1] \
                      else dict(zip(
                            ["flight_id", "eta_seconds", "transit_duration_estimate", "sep_deg"],
                            recorder.schedule_transit_recording.call_args[0]))
        assert call_kwargs.get("flight_id") == "TST123" or \
               recorder.schedule_transit_recording.call_args[0][0] == "TST123"

    def test_skips_when_scenery_mode(self):
        from src import telescope_routes
        import json

        client = _make_mock_client(connected=True, viewing_mode="scenery")
        with _app_ctx(), \
             patch.object(telescope_routes, "get_telescope_client", return_value=client):
            response, status = telescope_routes.transit_check()
            data = json.loads(response.get_data(as_text=True))
        assert status == 200
        assert data["checked"] is False
