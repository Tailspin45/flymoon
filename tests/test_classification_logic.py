#!/usr/bin/env python3
"""
Transit Classification Logic Test

Simple, direct tests of the classification logic to ensure thresholds are correct.
Tests the core functions without complex geometric calculations.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.transit import _angular_separation, get_possibility_level
import math

TARGET_ALT = 45.0  # degrees — used throughout; cos(45°) ≈ 0.707


def test_angular_separation_calculation():
    """Test that angular separation is calculated correctly with cosine correction."""
    print("Testing _angular_separation calculation...")
    print("-" * 60)

    # Expected values computed with cos(45°) ≈ 0.70711
    c = math.cos(math.radians(TARGET_ALT))
    test_cases = [
        # (alt_diff, az_diff, expected_result, description)
        (0,   0,   0.0,                                   "Perfect alignment"),
        (1,   0,   1.0,                                   "1° altitude only — no cosine effect"),
        (0,   1,   round(1.0 * c, 3),                    "1° azimuth only — compressed by cos(45°)"),
        (3,   4,   round(math.sqrt(9 + (4*c)**2), 3),    "Mixed 3°/4° — cosine on az"),
        (1,   1,   round(math.sqrt(1 + c**2), 3),        "1° each — cosine reduces az contribution"),
        (2,   2,   round(math.sqrt(4 + (2*c)**2), 3),    "2° each"),
        (5,   5,   round(math.sqrt(25 + (5*c)**2), 3),   "5° each"),
    ]

    passed = 0
    failed = 0

    for alt_diff, az_diff, expected, description in test_cases:
        result = round(_angular_separation(alt_diff, az_diff, TARGET_ALT), 3)
        if abs(result - expected) < 0.001:
            print(f"✓ PASS: {description}")
            print(f"  Input: alt_diff={alt_diff}°, az_diff={az_diff}° at target_alt={TARGET_ALT}°")
            print(f"  Expected: {expected}°, Got: {result}°")
            passed += 1
        else:
            print(f"✗ FAIL: {description}")
            print(f"  Input: alt_diff={alt_diff}°, az_diff={az_diff}° at target_alt={TARGET_ALT}°")
            print(f"  Expected: {expected}°, Got: {result}°")
            failed += 1
        print()

    print(f"Angular Separation Tests: {passed} passed, {failed} failed\n")
    return failed == 0


def test_classification_thresholds():
    """Test classification threshold boundaries.

    Thresholds (on-sky degrees, cosine-corrected):
      HIGH   ≤ 1.5°
      MEDIUM ≤ 2.5°
      LOW    ≤ 3.0°
      UNLIKELY > 3.0°

    We use az_diff=0 so cosine correction plays no role.
    """
    print("Testing classification thresholds (az_diff=0)...")
    print("-" * 60)

    test_cases = [
        # (alt_diff_as_sep, expected_level_int, expected_name, description)
        (0.0,  3, "HIGH",     "Perfect transit"),
        (0.5,  3, "HIGH",     "0.5° — well inside HIGH"),
        (1.5,  3, "HIGH",     "Exactly at HIGH boundary (1.5°)"),
        (1.51, 2, "MEDIUM",   "Just outside HIGH (1.51°)"),
        (2.0,  2, "MEDIUM",   "Mid MEDIUM range (2.0°)"),
        (2.5,  2, "MEDIUM",   "Exactly at MEDIUM boundary (2.5°)"),
        (2.51, 1, "LOW",      "Just outside MEDIUM (2.51°)"),
        (2.8,  1, "LOW",      "Mid LOW range (2.8°)"),
        (3.0,  1, "LOW",      "Exactly at LOW boundary (3.0°)"),
        (3.01, 0, "UNLIKELY", "Just outside LOW (3.01°)"),
        (10.0, 0, "UNLIKELY", "Far from target"),
    ]

    passed = 0
    failed = 0

    for sep, expected_level, expected_name, description in test_cases:
        result = get_possibility_level(TARGET_ALT, sep, 0.0)
        if result == expected_level:
            print(f"✓ PASS: {description} → {expected_name}")
            passed += 1
        else:
            print(f"✗ FAIL: {description}")
            print(f"  Expected: {expected_name} ({expected_level}), Got: {result}")
            failed += 1

    print(f"\nClassification Tests: {passed} passed, {failed} failed\n")
    return failed == 0


def test_cosine_correction_effect():
    """Verify cosine correction reduces azimuth contribution near zenith.

    At high target altitudes, az differences are geometrically compressed.
    An aircraft 5° off in azimuth at target_alt=89° should still be HIGH.
    """
    print("Testing zenith cosine correction...")
    print("-" * 60)

    test_cases = [
        # (alt_diff, az_diff, target_alt, expected_level_int, expected_name, description)
        (0.0, 5.0,  89.0, 3, "HIGH",   "5° az at zenith ≈ 0.09° on-sky — HIGH"),
        (0.0, 1.0,  45.0, 3, "HIGH",   "1° az at 45° → 0.71° on-sky — HIGH"),
        (0.0, 3.0,  10.0, 1, "LOW",    "3° az at 10° → 2.95° on-sky — LOW (> MEDIUM boundary)"),
        (2.0, 0.0,  45.0, 2, "MEDIUM", "2° alt, 0° az at 45° → 2.0° — MEDIUM"),
        (1.0, 0.0,  89.0, 3, "HIGH",   "1° alt at zenith — no cosine on alt — HIGH"),
    ]

    passed = 0
    failed = 0

    for alt_diff, az_diff, target_alt, expected_level, expected_name, description in test_cases:
        sep = _angular_separation(alt_diff, az_diff, target_alt)
        result = get_possibility_level(target_alt, alt_diff, az_diff)
        if result == expected_level:
            print(f"✓ PASS: {description}")
            print(f"  σ={sep:.3f}°, classified {expected_name}")
            passed += 1
        else:
            print(f"✗ FAIL: {description}")
            print(f"  σ={sep:.3f}°, expected {expected_name} ({expected_level}), got {result}")
            failed += 1
        print()

    print(f"Cosine Correction Tests: {passed} passed, {failed} failed\n")
    return failed == 0


def main():
    """Run all tests."""
    print("=" * 80)
    print("TRANSIT CLASSIFICATION LOGIC TEST SUITE")
    print("=" * 80)
    print()
    print("Testing the core classification logic:")
    print("  - _angular_separation(alt_diff, az_diff, target_alt)")
    print("  - get_possibility_level(target_alt, alt_diff, az_diff)")
    print()
    print("Classification thresholds (on-sky angular separation):")
    print("  HIGH:     σ ≤ 1.5°")
    print("  MEDIUM:   σ ≤ 2.5°")
    print("  LOW:      σ ≤ 3.0°")
    print("  UNLIKELY: σ > 3.0°")
    print()
    print("=" * 80)
    print()

    all_passed = True

    all_passed &= test_angular_separation_calculation()
    all_passed &= test_classification_thresholds()
    all_passed &= test_cosine_correction_effect()

    # Summary
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
