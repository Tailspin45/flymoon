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


def test_snapshot_matches_requested_bbox():
    """Exact-bbox lookup must return only the entry for the requested bbox,
    not the most-recently-fetched one (audit finding #6 regression)."""
    now = time.time()
    # Two disjoint bboxes cached at the same freshness.
    opensky._cache["37.000,-123.000,38.000,-122.000"] = {
        "ts": now - 5,
        "data": {"SFO1": {"lat": 37.6, "lon": -122.4}},
    }
    opensky._cache["51.000,-1.000,52.000,0.000"] = {
        "ts": now - 2,  # newer timestamp
        "data": {"LHR1": {"lat": 51.5, "lon": -0.1}},
    }

    # Requesting the SF bbox must NOT return the newer LHR entry.
    snap = opensky.get_latest_snapshot(lat_ll=37.0, lon_ll=-123.0, lat_ur=38.0, lon_ur=-122.0)
    assert set(snap.keys()) == {"SFO1"}, (
        "get_latest_snapshot returned wrong bbox data (audit finding #6)"
    )

    # Requesting the LHR bbox must return LHR data.
    snap2 = opensky.get_latest_snapshot(lat_ll=51.0, lon_ll=-1.0, lat_ur=52.0, lon_ur=0.0)
    assert set(snap2.keys()) == {"LHR1"}


def test_snapshot_returns_empty_dict_on_bbox_miss(caplog):
    """Requesting a bbox that has no cached entry returns {} and logs a WARNING."""
    import logging

    now = time.time()
    opensky._cache["10.000,10.000,11.000,11.000"] = {
        "ts": now,
        "data": {"XX1": {"lat": 10.5, "lon": 10.5}},
    }

    caplog.set_level(logging.WARNING)
    snap = opensky.get_latest_snapshot(lat_ll=50.0, lon_ll=50.0, lat_ur=51.0, lon_ur=51.0)

    assert snap == {}, "Expected empty dict on bbox miss"
    assert any("get_latest_snapshot" in r.getMessage() for r in caplog.records), (
        "Expected a WARNING log on bbox miss"
    )


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
