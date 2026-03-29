import csv
import json
import os
from datetime import datetime
from http import HTTPStatus
from typing import List

import requests

from src.position import AreaBoundingBox

# Fixed log schema — stable across code changes; waypoints excluded (too large, not useful for analysis)
TRANSIT_LOG_FIELDS = [
    "timestamp",
    "id",
    "fa_flight_id",
    "origin",
    "destination",
    "latitude",
    "longitude",
    "aircraft_elevation",
    "aircraft_elevation_feet",
    "aircraft_type",
    "speed",
    "is_possible_transit",
    "possibility_level",
    "elevation_change",
    "direction",
    "alt_diff",
    "az_diff",
    "time",
    "target_alt",
    "plane_alt",
    "target_az",
    "plane_az",
    "target",
    "distance_nm",
    "position_source",
    "scope_connected",
    "scope_mode",
]


def get_flight_data(
    area_bbox: AreaBoundingBox, url_: str, api_key: str = ""
) -> List[dict]:

    headers = {"Accept": "application/json; charset=UTF-8", "x-apikey": api_key}

    # example: https://aeroapi.flightaware.com/aeroapi/flights/search?query=-latlong+%2221.305695+-104.458904+23.925834+-101.365481%22&max_pages=1
    url = (
        f"{url_}?query=-latlong+%22{area_bbox.lat_lower_left}+{area_bbox.long_lower_left}+"
        f"{area_bbox.lat_upper_right}+{area_bbox.long_upper_right}%22&max_pages=1"
    )

    response = requests.get(url=url, headers=headers, timeout=15)
    if response.status_code == HTTPStatus.OK:
        return response.json()
    else:
        raise Exception(f"Error: {response.status_code}, {response.text}")


def parse_fligh_data(flight_data: dict):
    has_destination = isinstance(flight_data.get("destination"), dict)

    return {
        "name": flight_data["ident"],
        "aircraft_type": flight_data.get("aircraft_type", "N/A"),
        "fa_flight_id": flight_data.get("fa_flight_id", ""),
        "origin": flight_data["origin"]["city"],
        "destination": (
            "N/D"
            if not has_destination
            else flight_data.get("destination", dict()).get("city")
        ),
        "latitude": flight_data["last_position"]["latitude"],
        "longitude": flight_data["last_position"]["longitude"],
        "direction": flight_data["last_position"]["heading"],
        "speed": int(flight_data["last_position"]["groundspeed"]) * 1.852,
        "elevation": int(flight_data["last_position"]["altitude"])
        * 0.3048
        * 100,  # hundreds of feet to meters (for calculations)
        "elevation_feet": int(flight_data["last_position"]["altitude"])
        * 100,  # API returns hundreds of feet, multiply by 100
        "elevation_change": flight_data["last_position"]["altitude_change"],
        "waypoints": flight_data.get("waypoints", []),
    }


def load_existing_flight_data(path: str) -> dict:
    with open(path, "r") as file:
        return json.load(file)


def sort_results(data: List[dict]) -> List[dict]:
    """Sort flight results: transits first, then by smallest combined |alt_diff|+|az_diff|."""

    def _custom_sort(a: dict) -> tuple:
        alt_diff = abs(a.get("alt_diff") or 0)
        az_diff = abs(a.get("az_diff") or 0)
        total_diff = alt_diff + az_diff

        time_val = a["time"] if a["time"] is not None else 999
        # Sort: transits first (descending), then smallest total_diff, then ETA, then id
        return (-(a["is_possible_transit"] or 0), total_diff, time_val, a["id"])

    return sorted(data, key=_custom_sort)


def log_transit_event(event_dict: dict, dest_path: str) -> None:
    """Append one row to the transit event confirmation log (TRANSIT_EVENTS_LOGFILENAME).

    This is a synchronous write called from a background thread inside
    TransitDetector._fire_detection().  Creates the directory and header row if needed.

    Args:
        event_dict: Keys matching TRANSIT_EVENTS_FIELDS (missing keys written as "").
        dest_path:  Absolute or relative path to the daily CSV file.
    """
    from src.constants import TRANSIT_EVENTS_FIELDS

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    needs_header = not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0
    if not needs_header:
        # Verify header matches current schema; if not, start fresh
        with open(dest_path, "r", newline="") as f:
            existing_header = f.readline().strip().split(",")
        if existing_header != TRANSIT_EVENTS_FIELDS:
            import shutil

            shutil.move(dest_path, dest_path.replace(".csv", "_old_schema.csv"))
            needs_header = True

    row = {f: event_dict.get(f, "") for f in TRANSIT_EVENTS_FIELDS}
    with open(dest_path, "a", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=TRANSIT_EVENTS_FIELDS, extrasaction="ignore"
        )
        if needs_header:
            writer.writeheader()
        writer.writerow(row)


async def save_possible_transits(data: List[dict], dest_path: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows_to_write = []

    for flight in data:
        if flight["is_possible_transit"] == 1:
            row = {f: flight.get(f, "") for f in TRANSIT_LOG_FIELDS}
            row["timestamp"] = timestamp
            rows_to_write.append(row)

    if rows_to_write:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        # If file exists but has a different header (schema migration), start fresh
        needs_header = True
        if os.path.exists(dest_path):
            with open(dest_path, "r", newline="") as f:
                existing_header = f.readline().strip().split(",")
            if existing_header == TRANSIT_LOG_FIELDS:
                needs_header = False
            else:
                # Schema mismatch — rename old file and start fresh
                import shutil

                shutil.move(dest_path, dest_path.replace(".csv", "_old_schema.csv"))
        with open(dest_path, "a", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=TRANSIT_LOG_FIELDS, extrasaction="ignore"
            )
            if needs_header:
                writer.writeheader()
            writer.writerows(rows_to_write)
