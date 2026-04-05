#!/usr/bin/env python3
"""
Transit Algorithm Validator

Comprehensive test suite to validate the transit detection and classification
algorithm. Generates synthetic test cases with known geometric properties and
verifies the algorithm produces correct results.

Test Categories:
1. Threshold Boundaries - Test exact classification boundaries
2. Direct Transits - Aircraft passing through target center
3. Approach Angles - Various flight path geometries
4. Altitude Variations - Different aircraft altitudes
5. Edge Cases - Minimum altitude, horizon proximity

Classification Thresholds (must match src/transit.py get_possibility_level):
- HIGH:     angular_separation ≤ 2.0°
- MEDIUM:   angular_separation ≤ 4.0°
- LOW:      angular_separation ≤ 12.0°
- UNLIKELY: angular_separation > 12.0°
"""

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from skyfield.api import wgs84

from src.astro import CelestialObject
from src.constants import ASTRO_EPHEMERIS, EARTH_TIMESCALE
from src.transit import angular_separation, check_transit

# Test configuration
OBSERVER_LAT = 33.11
OBSERVER_LON = -117.31
OBSERVER_ELEV = 100  # meters

TARGET_ALT = 40.0  # degrees
TARGET_AZ = 180.0  # degrees (due south)
MIN_ALTITUDE_THRESHOLD = 15.0  # degrees

EARTH = ASTRO_EPHEMERIS["earth"]


class _FixedAngle:
    """Minimal Skyfield Angle stand-in for fixed test positions."""

    def __init__(self, degrees):
        self.degrees = degrees


class FixedCelestialObject:
    """Mock CelestialObject pinned to fixed alt/az for deterministic tests."""

    def __init__(self, altitude_deg, azimuth_deg):
        self.name = "mock"
        self.altitude = _FixedAngle(altitude_deg)
        self.azimuthal = _FixedAngle(azimuth_deg)

    def update_position(self, ref_datetime):
        pass  # position stays fixed


class TestCase:
    """Represents a single test case with expected results."""

    def __init__(
        self,
        name,
        description,
        aircraft_lat,
        aircraft_lon,
        aircraft_alt_m,
        aircraft_speed_kmh,
        aircraft_heading,
        expected_separation,
        expected_classification,
    ):
        self.name = name
        self.description = description
        self.aircraft_lat = aircraft_lat
        self.aircraft_lon = aircraft_lon
        self.aircraft_alt_m = aircraft_alt_m
        self.aircraft_speed_kmh = aircraft_speed_kmh
        self.aircraft_heading = aircraft_heading
        self.expected_separation = expected_separation
        self.expected_classification = expected_classification

    def to_flight_dict(self):
        """Convert to flight data dictionary format."""
        return {
            "name": self.name,
            "latitude": self.aircraft_lat,
            "longitude": self.aircraft_lon,
            "elevation": self.aircraft_alt_m,
            "speed": self.aircraft_speed_kmh,
            "direction": self.aircraft_heading,
            "origin": "TEST_ORIGIN",
            "destination": "TEST_DEST",
            "elevation_change": "-",
        }


def calculate_position_at_bearing(lat, lon, bearing_deg, distance_km):
    """Calculate lat/lon at a specific bearing and distance from a point.

    Uses Haversine formula for accurate positioning on sphere.
    """
    R = 6371  # Earth radius in km
    d = distance_km / R  # Angular distance

    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    bearing_rad = math.radians(bearing_deg)

    new_lat_rad = math.asin(
        math.sin(lat_rad) * math.cos(d)
        + math.cos(lat_rad) * math.sin(d) * math.cos(bearing_rad)
    )

    new_lon_rad = lon_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(d) * math.cos(lat_rad),
        math.cos(d) - math.sin(lat_rad) * math.sin(new_lat_rad),
    )

    return math.degrees(new_lat_rad), math.degrees(new_lon_rad)


def calculate_ground_truth_separation(
    aircraft_lat,
    aircraft_lon,
    aircraft_alt_m,
    observer_lat,
    observer_lon,
    observer_elev_m,
    target_alt_deg,
    target_az_deg,
):
    """Calculate the true angular separation using geometric calculations.

    This is our "ground truth" - independent calculation to verify algorithm.
    """
    # Calculate aircraft alt-az from observer
    my_position = EARTH + wgs84.latlon(
        observer_lat, observer_lon, elevation_m=observer_elev_m
    )

    ref_datetime = datetime.now(timezone.utc)
    time_ = EARTH_TIMESCALE.from_datetime(ref_datetime)

    aircraft_position = EARTH + wgs84.latlon(
        aircraft_lat, aircraft_lon, elevation_m=aircraft_alt_m
    )
    aircraft_alt, aircraft_az, _ = (aircraft_position - my_position).at(time_).altaz()

    # Spherical angular separation — matches the formula used in check_transit()
    angular_sep = angular_separation(
        aircraft_alt.degrees, aircraft_az.degrees, target_alt_deg, target_az_deg
    )

    alt_diff = abs(aircraft_alt.degrees - target_alt_deg)
    az_diff_raw = abs(aircraft_az.degrees - target_az_deg)
    az_diff = min(az_diff_raw, 360 - az_diff_raw)

    return angular_sep, alt_diff, az_diff, aircraft_alt.degrees, aircraft_az.degrees


def generate_test_cases():
    """Generate comprehensive set of test cases.

    All positions are anchored to the transit point — the plan-view ground
    projection where an aircraft at a given altitude appears directly in front
    of the target (alt/az match).  Lateral (east) lon offsets create a known
    angular miss distance (azimuth offset) while keeping alt_diff ≈ 0 at t=0.
    Aircraft head south (away from observer) so t=0 is always the closest
    approach; the algorithm's 15-minute forward scan finds the same minimum.
    """
    test_cases = []

    # ── Geometry helpers ──────────────────────────────────────────────────────
    def transit_km(alt_m):
        """Horizontal distance (km) where aircraft at alt_m appears at TARGET_ALT°."""
        return (alt_m - OBSERVER_ELEV) / math.tan(math.radians(TARGET_ALT)) / 1000.0

    def transit_pos(alt_m):
        """Lat/lon of the transit point for aircraft at alt_m."""
        return calculate_position_at_bearing(
            OBSERVER_LAT, OBSERVER_LON, TARGET_AZ, transit_km(alt_m)
        )

    def az_diff_for_spherical_sep(spherical_sep_deg):
        """Az offset (°) that produces spherical_sep_deg at TARGET_ALT elevation.
        Inverse of: cos(sep) = sin²(alt) + cos²(alt)*cos(daz)
        """
        alt_r = math.radians(TARGET_ALT)
        sep_r = math.radians(spherical_sep_deg)
        cos_daz = (math.cos(sep_r) - math.sin(alt_r) ** 2) / math.cos(alt_r) ** 2
        return math.degrees(math.acos(max(-1.0, min(1.0, cos_daz))))

    def east_lon_for_sep(spherical_sep_deg, alt_m):
        """East lon offset (°) that produces the given spherical angular separation."""
        az_d = az_diff_for_spherical_sep(spherical_sep_deg)
        d_east_m = transit_km(alt_m) * 1000.0 * math.tan(math.radians(az_d))
        m_per_deg = 111320.0 * math.cos(math.radians(OBSERVER_LAT))
        return d_east_m / m_per_deg

    ALT_STD = 10668  # 35,000 ft — standard altitude for boundary/approach tests
    t_lat, t_lon = transit_pos(ALT_STD)

    # =========================================================================
    # CATEGORY 1: Threshold Boundary Tests
    # Tests the actual classification boundaries: HIGH≤2°, MEDIUM≤4°, LOW≤12°
    # Aircraft at transit lat + lateral east offset → az_diff = expected_sep,
    # alt_diff ≈ 0.  Heading 180° (south, away from observer) keeps t=0 as min.
    # =========================================================================
    for name, sep, cls in [
        ("BOUNDARY_HIGH_1.9",      1.9,  3),  # inside HIGH  (≤2°)
        ("BOUNDARY_MED_2.1",       2.1,  2),  # inside MEDIUM (≤4°)
        ("BOUNDARY_MED_3.9",       3.9,  2),  # inside MEDIUM (≤4°)
        ("BOUNDARY_LOW_4.1",       4.1,  1),  # inside LOW   (≤12°)
        ("BOUNDARY_LOW_11.0",     11.0,  1),  # inside LOW   (≤12°)
        ("BOUNDARY_UNLIKELY_13.0",13.0,  0),  # UNLIKELY     (>12°)
    ]:
        lbl = {3: "HIGH", 2: "MEDIUM", 1: "LOW", 0: "UNLIKELY"}[cls]
        test_cases.append(TestCase(
            name=name,
            description=f"{sep}° lateral miss → {lbl}",
            aircraft_lat=t_lat,
            aircraft_lon=t_lon + east_lon_for_sep(sep, ALT_STD),
            aircraft_alt_m=ALT_STD,
            aircraft_speed_kmh=800,
            aircraft_heading=180,  # south — away from observer → min at t=0
            expected_separation=sep,
            expected_classification=cls,
        ))

    # =========================================================================
    # CATEGORY 2: Direct Transit
    # =========================================================================
    test_cases.append(TestCase(
        name="DIRECT_TRANSIT_0.0",
        description="Perfect alignment — aircraft exactly on transit point",
        aircraft_lat=t_lat,
        aircraft_lon=t_lon,
        aircraft_alt_m=ALT_STD,
        aircraft_speed_kmh=800,
        aircraft_heading=0,
        expected_separation=0.0,
        expected_classification=3,  # HIGH
    ))

    # =========================================================================
    # CATEGORY 3: Approach Heading Tests
    # All aircraft start AT the transit lat with a 1.5° east lateral offset.
    # Different headings all move away from the lateral miss distance → min at t=0.
    # =========================================================================
    APPROACH_SEP = 1.5
    d_lon_app = east_lon_for_sep(APPROACH_SEP, ALT_STD)
    for angle_name, heading in [
        ("NORTH",  0),    # north → alt increases, az_diff constant → sep grows
        ("EAST",   90),   # east  → az_diff increases → sep grows
        ("NE",     45),   # northeast → both increase
        ("SOUTH",  180),  # south → alt decreases, az_diff constant → sep grows
    ]:
        test_cases.append(TestCase(
            name=f"APPROACH_{angle_name}_{APPROACH_SEP}",
            description=f"1.5° miss, {angle_name} heading ({heading}°)",
            aircraft_lat=t_lat,
            aircraft_lon=t_lon + d_lon_app,
            aircraft_alt_m=ALT_STD,
            aircraft_speed_kmh=800,
            aircraft_heading=heading,
            expected_separation=APPROACH_SEP,
            expected_classification=3,  # HIGH — 1.5° < 2°
        ))

    # =========================================================================
    # CATEGORY 4: Altitude Variation Tests
    # Same 1.5° miss at each altitude's own transit distance.
    # All classify HIGH because sep=1.5° < 2° regardless of altitude.
    # =========================================================================
    for alt_name, alt_ft, alt_m in [
        ("LOW",  10000,  3048),
        ("MED",  25000,  7620),
        ("HIGH", 45000, 13716),
    ]:
        a_lat, a_lon = transit_pos(alt_m)
        test_cases.append(TestCase(
            name=f"ALTITUDE_{alt_name}_{APPROACH_SEP}",
            description=f"1.5° miss at {alt_ft} ft",
            aircraft_lat=a_lat,
            aircraft_lon=a_lon + east_lon_for_sep(APPROACH_SEP, alt_m),
            aircraft_alt_m=alt_m,
            aircraft_speed_kmh=800,
            aircraft_heading=180,
            expected_separation=APPROACH_SEP,
            expected_classification=3,  # HIGH
        ))

    # =========================================================================
    # CATEGORY 5: Edge Cases — speed should not affect classification
    # =========================================================================
    d_lon_edge = east_lon_for_sep(1.0, ALT_STD)
    test_cases.append(TestCase(
        name="EDGE_FAST_AIRCRAFT",
        description="Fast aircraft (600 kts = 1111 km/h) — speed must not affect classification",
        aircraft_lat=t_lat,
        aircraft_lon=t_lon + d_lon_edge,
        aircraft_alt_m=ALT_STD,
        aircraft_speed_kmh=1111,
        aircraft_heading=180,
        expected_separation=1.0,
        expected_classification=3,  # HIGH
    ))
    test_cases.append(TestCase(
        name="EDGE_SLOW_AIRCRAFT",
        description="Slow aircraft (100 kts = 185 km/h) — speed must not affect classification",
        aircraft_lat=t_lat,
        aircraft_lon=t_lon + d_lon_edge,
        aircraft_alt_m=ALT_STD,
        aircraft_speed_kmh=185,
        aircraft_heading=180,
        expected_separation=1.0,
        expected_classification=3,  # HIGH
    ))

    return test_cases


def run_test(test_case, my_position, target, ref_datetime):
    """Run a single test case and return results."""

    # Generate flight data
    flight = test_case.to_flight_dict()

    # Calculate ground truth
    ground_truth_sep, gt_alt_diff, gt_az_diff, aircraft_alt, aircraft_az = (
        calculate_ground_truth_separation(
            test_case.aircraft_lat,
            test_case.aircraft_lon,
            test_case.aircraft_alt_m,
            OBSERVER_LAT,
            OBSERVER_LON,
            OBSERVER_ELEV,
            TARGET_ALT,
            TARGET_AZ,
        )
    )

    # Run algorithm
    window_time = np.linspace(0, 15, 900)  # 15 minutes, 1 second intervals
    result = check_transit(
        flight, window_time, ref_datetime, my_position, target, EARTH
    )

    # Extract results
    algorithm_sep = result.get("angular_separation")
    algorithm_classification = result.get("possibility_level")
    algorithm_alt_diff = result.get("alt_diff")
    algorithm_az_diff = result.get("az_diff")

    # Verify classification
    expected_classification_value = test_case.expected_classification

    # Check if test passed
    passed = True
    errors = []

    if algorithm_classification != expected_classification_value:
        passed = False
        errors.append(
            f"Classification mismatch: expected {expected_classification_value}, got {algorithm_classification}"
        )

    # Tolerance scales with angle: flat-earth approx error grows at large offsets
    sep_tol = max(0.15, 0.04 * (test_case.expected_separation or 1.0))
    if algorithm_sep is not None and abs(ground_truth_sep - algorithm_sep) > sep_tol:
        passed = False
        errors.append(
            f"Separation mismatch: ground_truth={ground_truth_sep:.3f}°, algorithm={algorithm_sep:.3f}°"
        )

    return {
        "test_name": test_case.name,
        "description": test_case.description,
        "passed": passed,
        "errors": errors,
        "ground_truth": {
            "angular_separation": round(ground_truth_sep, 3),
            "alt_diff": round(gt_alt_diff, 3),
            "az_diff": round(gt_az_diff, 3),
            "aircraft_alt": round(aircraft_alt, 2),
            "aircraft_az": round(aircraft_az, 2),
        },
        "algorithm": {
            "angular_separation": algorithm_sep,
            "alt_diff": algorithm_alt_diff,
            "az_diff": algorithm_az_diff,
            "classification": algorithm_classification,
        },
        "expected": {
            "classification": expected_classification_value,
        },
    }


def main():
    """Run the complete test suite."""

    print("=" * 80)
    print("TRANSIT ALGORITHM VALIDATION SUITE")
    print("=" * 80)
    print()
    print("Testing configuration:")
    print(f"  Observer: ({OBSERVER_LAT}, {OBSERVER_LON}) at {OBSERVER_ELEV}m")
    print(f"  Target: altitude={TARGET_ALT}°, azimuth={TARGET_AZ}°")
    print(f"  Minimum altitude threshold: {MIN_ALTITUDE_THRESHOLD}°")
    print()
    print("Classification thresholds:")
    print("  HIGH:     angular_separation ≤ 2.0°")
    print("  MEDIUM:   angular_separation ≤ 4.0°")
    print("  LOW:      angular_separation ≤ 12.0°")
    print("  UNLIKELY: angular_separation > 12.0°")
    print()
    print("=" * 80)
    print()

    # Setup
    my_position = EARTH + wgs84.latlon(
        OBSERVER_LAT, OBSERVER_LON, elevation_m=OBSERVER_ELEV
    )

    # Use a fixed-position stub so tests are deterministic regardless of real moon position
    target = FixedCelestialObject(TARGET_ALT, TARGET_AZ)

    ref_datetime = datetime.now(timezone.utc)

    # Generate test cases
    test_cases = generate_test_cases()
    print(f"Generated {len(test_cases)} test cases\n")

    # Run tests
    results = []
    passed_count = 0
    failed_count = 0

    for i, test_case in enumerate(test_cases, 1):
        print(f"[{i}/{len(test_cases)}] Running: {test_case.name}...")
        result = run_test(test_case, my_position, target, ref_datetime)
        results.append(result)

        if result["passed"]:
            passed_count += 1
            print(f"  ✓ PASS")
        else:
            failed_count += 1
            print(f"  ✗ FAIL")
            for error in result["errors"]:
                print(f"    - {error}")
        print()

    # Summary
    print("=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    print(f"Total tests:  {len(test_cases)}")
    print(f"Passed:       {passed_count} ({100*passed_count/len(test_cases):.1f}%)")
    print(f"Failed:       {failed_count} ({100*failed_count/len(test_cases):.1f}%)")
    print()

    if failed_count > 0:
        print("FAILED TESTS:")
        print("-" * 80)
        for result in results:
            if not result["passed"]:
                print(f"\n{result['test_name']}: {result['description']}")
                print(
                    f"  Expected classification: {result['expected']['classification']}"
                )
                print(
                    f"  Algorithm classification: {result['algorithm']['classification']}"
                )
                print(
                    f"  Ground truth separation: {result['ground_truth']['angular_separation']}°"
                )
                print(
                    f"  Algorithm separation: {result['algorithm']['angular_separation']}°"
                )
                for error in result["errors"]:
                    print(f"  ERROR: {error}")
        print()

    # Save detailed results to JSON
    output_file = Path(__file__).parent / "test_results.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Detailed results saved to: {output_file}")
    print()

    # Return exit code
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
