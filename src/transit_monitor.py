"""
Background transit monitoring service.
Polls FlightAware API every 10 minutes and uses cached flight data with 
position prediction for transit calculations in between.
"""

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional
import os

from src.transit import get_transits
from src.constants import PossibilityLevel
from src import logger


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
        
        # Get observer position from environment
        self.latitude = float(os.getenv("OBSERVER_LATITUDE", "0"))
        self.longitude = float(os.getenv("OBSERVER_LONGITUDE", "0"))
        self.elevation = float(os.getenv("OBSERVER_ELEVATION", "0"))
    
    def start(self):
        """Start the background monitoring thread."""
        if self.running:
            return
        
        if self.latitude == 0 and self.longitude == 0:
            logger.warning("[TransitMonitor] No observer location configured, transit monitoring disabled")
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        logger.info(f"[TransitMonitor] Started monitoring (API: {self.api_poll_interval}s, calc: {self.calc_interval}s)")
        print(f"✅ [TransitMonitor] Started monitoring (API: {self.api_poll_interval}s, calc: {self.calc_interval}s)")
    
    def stop(self):
        """Stop the background monitoring thread."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("[TransitMonitor] Stopped monitoring")
    
    def _monitor_loop(self):
        """Background loop that checks for transits."""
        while self.running:
            try:
                now = datetime.now(ZoneInfo("UTC"))
                
                # Check if we need to fetch new flight data from API
                needs_api_call = (
                    self.last_api_call is None or 
                    (now - self.last_api_call).total_seconds() >= self.api_poll_interval
                )
                
                if needs_api_call:
                    logger.info("[TransitMonitor] Fetching fresh flight data from FlightAware API")
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
        now = datetime.now(ZoneInfo("UTC"))
        
        for target_name in ['moon', 'sun']:
            try:
                # Get transit predictions for this target
                # The get_transits function will handle caching internally
                transit_data = get_transits(
                    latitude=self.latitude,
                    longitude=self.longitude,
                    elevation=self.elevation,
                    target_name=target_name,
                    test_mode=False
                )
                
                for transit in transit_data.get('flights', []):
                    # Transit 'time' is minutes until transit (float), not ISO datetime
                    time_minutes = transit.get('time')
                    if time_minutes is None or not isinstance(time_minutes, (int, float)):
                        continue
                    
                    # Convert minutes to seconds
                    seconds_until = float(time_minutes) * 60
                    
                    # Only include if not passed and within 5 minutes
                    if 0 < seconds_until <= 300:
                        probability = transit.get('possibility_level')
                        
                        # Only include HIGH and MEDIUM probability (compare to enum values, not enums)
                        if probability in [PossibilityLevel.HIGH.value, PossibilityLevel.MEDIUM.value]:
                            all_transits.append({
                                'flight': transit.get('name', 'Unknown'),
                                'target': target_name.title(),
                                'probability': PossibilityLevel(probability).name,
                                'seconds_until': int(seconds_until),
                                'altitude': round(float(transit.get('target_altitude', 0)), 1),
                                'azimuth': round(float(transit.get('target_azimuth', 0)), 1)
                            })
            except Exception as e:
                logger.warning(f"[TransitMonitor] Error checking {target_name} transits: {e}")
                continue
        
        # Sort by time (nearest first)
        all_transits.sort(key=lambda t: t['seconds_until'])
        
        # Update cache
        self.cached_transits = all_transits
        
        if len(all_transits) > 0:
            source = "API" if fetch_flights else "cache"
            logger.info(f"[TransitMonitor] Found {len(all_transits)} imminent transits (source: {source})")
    
    def get_transits(self) -> Dict:
        """Get cached transit data."""
        return {
            'success': True,
            'transits': self.cached_transits,
            'count': len(self.cached_transits),
            'last_api_call': self.last_api_call.isoformat() if self.last_api_call else None,
            'last_calc': self.last_calc.isoformat() if self.last_calc else None
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
