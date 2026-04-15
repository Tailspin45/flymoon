"""Tier 3 regression tests for OpenSky snapshot freshness and caller behavior."""

import time

from src import opensky
from src.transit_detector import TransitDetector


def test_get_latest_snapshot_empty_cache_returns_empty_dict():
    assert opensky.get_latest_snapshot() == {}


def test_get_latest_snapshot_returns_most_recent_bbox_data():
    now = time.time()
    opensky._cache["bbox-old"] = {
        "ts": now - 20,
        "data": {"OLD1": {"lat": 1.0, "lon": 2.0}},
    }
    opensky._cache["bbox-new"] = {
        "ts": now - 1,
        "data": {"NEW1": {"lat": 3.0, "lon": 4.0}},
    }

    snap = opensky.get_latest_snapshot()
    assert set(snap.keys()) == {"NEW1"}


def test_get_latest_snapshot_respects_max_age_seconds():
    now = time.time()
    opensky._cache["bbox-stale"] = {
        "ts": now - 91,
        "data": {"STALE1": {"lat": 1.0, "lon": 2.0}},
    }

    assert opensky.get_latest_snapshot(max_age_s=90.0) == {}


def test_get_latest_snapshot_returns_copy_not_live_cache_reference():
    now = time.time()
    opensky._cache["bbox"] = {
        "ts": now,
        "data": {"AAL1": {"lat": 1.0, "lon": 2.0, "nested": {"x": 1}}},
    }

    snap = opensky.get_latest_snapshot()
    snap["AAL1"]["nested"]["x"] = 99

    assert opensky._cache["bbox"]["data"]["AAL1"]["nested"]["x"] == 1


def test_enrichment_uses_fresh_snapshot_policy_and_altitude_m_in_fallback(monkeypatch):
    calls = []

    def fake_get_latest_snapshot(max_age_s=None):
        calls.append(("snapshot", max_age_s))
        return {}

    def fake_fetch_opensky_positions(lat_ll, lon_ll, lat_ur, lon_ur):
        calls.append(("fetch", (lat_ll, lon_ll, lat_ur, lon_ur)))
        return {
            "UAL123": {
                "lat": 37.62,
                "lon": -122.38,
                "altitude_m": 11234.0,
                "on_ground": False,
                "origin_country": "United States",
            }
        }

    monkeypatch.setattr(opensky, "get_latest_snapshot", fake_get_latest_snapshot)
    monkeypatch.setattr(opensky, "fetch_opensky_positions", fake_fetch_opensky_positions)

    flights = TransitDetector._fetch_flights_for_enrichment(37.0, -123.0, 38.0, -122.0)

    assert calls[0] == ("snapshot", 90.0)
    assert any(kind == "fetch" for kind, _ in calls if isinstance((kind, _), tuple))
    assert len(flights) == 1
    assert flights[0]["name"] == "UAL123"
    assert flights[0]["elevation"] == 11234.0
    assert flights[0]["origin_country"] == "United States"
