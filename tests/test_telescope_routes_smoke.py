#!/usr/bin/env python3
"""Smoke test for all registered /telescope routes.

This test is intentionally broad and shallow: it exercises every telescope
route with at least one valid-shape request using Flask's test client and
mocked telescope/alpaca clients. It is a regression net for route wiring,
import safety, and handler-level request parsing.
"""

from io import BytesIO
from unittest.mock import patch

import app as flask_app
from src.telescope_routes import MockSeestarClient


_ALLOWED_STATUS = {200, 201, 202, 204, 302, 400, 401, 403, 404, 405, 409, 422, 500, 503, 504}


def _client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


def _materialize_path(path: str) -> str:
    if "<path:name>" in path:
        return path.replace("<path:name>", "smoke-location")
    return path


def _request_for_route(client, method: str, path: str):
    """Build one safe, valid-shape request per route/method."""
    method = method.upper()
    path = _materialize_path(path)

    # GET routes with required query args
    if method == "GET" and path == "/telescope/files/frame":
        return client.get(f"{path}?path=captures/does_not_exist.mp4&frame=0")
    if method == "GET" and path == "/telescope/files/video-info":
        return client.get(f"{path}?path=captures/does_not_exist.mp4")
    if method == "GET" and path == "/telescope/composite":
        return client.get(path)
    if method == "GET" and path == "/telescope/preview/stream.mjpg":
        return client.get(path, buffered=False)

    # Endpoints expecting multipart uploads
    if method == "POST" and path == "/telescope/files/upload":
        payload = {
            "file": (BytesIO(b"not-a-jpeg"), "smoke.jpg"),
        }
        return client.post(path, data=payload, content_type="multipart/form-data")

    # JSON shape for routes that expect structured payloads
    if method in {"POST", "PATCH", "DELETE"}:
        body = {}

        if path == "/telescope/goto":
            body = {"mode": "altaz", "alt": 45, "az": 180}
        elif path == "/telescope/nudge":
            body = {"axis": "alt", "rate": 0.2}
        elif path == "/telescope/focus/step":
            body = {"steps": 10}
        elif path == "/telescope/settings/camera":
            body = {"gain": 80}
        elif path == "/telescope/camera/auto-exp":
            body = {"enabled": True}
        elif path == "/telescope/goto/locations":
            body = {"name": "smoke-location", "alt": 45, "az": 180}
        elif path == "/telescope/files/favorites":
            body = {"favorites": []}
        elif path == "/telescope/files/delete":
            body = {"path": "captures/does_not_exist.jpg"}
        elif path == "/telescope/files/rename":
            body = {
                "path": "captures/does_not_exist.jpg",
                "new_name": "renamed.jpg",
            }
        elif path == "/telescope/files/analyze":
            body = {"path": "captures/does_not_exist.mp4"}
        elif path == "/telescope/files/isolate-transit":
            body = {"path": "captures/does_not_exist.mp4"}
        elif path == "/telescope/files/composite-from-frames":
            body = {"path": "captures/does_not_exist.mp4", "frame_indices": [0]}
        elif path == "/telescope/files/trim":
            body = {"path": "captures/does_not_exist.mp4", "start_s": 0, "end_s": 1}
        elif path == "/telescope/files/export":
            body = {"path": "captures/does_not_exist.mp4"}
        elif path == "/telescope/debug/cmd":
            body = {"method": "scope_get_equ_coord"}
        elif path == "/telescope/alpaca/tracking":
            body = {"enabled": True}
        elif path == "/telescope/alpaca/settings":
            body = {"poll_interval_sec": 2}
        elif path == "/telescope/detect/settings":
            body = {"score_a_threshold": 1.0}
        elif path == "/telescope/harness/inject":
            body = {"size_px": 8, "speed_px_s": 50}
        elif path == "/telescope/harness/sweep":
            body = {"sizes_px": [6], "speeds_px_s": [50]}
        elif path == "/telescope/harness/validate":
            body = {"max_files": 1}

        return client.open(path, method=method, json=body)

    # Default for GET/HEAD-like routes
    return client.open(path, method=method)


def test_telescope_routes_smoke_all_registered_endpoints():
    """Every registered /telescope route responds with a documented status."""
    client = _client()

    mock_client = MockSeestarClient()
    # Keep smoke requests in the fast/failure-safe path (no live RTSP stream).
    mock_client.connect = lambda: False

    checked = []
    with (
        patch("src.telescope_routes.get_telescope_client", return_value=mock_client),
        patch("src.telescope_routes.get_alpaca_client", return_value=None),
        patch("tests.test_detection_harness.validate_real_videos", return_value=[]),
    ):
        for rule in sorted(flask_app.app.url_map.iter_rules(), key=lambda r: r.rule):
            if not rule.rule.startswith("/telescope"):
                continue

            for method in sorted(m for m in rule.methods if m in {"GET", "POST", "PATCH", "DELETE"}):
                # Skip HEAD/OPTIONS; Flask synthesizes these.
                resp = _request_for_route(client, method, rule.rule)
                checked.append((method, rule.rule, resp.status_code))
                assert resp.status_code in _ALLOWED_STATUS, (
                    f"Unexpected status {resp.status_code} for {method} {rule.rule}"
                )

    # Guard against accidental route loss in future refactors.
    assert len(checked) >= 60, f"Expected broad route coverage, got {len(checked)}"
