#!/usr/bin/env python3
"""
Phase 3 Diagnostic: Bounding Box Coverage Analysis
===================================================
Validates that transit_corridor_bbox() produces boxes large enough to
capture all aircraft that could transit Sun/Moon within the prediction window.

Tests:
  1. Bbox contains transit ground point for Sun at various azimuths
  2. Bbox covers typical aircraft 15 min away (worst case 950 km/h)
  3. Bbox doesn't shrink too aggressively at low elevations
  4. Bbox area analysis: is it too large (API credits) or too small (miss risk)?
  5. Coverage at typical FL350 (10,668 m) altitude

Usage:
  python tests/diag_phase3_bbox_coverage.py
  python tests/diag_phase3_bbox_coverage.py --output docs/diag_logs/bbox_map.html
"""
import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.constants import ASTRO_EPHEMERIS, EARTH_TIMESCALE
from src.position import geographic_to_altaz, get_my_pos, predict_position, transit_corridor_bbox

EARTH = ASTRO_EPHEMERIS["earth"]


def _pass(name: str, detail: str = "") -> dict:
    msg = f"  PASS  {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return {"test": name, "status": "PASS", "detail": detail}


def _fail(name: str, detail: str = "") -> dict:
    msg = f"  FAIL  {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return {"test": name, "status": "FAIL", "detail": detail}


def _info(msg: str):
    print(f"        {msg}")


def _bbox_area_deg2(bbox):
    """Approximate area of bbox in deg²."""
    return (bbox.lat_upper_right - bbox.lat_lower_left) * (
        bbox.long_upper_right - bbox.long_lower_left
    )


def _bbox_area_km2(bbox, center_lat):
    """Approximate area in km²."""
    lat_span_km = (bbox.lat_upper_right - bbox.lat_lower_left) * 111.32
    lon_span_km = (
        (bbox.long_upper_right - bbox.long_lower_left)
        * 111.32
        * math.cos(math.radians(center_lat))
    )
    return lat_span_km * lon_span_km


def _haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _project(lat_r, lon_r, bearing_r, dist_km):
    """Project from (lat_r, lon_r) by dist_km along bearing_r."""
    R = 6371.0
    ratio = dist_km / R
    new_lat_r = math.asin(
        math.sin(lat_r) * math.cos(ratio)
        + math.cos(lat_r) * math.sin(ratio) * math.cos(bearing_r)
    )
    new_lon_r = lon_r + math.atan2(
        math.sin(bearing_r) * math.sin(ratio) * math.cos(lat_r),
        math.cos(ratio) - math.sin(lat_r) * math.sin(new_lat_r),
    )
    return math.degrees(new_lat_r), math.degrees(new_lon_r)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_bbox_contains_ground_point_multi_az(obs_lat, obs_lon):
    """Bbox must contain transit ground point for Sun at 8 azimuths."""
    alt = 30.0  # moderate altitude
    h_km = 10.0
    d_km = (h_km) / math.tan(math.radians(alt))

    all_pass = True
    for az in [0, 45, 90, 135, 180, 225, 270, 315]:
        bbox = transit_corridor_bbox(obs_lat, obs_lon, alt, az)
        bearing = math.radians(az)
        obs_lat_r = math.radians(obs_lat)
        obs_lon_r = math.radians(obs_lon)
        gp_lat, gp_lon = _project(obs_lat_r, obs_lon_r, bearing, d_km)

        in_bbox = (
            bbox.lat_lower_left <= gp_lat <= bbox.lat_upper_right
            and bbox.long_lower_left <= gp_lon <= bbox.long_upper_right
        )
        if not in_bbox:
            _info(f"  az={az:3d}° MISS: gp=({gp_lat:.2f},{gp_lon:.2f}) bbox=[({bbox.lat_lower_left:.2f},{bbox.long_lower_left:.2f}),({bbox.lat_upper_right:.2f},{bbox.long_upper_right:.2f})]")
            all_pass = False

    return (
        _pass("bbox contains ground point (8 azimuths)")
        if all_pass
        else _fail("bbox contains ground point (8 azimuths)", "one or more azimuths missed")
    )


def test_bbox_captures_15min_inbound_aircraft(obs_lat, obs_lon):
    """
    Place a fast inbound aircraft 15 min away (950 km/h = 237 km) from the
    transit ground point and verify it falls inside the bbox.
    """
    target_alt = 40.0  # high sun
    target_az = 180.0  # due south
    h_km = 10.0
    d_km = h_km / math.tan(math.radians(target_alt))

    obs_lat_r = math.radians(obs_lat)
    obs_lon_r = math.radians(obs_lon)
    bearing = math.radians(target_az)
    tgp_lat, tgp_lon = _project(obs_lat_r, obs_lon_r, bearing, d_km)
    tgp_lat_r = math.radians(tgp_lat)
    tgp_lon_r = math.radians(tgp_lon)

    # Aircraft 237 km further south (same azimuth) from TGP
    travel_km = 950.0 * (15.0 / 60.0)  # 237.5 km
    aircraft_lat, aircraft_lon = _project(tgp_lat_r, tgp_lon_r, bearing, travel_km)

    bbox = transit_corridor_bbox(obs_lat, obs_lon, target_alt, target_az)

    in_bbox = (
        bbox.lat_lower_left <= aircraft_lat <= bbox.lat_upper_right
        and bbox.long_lower_left <= aircraft_lon <= bbox.long_upper_right
    )
    _info(
        f"TGP: ({tgp_lat:.2f},{tgp_lon:.2f})  "
        f"Aircraft 15 min away: ({aircraft_lat:.2f},{aircraft_lon:.2f})"
    )
    _info(
        f"Bbox: ({bbox.lat_lower_left:.2f},{bbox.long_lower_left:.2f}) → "
        f"({bbox.lat_upper_right:.2f},{bbox.long_upper_right:.2f})"
    )

    return (
        _pass("bbox captures 15-min inbound aircraft", f"aircraft at ({aircraft_lat:.2f},{aircraft_lon:.2f})")
        if in_bbox
        else _fail("bbox captures 15-min inbound aircraft", f"aircraft ({aircraft_lat:.2f},{aircraft_lon:.2f}) outside bbox")
    )


def test_bbox_low_altitude_not_degenerate(obs_lat, obs_lon):
    """At low target altitude (5°), bbox should still be valid and large (not truncated)."""
    bbox_5 = transit_corridor_bbox(obs_lat, obs_lon, 5.0, 180.0)
    bbox_30 = transit_corridor_bbox(obs_lat, obs_lon, 30.0, 180.0)

    area_5 = _bbox_area_km2(bbox_5, obs_lat)
    area_30 = _bbox_area_km2(bbox_30, obs_lat)

    _info(f"Alt=5°:  area={area_5:,.0f} km²  bbox width={bbox_5.long_upper_right - bbox_5.long_lower_left:.2f}°")
    _info(f"Alt=30°: area={area_30:,.0f} km²  bbox width={bbox_30.long_upper_right - bbox_30.long_lower_left:.2f}°")

    valid = (
        bbox_5.lat_lower_left < bbox_5.lat_upper_right
        and bbox_5.long_lower_left < bbox_5.long_upper_right
    )
    return (
        _pass("bbox low altitude valid", f"area={area_5:,.0f} km²")
        if valid
        else _fail("bbox low altitude valid", "degenerate bbox at low altitude")
    )


def test_bbox_size_api_budget(obs_lat, obs_lon):
    """
    Assess typical bbox area at various sun altitudes.
    OpenSky returns ~10-200 aircraft per large bbox query.
    Flag if bbox exceeds 400 km² (too large, wasteful).
    """
    print()
    print("  Bbox size vs target altitude (for API budget planning):")
    print(f"  {'Alt':>5}  {'Width°':>8}  {'Height°':>8}  {'Area km²':>12}  {'OpenSky budget note'}")

    rows = []
    for alt in [5, 10, 20, 30, 45, 60, 75]:
        bbox = transit_corridor_bbox(obs_lat, obs_lon, alt, 180.0)
        w = bbox.long_upper_right - bbox.long_lower_left
        h = bbox.lat_upper_right - bbox.lat_lower_left
        area = _bbox_area_km2(bbox, obs_lat)
        note = "OK" if area < 2_000_000 else "LARGE"
        print(f"  {alt:5}°  {w:8.2f}  {h:8.2f}  {area:12,.0f}  {note}")
        rows.append({"alt_deg": alt, "width_deg": round(w, 3), "height_deg": round(h, 3), "area_km2": round(area)})

    return rows


def test_bbox_fl350_altitude(obs_lat, obs_lon):
    """
    At FL350 (10,668 m), verify bbox captures aircraft 15 min away.
    This is the standard cruise altitude for commercial jets.
    """
    fl350_m = 10_668.0
    target_alt = 35.0
    target_az = 200.0  # SW

    bbox = transit_corridor_bbox(
        obs_lat, obs_lon, target_alt, target_az,
        aircraft_altitude_m=fl350_m,
    )
    area = _bbox_area_km2(bbox, obs_lat)

    valid = (
        bbox.lat_lower_left < bbox.lat_upper_right
        and bbox.long_lower_left < bbox.long_upper_right
    )
    _info(f"FL350 bbox area: {area:,.0f} km²")

    return (
        _pass("bbox FL350 altitude valid", f"area={area:,.0f} km²")
        if valid
        else _fail("bbox FL350 altitude valid", "degenerate bbox")
    )


def generate_html_map(obs_lat, obs_lon, output_path):
    """Generate a simple Leaflet map showing bbox coverage at various sun positions."""
    bboxes = []
    for alt in [10, 20, 30, 45, 60]:
        for az in [90, 180, 270]:
            bbox = transit_corridor_bbox(obs_lat, obs_lon, alt, az)
            bboxes.append({
                "alt": alt, "az": az,
                "lat_ll": bbox.lat_lower_left,
                "lon_ll": bbox.long_lower_left,
                "lat_ur": bbox.lat_upper_right,
                "lon_ur": bbox.long_upper_right,
            })

    colors = {10: "red", 20: "orange", 30: "yellow", 45: "green", 60: "blue"}

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<title>Phase 3: Bbox Coverage Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>body{{margin:0}} #map{{height:100vh}}</style>
</head><body>
<div id="map"></div>
<script>
var map = L.map('map').setView([{obs_lat}, {obs_lon}], 7);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  {{attribution:'© OpenStreetMap'}}).addTo(map);
L.circleMarker([{obs_lat}, {obs_lon}], {{radius:8,color:'black',fillColor:'white',fillOpacity:1}})
  .bindPopup('Observer').addTo(map);
"""
    for b in bboxes:
        color = colors.get(b["alt"], "gray")
        popup = f"alt={b['alt']}° az={b['az']}°"
        html += (
            f"L.rectangle([[{b['lat_ll']},{b['lon_ll']}],[{b['lat_ur']},{b['lon_ur']}]],"
            f"{{color:'{color}',weight:1,fillOpacity:0.05}})"
            f".bindPopup('{popup}').addTo(map);\n"
        )

    html += """
// Legend
var legend = L.control({position:'bottomright'});
legend.onAdd = function() {
  var d = L.DomUtil.create('div','info legend');
  d.innerHTML = '<b>Target alt</b><br>';
"""
    for alt, color in colors.items():
        html += f"  d.innerHTML += '<span style=\"color:{color}\">■</span> {alt}°<br>';\n"
    html += """  return d;};
legend.addTo(map);
</script></body></html>"""

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)
    print(f"  HTML map written to {output_path}")
    return bboxes


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--observer-lat", type=float, default=33.111369)
    parser.add_argument("--observer-lon", type=float, default=-117.310169)
    parser.add_argument("--output", default="docs/diag_logs/bbox_map.html")
    parser.add_argument("--json-output", default="docs/diag_logs/phase3_bbox_results.json")
    args = parser.parse_args()

    obs_lat = args.observer_lat
    obs_lon = args.observer_lon

    print("=" * 70)
    print("PHASE 3 BBOX COVERAGE ANALYSIS")
    print("=" * 70)
    print(f"Observer: lat={obs_lat}, lon={obs_lon}")
    print()

    results = []

    print("--- 1. Ground point containment (8 azimuths, alt=30°) ---")
    results.append(test_bbox_contains_ground_point_multi_az(obs_lat, obs_lon))
    print()

    print("--- 2. 15-minute inbound aircraft capture ---")
    results.append(test_bbox_captures_15min_inbound_aircraft(obs_lat, obs_lon))
    print()

    print("--- 3. Low altitude (5°) bbox validity ---")
    results.append(test_bbox_low_altitude_not_degenerate(obs_lat, obs_lon))
    print()

    print("--- 4. Bbox size vs altitude (API budget) ---")
    size_table = test_bbox_size_api_budget(obs_lat, obs_lon)
    print()

    print("--- 5. FL350 altitude test ---")
    results.append(test_bbox_fl350_altitude(obs_lat, obs_lon))
    print()

    print("--- 6. Generating HTML coverage map ---")
    bboxes = generate_html_map(obs_lat, obs_lon, args.output)
    print()

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")

    print("=" * 70)
    if failed == 0:
        print(f"ALL {passed} TESTS PASSED")
    else:
        print(f"{failed}/{passed+failed} TESTS FAILED")
    print("=" * 70)

    output = {
        "phase": "phase3_bbox_coverage",
        "observer": {"lat": obs_lat, "lon": obs_lon},
        "tests": results,
        "size_table": size_table,
        "summary": {"passed": passed, "failed": failed},
    }
    out_path = Path(args.json_output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults written to {out_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
