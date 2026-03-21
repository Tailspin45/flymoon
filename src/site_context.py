"""
Observer site coordinates for server-side astronomy and Seestar init.

The map UI keeps lat/lon/elev in inputs + localStorage; .env may be empty or
stale. Telemetry alt/az and goto_altaz must use the same site as /flights or
azimuth will be wrong (e.g. ~0° when pointed south if server used 0,0).
"""

from __future__ import annotations

import os
import threading
from typing import Optional, Tuple

_lock = threading.Lock()
_override_lat: Optional[float] = None
_override_lon: Optional[float] = None
_override_elev: Optional[float] = None


def clear_observer_browser_override() -> None:
    """Drop browser override so .env OBSERVER_* is used again (e.g. after bad localStorage sync)."""
    global _override_lat, _override_lon, _override_elev
    with _lock:
        _override_lat = None
        _override_lon = None
        _override_elev = None


def set_observer_from_browser(
    latitude: Optional[float],
    longitude: Optional[float],
    elevation: Optional[float] = None,
) -> None:
    """Store observer position pushed from the map page (overrides .env for server)."""
    global _override_lat, _override_lon, _override_elev
    with _lock:
        if latitude is not None:
            _override_lat = float(latitude)
        if longitude is not None:
            _override_lon = float(longitude)
        if elevation is not None:
            _override_elev = float(elevation)


def get_observer_coordinates() -> Tuple[float, float, float]:
    """(lat, lon, elev_m). Browser override wins when set; else .env."""
    with _lock:
        olat, olon, oelev = _override_lat, _override_lon, _override_elev
    lat = (
        olat
        if olat is not None
        else float(os.getenv("OBSERVER_LATITUDE", "0") or "0")
    )
    lon = (
        olon
        if olon is not None
        else float(os.getenv("OBSERVER_LONGITUDE", "0") or "0")
    )
    elev = (
        oelev
        if oelev is not None
        else float(os.getenv("OBSERVER_ELEVATION", "0") or "0")
    )
    return lat, lon, elev


def observer_snapshot_for_api() -> dict:
    """Override fields (if any) plus effective lat/lon used by telescope / astronomy."""
    with _lock:
        snap = {
            "observer_latitude": _override_lat,
            "observer_longitude": _override_lon,
            "observer_elevation": _override_elev,
            "observer_from_browser": _override_lat is not None
            and _override_lon is not None,
        }
    elat, elon, eelev = get_observer_coordinates()
    snap["observer_effective_latitude"] = elat
    snap["observer_effective_longitude"] = elon
    snap["observer_effective_elevation"] = eelev
    return snap
