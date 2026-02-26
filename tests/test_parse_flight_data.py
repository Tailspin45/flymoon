#!/usr/bin/env python3
"""
Unit tests for src/flight_data.py — parse_fligh_data() unit conversions.

The most dangerous bugs in this module are silent unit errors:
  - FA altitude is in *hundreds of feet*, not feet
  - Speed is in knots, not km/h
  - Destination may be None (N/D flight)

These tests verify every conversion without making any network calls.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.flight_data import parse_fligh_data


# ── fixture builder ───────────────────────────────────────────────────────────

def make_fa_flight(
    ident="UAL123",
    altitude_hundreds_ft=350,    # FA format: hundreds of feet (350 = FL350)
    groundspeed_knots=450,
    heading=90,
    latitude=33.5,
    longitude=-117.5,
    altitude_change="-",
    origin_city="Los Angeles",
    dest_city="New York",
    aircraft_type="B738",
):
    dest = (
        {"code": "KJFK", "code_icao": "KJFK", "code_iata": "JFK",
         "name": "John F Kennedy Intl", "city": dest_city}
        if dest_city else None
    )
    return {
        "ident": ident,
        "aircraft_type": aircraft_type,
        "fa_flight_id": f"{ident}-test",
        "origin": {"code": "KLAX", "code_icao": "KLAX", "code_iata": "LAX",
                   "name": "Los Angeles Intl", "city": origin_city},
        "destination": dest,
        "waypoints": [],
        "last_position": {
            "fa_flight_id": f"{ident}-test",
            "altitude": altitude_hundreds_ft,
            "altitude_change": altitude_change,
            "groundspeed": groundspeed_knots,
            "heading": heading,
            "latitude": latitude,
            "longitude": longitude,
            "timestamp": "2026-02-26T20:00:00Z",
            "update_type": "A",
        },
    }


# ── tests ─────────────────────────────────────────────────────────────────────

def test_altitude_hundreds_of_feet_to_metres():
    """FA altitude=350 (hundreds of feet) → 350 × 100 × 0.3048 = 10,668 m."""
    flight = make_fa_flight(altitude_hundreds_ft=350)
    parsed = parse_fligh_data(flight)

    expected_m = 350 * 100 * 0.3048   # 10,668 m
    assert abs(parsed["elevation"] - expected_m) < 1.0, \
        f"elevation should be {expected_m:.0f} m, got {parsed['elevation']:.0f} m"
    print(f"✓ Altitude 350 (hundreds-ft) → {parsed['elevation']:.0f} m (expected {expected_m:.0f} m)")


def test_altitude_feet_display():
    """elevation_feet should be altitude × 100 (raw feet for display)."""
    flight = make_fa_flight(altitude_hundreds_ft=350)
    parsed = parse_fligh_data(flight)
    assert parsed["elevation_feet"] == 35000, \
        f"elevation_feet should be 35000, got {parsed['elevation_feet']}"
    print(f"✓ Altitude 350 (hundreds-ft) → elevation_feet={parsed['elevation_feet']} ft")


def test_altitude_units_not_raw_feet():
    """Critical: input of 35000 (raw feet) must NOT silently produce ~1036 km."""
    # If someone accidentally passes raw feet (35000) instead of hundreds (350),
    # elevation would be 35000 × 100 × 0.3048 = 1,036,800 m — LEO altitude.
    # This test documents the expected (correct) behaviour for FA format.
    flight = make_fa_flight(altitude_hundreds_ft=350)
    parsed = parse_fligh_data(flight)
    assert parsed["elevation"] < 20_000, \
        f"elevation {parsed['elevation']:.0f} m looks like raw-feet input was used (would be ~1036 km)"
    print(f"✓ Elevation sanity: {parsed['elevation']:.0f} m < 20,000 m (not in orbit)")


def test_speed_knots_to_kmh():
    """450 knots × 1.852 = 833.4 km/h."""
    flight = make_fa_flight(groundspeed_knots=450)
    parsed = parse_fligh_data(flight)
    expected = 450 * 1.852
    assert abs(parsed["speed"] - expected) < 0.1, \
        f"speed should be {expected:.1f} km/h, got {parsed['speed']:.1f} km/h"
    print(f"✓ 450 knots → {parsed['speed']:.1f} km/h (expected {expected:.1f})")


def test_zero_speed():
    """0 knots → 0.0 km/h."""
    flight = make_fa_flight(groundspeed_knots=0)
    parsed = parse_fligh_data(flight)
    assert parsed["speed"] == 0.0
    print("✓ 0 knots → 0.0 km/h")


def test_destination_city_extracted():
    """Destination city is extracted from nested dict."""
    flight = make_fa_flight(dest_city="New York")
    parsed = parse_fligh_data(flight)
    assert parsed["destination"] == "New York", \
        f"destination should be 'New York', got '{parsed['destination']}'"
    print(f"✓ Destination city extracted: '{parsed['destination']}'")


def test_no_destination_returns_nd():
    """None destination dict → 'N/D'."""
    flight = make_fa_flight(dest_city=None)
    parsed = parse_fligh_data(flight)
    assert parsed["destination"] == "N/D", \
        f"destination should be 'N/D', got '{parsed['destination']}'"
    print(f"✓ No destination → 'N/D'")


def test_elevation_change_passthrough():
    """altitude_change is passed through unchanged."""
    for code in ("-", "C", "D"):
        flight = make_fa_flight(altitude_change=code)
        parsed = parse_fligh_data(flight)
        assert parsed["elevation_change"] == code, \
            f"elevation_change should be '{code}', got '{parsed['elevation_change']}'"
    print("✓ elevation_change codes passed through: -, C, D")


def test_identity_fields():
    """ident → name, fa_flight_id, aircraft_type preserved."""
    flight = make_fa_flight(ident="SWA42", aircraft_type="B737")
    parsed = parse_fligh_data(flight)
    assert parsed["name"] == "SWA42"
    assert parsed["aircraft_type"] == "B737"
    assert "fa_flight_id" in parsed
    print("✓ Identity fields (name, aircraft_type, fa_flight_id) preserved")


def test_coordinates_preserved():
    """lat/lon/heading passed through unchanged."""
    flight = make_fa_flight(latitude=40.123, longitude=-75.456, heading=270)
    parsed = parse_fligh_data(flight)
    assert parsed["latitude"]  == 40.123
    assert parsed["longitude"] == -75.456
    assert parsed["direction"] == 270
    print("✓ Coordinates and heading preserved")


# ── runner ────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_altitude_hundreds_of_feet_to_metres,
        test_altitude_feet_display,
        test_altitude_units_not_raw_feet,
        test_speed_knots_to_kmh,
        test_zero_speed,
        test_destination_city_extracted,
        test_no_destination_returns_nd,
        test_elevation_change_passthrough,
        test_identity_fields,
        test_coordinates_preserved,
    ]

    print("=" * 70)
    print("FLIGHT DATA PARSE UNIT TESTS")
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
            print(f"✗ ERROR {t.__name__}: {e}")
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
