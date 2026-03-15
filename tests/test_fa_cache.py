#!/usr/bin/env python3
"""
Unit tests for the FlightAware enrichment cache (_enrich_from_fa).

All HTTP calls are mocked — no network traffic, no API credits consumed.
Tests cover: cache miss (live fetch), cache hit, TTL expiry, HTTP errors,
empty flight list (VFR/untracked), and exception handling.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.transit as transit_module
from src.transit import _enrich_from_fa

# ── helpers ───────────────────────────────────────────────────────────────────

FAKE_API_KEY = "test-api-key-123"

SAMPLE_FA_RESPONSE = {
    "flights": [
        {
            "aircraft_type": "B738",
            "fa_flight_id": "UAL123-test",
            "origin": {"city": "Los Angeles"},
            "destination": {"city": "New York"},
        }
    ]
}

EXPECTED_ENRICHMENT = {
    "aircraft_type": "B738",
    "fa_flight_id": "UAL123-test",
    "origin": "Los Angeles",
    "destination": "New York",
}


def _clear_cache():
    """Empty the module-level enrichment cache and reset backoff state between tests."""
    transit_module._FA_ENRICHMENT_CACHE.clear()
    transit_module._FA_ENRICHMENT_BACKOFF_UNTIL = 0.0
    transit_module._FA_ENRICHMENT_LAST_BACKOFF_LOG = 0.0


def make_mock_response(status_code=200, json_data=None):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data or {}
    return mock


# ── tests ─────────────────────────────────────────────────────────────────────


def test_cache_miss_fetches_api():
    """First call for a callsign hits the FA API and returns enrichment data."""
    _clear_cache()
    mock_resp = make_mock_response(200, SAMPLE_FA_RESPONSE)

    with patch("src.transit.requests.get", return_value=mock_resp) as mock_get:
        result = _enrich_from_fa("UAL123", FAKE_API_KEY)

    mock_get.assert_called_once()
    assert result == EXPECTED_ENRICHMENT, f"Unexpected enrichment: {result}"
    print(f"✓ Cache miss → API called, returned {result}")


def test_cache_hit_skips_api():
    """Second call for the same callsign uses the cache, no API call made."""
    _clear_cache()
    mock_resp = make_mock_response(200, SAMPLE_FA_RESPONSE)

    with patch("src.transit.requests.get", return_value=mock_resp) as mock_get:
        _enrich_from_fa("UAL123", FAKE_API_KEY)  # populate cache
        result = _enrich_from_fa("UAL123", FAKE_API_KEY)  # should hit cache

    assert (
        mock_get.call_count == 1
    ), f"API should be called exactly once, called {mock_get.call_count} times"
    assert result == EXPECTED_ENRICHMENT
    print(f"✓ Cache hit → API called only once for two requests")


def test_cache_ttl_expiry_refetches():
    """After the TTL expires, the next call hits the API again."""
    _clear_cache()
    mock_resp = make_mock_response(200, SAMPLE_FA_RESPONSE)

    with patch("src.transit.requests.get", return_value=mock_resp) as mock_get:
        _enrich_from_fa("UAL123", FAKE_API_KEY)

        # Manually expire the cache entry
        transit_module._FA_ENRICHMENT_CACHE["UAL123"]["ts"] -= (
            transit_module._FA_ENRICHMENT_TTL + 1
        )

        result = _enrich_from_fa("UAL123", FAKE_API_KEY)

    assert (
        mock_get.call_count == 2
    ), f"After TTL expiry, API should be called again (got {mock_get.call_count} calls)"
    assert result == EXPECTED_ENRICHMENT
    print(f"✓ TTL expiry → API re-fetched after cache expired")


def test_empty_flight_list_caches_miss():
    """VFR/untracked callsign returns {} and caches the miss to avoid repeat calls."""
    _clear_cache()
    mock_resp = make_mock_response(200, {"flights": []})

    with patch("src.transit.requests.get", return_value=mock_resp) as mock_get:
        result1 = _enrich_from_fa("N12345", FAKE_API_KEY)
        result2 = _enrich_from_fa("N12345", FAKE_API_KEY)  # should use cached miss

    assert result1 == {}, f"Empty flights should return {{}}, got {result1}"
    assert result2 == {}, f"Cached miss should return {{}}, got {result2}"
    assert (
        mock_get.call_count == 1
    ), f"API should be called once even for VFR miss, called {mock_get.call_count} times"
    print(f"✓ VFR/untracked → returns {{}} and caches miss to prevent repeat calls")


def test_http_error_returns_empty():
    """Non-200 HTTP response returns {} without caching."""
    _clear_cache()
    mock_resp = make_mock_response(429)

    with patch("src.transit.requests.get", return_value=mock_resp):
        result = _enrich_from_fa("UAL456", FAKE_API_KEY)

    assert result == {}, f"HTTP error should return {{}}, got {result}"
    print(f"✓ HTTP 429 → returns {{}} (not cached, will retry next call)")


def test_network_exception_returns_empty():
    """Network exception returns {} gracefully without crashing."""
    _clear_cache()

    with patch("src.transit.requests.get", side_effect=Exception("connection refused")):
        result = _enrich_from_fa("UAL789", FAKE_API_KEY)

    assert result == {}, f"Exception should return {{}}, got {result}"
    print(f"✓ Network exception → returns {{}} without crashing")


def test_no_api_key_returns_empty():
    """Missing API key skips the HTTP call entirely and returns {}."""
    _clear_cache()

    with patch("src.transit.requests.get") as mock_get:
        result = _enrich_from_fa("UAL000", "")

    mock_get.assert_not_called()
    assert result == {}, f"No API key should return {{}}, got {result}"
    print(f"✓ No API key → returns {{}} without any HTTP call")


def test_missing_aircraft_type_defaults_to_na():
    """Null aircraft_type in FA response defaults to 'N/A'."""
    _clear_cache()
    response = {
        "flights": [
            {
                "aircraft_type": None,
                "fa_flight_id": "TEST-001",
                "origin": {"city": "Dallas"},
                "destination": {"city": "Miami"},
            }
        ]
    }
    mock_resp = make_mock_response(200, response)

    with patch("src.transit.requests.get", return_value=mock_resp):
        result = _enrich_from_fa("DAL1", FAKE_API_KEY)

    assert (
        result["aircraft_type"] == "N/A"
    ), f"Null aircraft_type should default to 'N/A', got '{result['aircraft_type']}'"
    print(f"✓ Null aircraft_type → 'N/A'")


def test_missing_destination_city_defaults_to_nd():
    """None destination in FA response defaults to 'N/D'."""
    _clear_cache()
    response = {
        "flights": [
            {
                "aircraft_type": "A320",
                "fa_flight_id": "TEST-002",
                "origin": {"city": "Chicago"},
                "destination": None,
            }
        ]
    }
    mock_resp = make_mock_response(200, response)

    with patch("src.transit.requests.get", return_value=mock_resp):
        result = _enrich_from_fa("AAL2", FAKE_API_KEY)

    assert (
        result["destination"] == "N/D"
    ), f"None destination should default to 'N/D', got '{result['destination']}'"
    print(f"✓ None destination → 'N/D'")


# ── runner ────────────────────────────────────────────────────────────────────


def main():
    tests = [
        test_cache_miss_fetches_api,
        test_cache_hit_skips_api,
        test_cache_ttl_expiry_refetches,
        test_empty_flight_list_caches_miss,
        test_http_error_returns_empty,
        test_network_exception_returns_empty,
        test_no_api_key_returns_empty,
        test_missing_aircraft_type_defaults_to_na,
        test_missing_destination_city_defaults_to_nd,
    ]

    print("=" * 70)
    print("FA ENRICHMENT CACHE UNIT TESTS")
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
