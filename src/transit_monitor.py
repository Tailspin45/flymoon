"""
Background transit monitoring service.
Polls FlightAware API every 10 minutes and uses cached flight data with 
position prediction for transit calculations in between.
"""

import asyncio
import os
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from src import logger
from src.astro import targets_above_horizon
from src.constants import POSSIBLE_TRANSITS_LOGFILENAME, PossibilityLevel
from src.flight_data import save_possible_transits
from src.transit import get_transits


class TransitMonitor:
    """Background service that monitors for upcoming transits."""

    def __init__(self, api_poll_interval: int = 600, calc_interval: int = 30):
        """
        Initialize transit monitor.

        Args:
            api_poll_interval: Seconds between FlightAware API calls (default: 600 = 10 minutes)
            calc_interval: Seconds between transit calculations using cached data (default: 30)
        """
        self.api_poll_interval = api_poll_interval
        self.calc_interval = calc_interval
        self.cached_transits: List[Dict] = []
        self.cached_flight_data: Optional[Dict] = None
        self.last_api_call: Optional[datetime] = None
        self.last_calc: Optional[datetime] = None
        self.running = False
        self.thread = None
        self.disabled_targets: Set[str] = set()

        # Get observer position from environment
        self.latitude = float(os.getenv("OBSERVER_LATITUDE", "0"))
        self.longitude = float(os.getenv("OBSERVER_LONGITUDE", "0"))
        self.elevation = float(os.getenv("OBSERVER_ELEVATION", "0"))

    def start(self):
        """Start the background monitoring thread."""
        if self.running:
            return

        if self.latitude == 0 and self.longitude == 0:
            logger.warning(
                "[TransitMonitor] No observer location configured, transit monitoring disabled"
            )
            return

        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        logger.info(
            f"[TransitMonitor] Started monitoring (API: {self.api_poll_interval}s, calc: {self.calc_interval}s)"
        )
        print(
            f"✅ [TransitMonitor] Started monitoring (API: {self.api_poll_interval}s, calc: {self.calc_interval}s)"
        )

    def stop(self):
        """Stop the background monitoring thread."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("[TransitMonitor] Stopped monitoring")

    def set_disabled_targets(self, disabled: Set[str]) -> None:
        """Update the set of targets the monitor should skip (e.g. {'moon'})."""
        self.disabled_targets = {t.strip().lower() for t in disabled}
        logger.info("[TransitMonitor] Disabled targets: %s", self.disabled_targets or "none")

    def _monitor_loop(self):
        """Background loop that checks for transits."""
        while self.running:
            try:
                now = datetime.now(ZoneInfo("UTC"))

                # Skip all queries when neither Sun nor Moon is above horizon
                if not targets_above_horizon(
                    self.latitude, self.longitude, self.elevation
                ):
                    logger.debug(
                        "[TransitMonitor] Both Sun and Moon below horizon, skipping"
                    )
                    time.sleep(60)
                    continue

                # Check if we need to fetch new flight data from API
                needs_api_call = (
                    self.last_api_call is None
                    or (now - self.last_api_call).total_seconds()
                    >= self.api_poll_interval
                )

                if needs_api_call:
                    logger.info(
                        "[TransitMonitor] Fetching fresh flight data from FlightAware API"
                    )
                    self._check_transits(fetch_flights=True)
                    self.last_api_call = now
                else:
                    # Use cached flight data with position prediction
                    self._check_transits(fetch_flights=False)

                self.last_calc = now

            except Exception as e:
                logger.error(f"[TransitMonitor] Error checking transits: {e}")

            # Sleep for calculation interval
            time.sleep(self.calc_interval)

    def _check_transits(self, fetch_flights: bool = True):
        """
        Check for upcoming transits.

        Args:
            fetch_flights: If True, fetch fresh data from FlightAware API.
                          If False, use cached flight data with position prediction.
        """
        all_transits = []
        datetime.now(ZoneInfo("UTC"))

        for target_name in ["moon", "sun"]:
            if target_name in self.disabled_targets:
                logger.debug("[TransitMonitor] %s disabled by user, skipping", target_name)
                continue
            try:
                # Get transit predictions for this target
                # The get_transits function will handle caching internally
                transit_data = get_transits(
                    latitude=self.latitude,
                    longitude=self.longitude,
                    elevation=self.elevation,
                    target_name=target_name,
                    test_mode=False,
                )

                for transit in transit_data.get("flights", []):
                    # Transit 'time' is minutes until transit (float), not ISO datetime
                    time_minutes = transit.get("time")
                    if time_minutes is None or not isinstance(
                        time_minutes, (int, float)
                    ):
                        continue

                    # Convert minutes to seconds
                    seconds_until = float(time_minutes) * 60

                    # Only include if not passed and within 5 minutes
                    if 0 < seconds_until <= 300:
                        probability = transit.get("possibility_level")

                        # Only include HIGH and MEDIUM probability (compare to enum values, not enums)
                        if probability in [
                            PossibilityLevel.HIGH.value,
                            PossibilityLevel.MEDIUM.value,
                        ]:
                            all_transits.append(
                                {
                                    "flight": transit.get(
                                        "id", transit.get("name", "Unknown")
                                    ),
                                    "target": target_name.title(),
                                    "probability": PossibilityLevel(probability).name,
                                    "seconds_until": int(seconds_until),
                                    "altitude": round(
                                        float(transit.get("target_alt") or 0), 1
                                    ),
                                    "azimuth": round(
                                        float(transit.get("target_az") or 0), 1
                                    ),
                                }
                            )
            except Exception as e:
                import traceback

                logger.warning(
                    f"[TransitMonitor] Error checking {target_name} transits: {e}\n{traceback.format_exc()}"
                )
                continue

        # Sort by time (nearest first)
        all_transits.sort(key=lambda t: t["seconds_until"])

        # Log any newly-seen transits (near-misses) to the CSV so they persist
        # even when the web map isn't open.
        new_flights = {t["flight"] for t in all_transits}
        known_flights = {t["flight"] for t in self.cached_transits}
        newly_detected = new_flights - known_flights
        if newly_detected:
            from datetime import date as _date

            date_ = _date.today().strftime("%Y%m%d")
            log_rows = []
            for transit in all_transits:
                if transit["flight"] not in newly_detected:
                    continue
                # Build a minimal row compatible with save_possible_transits schema
                log_rows.append(
                    {
                        "id": transit["flight"],
                        "is_possible_transit": 1,
                        "possibility_level": PossibilityLevel[
                            transit["probability"]
                        ].value,
                        "target_alt": transit["altitude"],
                        "target_az": transit["azimuth"],
                        "time": round(transit["seconds_until"] / 60, 3),
                        "fa_flight_id": "",
                        "origin": "",
                        "destination": "",
                        "aircraft_type": "",
                        "aircraft_elevation": 0,
                        "speed": 0,
                        "latitude": 0,
                        "longitude": 0,
                        "alt_diff": 0,
                        "az_diff": 0,
                        "plane_alt": 0,
                        "plane_az": 0,
                        "direction": 0,
                        "elevation_change": "",
                        "vertical_rate": None,
                        "category": None,
                        "squawk": None,
                        "on_ground": False,
                        "icao24": "",
                        "origin_country": None,
                        "position_source": "detection",
                        "position_age_s": None,
                        "scope_connected": False,
                        "scope_mode": "",
                    }
                )
            if log_rows:
                try:
                    asyncio.run(
                        save_possible_transits(
                            log_rows,
                            POSSIBLE_TRANSITS_LOGFILENAME.format(date_=date_),
                        )
                    )
                    logger.info(
                        f"[TransitMonitor] Logged {len(log_rows)} near-miss transit(s): "
                        f"{', '.join(newly_detected)}"
                    )
                except Exception as e:
                    logger.warning(f"[TransitMonitor] Near-miss log write failed: {e}")

        # Update cache
        self.cached_transits = all_transits

        if len(all_transits) > 0:
            source = "API" if fetch_flights else "cache"
            logger.info(
                f"[TransitMonitor] Found {len(all_transits)} imminent transits (source: {source})"
            )

    def get_transits(self) -> Dict:
        """Get cached transit data."""
        return {
            "success": True,
            "transits": self.cached_transits,
            "count": len(self.cached_transits),
            "last_api_call": (
                self.last_api_call.isoformat() if self.last_api_call else None
            ),
            "last_calc": self.last_calc.isoformat() if self.last_calc else None,
        }


# Global instance
_monitor = None


def get_monitor() -> TransitMonitor:
    """Get the global transit monitor instance."""
    global _monitor
    if _monitor is None:
        # API every 10 minutes, calculations every 30 seconds
        _monitor = TransitMonitor(api_poll_interval=600, calc_interval=30)
    return _monitor
