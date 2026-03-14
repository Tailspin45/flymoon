#!/usr/bin/env python3
"""
Transit Classification Logic Test

Simple, direct tests of the classification logic to ensure thresholds are correct.
Tests the core functions without complex geometric calculations.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


from src.transit import angular_separation, get_possibility_level

TARGET_ALT = 45.0  # degrees — used throughout


def test_angular_separation_calculation():
    """Test that angular separation is calculated correctly with spherical cosines."""
    test_cases = [
        # (alt1, az1, alt2, az2, max_expected, description)
        (45.0, 180.0, 45.0, 180.0, 0.001, "Perfect alignment"),
        (45.0, 180.0, 46.0, 180.0, 1.01, "1° altitude only"),
        (45.0, 180.0, 45.0, 181.0, 0.75, "1° azimuth at 45° — compressed"),
        (45.0, 180.0, 48.0, 184.0, 4.5, "Mixed 3°/4° offset"),
        (45.0, 180.0, 46.0, 181.0, 1.3, "1° each — compressed az"),
    ]

    for alt1, az1, alt2, az2, max_expected, description in test_cases:
        result = round(angular_separation(alt1, az1, alt2, az2), 3)
        assert result <= max_expected, (
            f"{description}: expected ≤ {max_expected}°, got {result}°"
        )


def test_classification_thresholds():
    """Test classification threshold boundaries.

    Thresholds (on-sky degrees):
      HIGH   ≤ 2.0°
      MEDIUM ≤ 4.0°
      LOW    ≤ 12.0°
      UNLIKELY > 12.0°

    get_possibility_level() now takes a single angular separation argument.
    """
    test_cases = [
        # (angular_sep, expected_level_int, expected_name, description)
        (0.0, 3, "HIGH", "Perfect transit"),
        (0.5, 3, "HIGH", "0.5° — well inside HIGH"),
        (2.0, 3, "HIGH", "Exactly at HIGH boundary (2.0°)"),
        (2.01, 2, "MEDIUM", "Just outside HIGH (2.01°)"),
        (3.0, 2, "MEDIUM", "Mid MEDIUM range (3.0°)"),
        (4.0, 2, "MEDIUM", "Exactly at MEDIUM boundary (4.0°)"),
        (4.01, 1, "LOW", "Just outside MEDIUM (4.01°)"),
        (8.0, 1, "LOW", "Mid LOW range (8.0°)"),
        (12.0, 1, "LOW", "Exactly at LOW boundary (12.0°)"),
        (12.01, 0, "UNLIKELY", "Just outside LOW (12.01°)"),
        (50.0, 0, "UNLIKELY", "Far from target"),
    ]

    for sep, expected_level, expected_name, description in test_cases:
        result = get_possibility_level(sep)
        assert result == expected_level, (
            f"{description}: expected {expected_name} ({expected_level}), got {result}"
        )


def test_cosine_correction_effect():
    """Verify cosine correction at zenith via the spherical cosines formula.

    Near the zenith, azimuth differences correspond to very small on-sky
    separations.  The spherical law of cosines handles this naturally.
    """
    test_cases = [
        # (alt1, az1, alt2, az2, expected_level_int, expected_name, description)
        (89.0, 100.0, 89.0, 105.0, 3, "HIGH", "5° az at zenith ≈ tiny on-sky — HIGH"),
        (45.0, 180.0, 45.0, 181.0, 3, "HIGH", "1° az at 45° — HIGH"),
        (
            10.0,
            180.0,
            10.0,
            183.0,
            2,
            "MEDIUM",
            "3° az at 10° → ~2.95° on-sky — MEDIUM",
        ),
        (45.0, 180.0, 47.0, 180.0, 2, "MEDIUM", "2° alt, 0° az at 45° — MEDIUM"),
        (89.0, 100.0, 90.0, 100.0, 3, "HIGH", "1° alt at zenith — HIGH"),
    ]

    for (
        alt1,
        az1,
        alt2,
        az2,
        expected_level,
        expected_name,
        description,
    ) in test_cases:
        sep = angular_separation(alt1, az1, alt2, az2)
        result = get_possibility_level(sep)
        assert result == expected_level, (
            f"{description}: σ={sep:.3f}°, expected {expected_name} ({expected_level}), got {result}"
        )


def main():
    """Run all tests."""
    print("=" * 80)
    print("TRANSIT CLASSIFICATION LOGIC TEST SUITE")
    print("=" * 80)
    print()
    print("Testing the core classification logic:")
    print("  - angular_separation(alt1, az1, alt2, az2)")
    print("  - get_possibility_level(angular_separation)")
    print()
    print("Classification thresholds (on-sky angular separation):")
    print("  HIGH:     σ ≤ 2.0°")
    print("  MEDIUM:   σ ≤ 4.0°")
    print("  LOW:      σ ≤ 12.0°")
    print("  UNLIKELY: σ > 12.0°")
    print()
    print("=" * 80)
    print()

    all_passed = True

    try:
        test_angular_separation_calculation()
        print("✓ Angular separation tests passed")
    except AssertionError as e:
        print(f"✗ Angular separation test failed: {e}")
        all_passed = False

    try:
        test_classification_thresholds()
        print("✓ Classification threshold tests passed")
    except AssertionError as e:
        print(f"✗ Classification threshold test failed: {e}")
        all_passed = False

    try:
        test_cosine_correction_effect()
        print("✓ Cosine correction tests passed")
    except AssertionError as e:
        print(f"✗ Cosine correction test failed: {e}")
        all_passed = False

    print()
    print("=" * 80)
    print("FINAL RESULT")
    print("=" * 80)
    if all_passed:
        print("✓ ALL TESTS PASSED")
        print()
        print("The classification logic is working correctly:")
        print("  - Angular separation calculation is accurate")
        print("  - Threshold boundaries are exact")
        print("  - Combined scenarios produce correct classifications")
        print()
        return 0
    else:
        print("✗ SOME TESTS FAILED")
        print()
        print("Review the failures above to identify issues.")
        print()
        return 1


if __name__ == "__main__":
    sys.exit(main())
