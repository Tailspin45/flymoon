#!/usr/bin/env python3
"""
Flask route smoke tests — exercises core endpoints using Flask's built-in test
client (no real server or network required).

get_transits() is mocked to return a fixed result, so these tests exercise
the request parsing, response formatting, and error handling in app.py without
hitting FlightAware or OpenSky.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as flask_app

# Query string used for all /flights smoke tests
OBSERVER_QS = "latitude=33.11&longitude=-117.31&elevation=100"

# Minimal return value that satisfies the app's response builder
MOCK_TRANSITS = {
    "sun": {
        "transits": [],
        "target_coordinates": {"altitude": 45.0, "azimuthal": 180.0},
    }
}


def get_client():
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


# ── tests ─────────────────────────────────────────────────────────────────────

def test_index_returns_200():
    """GET / returns the main HTML page."""
    client = get_client()
    resp = client.get("/")
    assert resp.status_code == 200, f"/ returned {resp.status_code}"
    assert b"html" in resp.data.lower(), "/ should return HTML"
    print("✓ GET / → 200 HTML")


def test_flights_missing_lat_lon_returns_400():
    """GET /flights without lat/lon returns 400."""
    client = get_client()
    resp = client.get("/flights")
    assert resp.status_code == 400, \
        f"Missing lat/lon should return 400, got {resp.status_code}"
    body = resp.get_json()
    assert body is not None
    assert "error" in body
    print(f"✓ GET /flights (no lat/lon) → 400: {body['error'][:60]}")


def test_flights_with_observer_returns_json():
    """GET /flights with observer position returns valid JSON with expected keys."""
    client = get_client()
    with patch("app.get_transits", return_value={"flights": [], "target_coordinates": {"altitude": 45.0, "azimuthal": 180.0}}):
        resp = client.get(f"/flights?{OBSERVER_QS}")
    assert resp.status_code == 200, f"/flights returned {resp.status_code}: {resp.data[:200]}"
    body = resp.get_json()
    assert body is not None, "Response is not valid JSON"
    assert "flights" in body, f"Response missing 'flights' key: {list(body.keys())}"
    assert "targetCoordinates" in body, \
        f"Response missing 'targetCoordinates': {list(body.keys())}"
    print(f"✓ GET /flights → 200, keys: {list(body.keys())}")


def test_flights_target_coordinates_schema():
    """targetCoordinates contains per-target dicts with altitude and azimuthal fields."""
    client = get_client()
    with patch("app.get_transits", return_value={"flights": [], "target_coordinates": {"altitude": 52.3, "azimuthal": 220.1}}):
        resp = client.get(f"/flights?{OBSERVER_QS}")
    body = resp.get_json()
    coords = body["targetCoordinates"]
    # targetCoordinates is keyed by target name (sun/moon)
    assert isinstance(coords, dict), f"targetCoordinates should be dict, got {type(coords)}"
    for target_name, target_coords in coords.items():
        assert "altitude"  in target_coords, f"[{target_name}] missing 'altitude': {target_coords}"
        assert "azimuthal" in target_coords, f"[{target_name}] missing 'azimuthal': {target_coords}"
        print(f"✓ targetCoordinates[{target_name}]: alt={target_coords['altitude']:.1f}°, az={target_coords['azimuthal']:.1f}°")


def test_flights_response_flight_schema():
    """Each flight in the response contains the required fields."""
    client = get_client()
    mock_transit = {
        "id": "UAL123-test",
        "name": "UAL123",
        "fa_flight_id": "UAL123-test",
        "aircraft_type": "B738",
        "latitude": 33.5,
        "longitude": -117.2,
        "direction": 270,
        "speed": 800,
        "elevation": 10668,
        "elevation_feet": 35000,
        "elevation_change": "-",
        "origin": "Los Angeles",
        "destination": "New York",
        "possibility_level": 3,
        "is_possible_transit": 1,
        "alt_diff": 0.5,
        "az_diff": 0.3,
        "time": 5,
        "min_separation_time": "12:00:00",
        "target_name": "sun",
    }
    with patch("app.get_transits", return_value={"flights": [mock_transit]}):
        resp = client.get(f"/flights?{OBSERVER_QS}")
    body = resp.get_json()
    flights = body.get("flights", [])
    assert len(flights) >= 1, "Expected at least one flight in response"
    required_keys = {"id", "latitude", "longitude", "is_possible_transit", "possibility_level"}
    for flight in flights[:5]:
        missing = required_keys - set(flight.keys())
        assert not missing, f"Flight missing keys: {missing}\n  Flight: {flight}"
    print(f"✓ Required flight keys present ({len(flights)} flights)")


def test_transit_log_returns_200():
    """GET /transit-log returns without error."""
    client = get_client()
    resp = client.get("/transit-log")
    assert resp.status_code in (200, 302), f"/transit-log returned {resp.status_code}"
    print(f"✓ GET /transit-log → {resp.status_code}")


def test_static_js_served():
    """Core JS file is served from /static/."""
    client = get_client()
    resp = client.get("/static/app.js")
    assert resp.status_code == 200, f"/static/app.js returned {resp.status_code}"
    assert len(resp.data) > 1000, "app.js looks too small"
    print(f"✓ GET /static/app.js → 200 ({len(resp.data):,} bytes)")


# ── runner ────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_index_returns_200,
        test_flights_missing_lat_lon_returns_400,
        test_flights_with_observer_returns_json,
        test_flights_target_coordinates_schema,
        test_flights_response_flight_schema,
        test_transit_log_returns_200,
        test_static_js_served,
    ]

    print("=" * 70)
    print("FLASK ROUTE SMOKE TESTS")
    print("=" * 70)
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"✗ FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"✗ ERROR {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print()
    print("=" * 70)
    if failed == 0:
        print(f"✓ ALL {passed} TESTS PASSED")
        return 0
    else:
        print(f"✗ {failed}/{passed+failed} TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
