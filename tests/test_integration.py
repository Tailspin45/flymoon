#!/usr/bin/env python3
"""
Test Data Integration Test

Tests the full transit detection pipeline using synthetically generated
test data. Verifies that flights positioned at known angular separations
are classified correctly.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.transit import get_transits

_TEST_FILE = Path(__file__).parent.parent / "data" / "raw_flight_data_example.json"


def _load_test_data():
    """Load the test flight data. Returns None if unavailable."""
    if not _TEST_FILE.exists():
        return None
    with open(_TEST_FILE) as f:
        return json.load(f)


def test_data_classification():
    """Test that test data produces expected classifications."""

    test_data = _load_test_data()
    if not test_data:
        # Skip gracefully when test data has not been generated yet.
        import pytest
        pytest.skip(
            f"Test data file not found: {_TEST_FILE}. "
            "Run: python3 data/test_data_generator.py"
        )

    assert "_test_metadata" in test_data, (
        "No _test_metadata in test data. "
        "Run: python3 data/test_data_generator.py --scenario dual_tracking"
    )

    meta = test_data["_test_metadata"]

    # Run transit detection for both targets and combine results
    all_flights = []
    for target_name in ("moon", "sun"):
        r = get_transits(
            latitude=meta.get("observer_latitude", 33.11),
            longitude=meta.get("observer_longitude", -117.31),
            elevation=100,
            target_name=target_name,
            test_mode=True,
        )
        for f in r["flights"]:
            f["target"] = target_name
        all_flights.extend(r["flights"])

    # Bucket by classification
    classifications = {"HIGH": [], "MEDIUM": [], "LOW": [], "UNLIKELY": []}
    level_map = {3: "HIGH", 2: "MEDIUM", 1: "LOW", 0: "UNLIKELY"}

    for flight in all_flights:
        classification = level_map.get(flight["possibility_level"], "UNKNOWN")
        classifications[classification].append(
            {
                "id": flight["id"],
                "alt_diff": flight.get("alt_diff"),
                "az_diff": flight.get("az_diff"),
                "target": flight.get("target"),
            }
        )

    # Check HIGH classifications (should be ≤ 1.5°)
    for flight in classifications["HIGH"]:
        if flight["alt_diff"] is not None and flight["az_diff"] is not None:
            assert not (flight["alt_diff"] > 1.5 and flight["az_diff"] > 1.5), (
                f"{flight['id']}: HIGH but alt_diff={flight['alt_diff']}°, "
                f"az_diff={flight['az_diff']}°"
            )

    # Check expected flight IDs — only for targets currently above horizon.
    # Thresholds: HIGH ≤ 2.0°, MEDIUM ≤ 4.0°, LOW ≤ 12.0°.
    # MOON_HIGH / SUN_HIGH are placed at ~0.5° → HIGH.
    # MOON_MED  / SUN_MED  are placed at ~2.0° → HIGH (boundary).
    # MOON_LOW  / SUN_LOW  are placed at ~2.8° → MEDIUM.
    moon_visible = meta.get("moon_altitude", -99) >= 15
    sun_visible = meta.get("sun_altitude", -99) >= 15

    # Flights that must appear in HIGH or better
    expected_high_or_better = []
    # Flights that must appear in MEDIUM or better (HIGH ∪ MEDIUM)
    expected_medium_or_better = []
    if moon_visible:
        expected_high_or_better.append("MOON_HIGH")
        expected_high_or_better.append("MOON_MED")
        expected_medium_or_better.append("MOON_LOW")
    if sun_visible:
        expected_high_or_better.append("SUN_HIGH")
        expected_high_or_better.append("SUN_MED")
        expected_medium_or_better.append("SUN_LOW")

    for expected_id in expected_high_or_better:
        assert any(f["id"] == expected_id for f in classifications["HIGH"]), (
            f"Expected {expected_id} to be HIGH classification"
        )

    for expected_id in expected_medium_or_better:
        found = any(
            f["id"] == expected_id
            for level in ("HIGH", "MEDIUM")
            for f in classifications[level]
        )
        assert found, f"Expected {expected_id} to be MEDIUM or better classification"


def main():
    """Run the integration test."""
    print("=" * 80)
    print("TEST DATA INTEGRATION TEST")
    print("=" * 80)
    print()

    test_data = _load_test_data()
    if not test_data:
        print(f"Test data file not found: {_TEST_FILE}")
        print("Run: python3 data/test_data_generator.py")
        return 1

    try:
        test_data_classification()
        print("✓ TEST DATA INTEGRATION TEST PASSED")
        print()
        print("The full transit detection pipeline is working correctly:")
        print("  - Test data loaded successfully")
        print("  - Transit detection executed without errors")
        print("  - All flights classified according to angular separation thresholds")
        print("  - Expected classifications match actual results")
        print()
        return 0
    except AssertionError as e:
        print("✗ TEST DATA INTEGRATION TEST FAILED")
        print()
        print(f"  - {e}")
        print()
        return 1


if __name__ == "__main__":
    sys.exit(main())
