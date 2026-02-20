"""
OpenSky Network API client for last-mile position refinement.

When a transit candidate is within 60 seconds of predicted transit,
this client queries OpenSky for the most current position data
without consuming FlightAware API credits.

OpenSky free tier limitations:
- Anonymous: 100 requests/day, 5 requests/min
- Registered: 400 requests/day, 10 requests/min
- Data delay: ~5-10 seconds from real-time

Usage:
    client = OpenSkyClient()
    position = client.get_aircraft_position(callsign="UAL123")
    if position:
        lat, lon, alt, heading, speed = position
"""

import time
from typing import Optional, Tuple
import requests
from src import logger


class OpenSkyClient:
    """Client for OpenSky Network API."""
    
    BASE_URL = "https://opensky-network.org/api/states/all"
    
    def __init__(self, username: Optional[str] = None, password: Optional[str] = None):
        """
        Initialize OpenSky client.
        
        Args:
            username: Optional OpenSky account username for higher rate limits
            password: Optional OpenSky account password
        """
        self.auth = (username, password) if username and password else None
        self._last_request_time = 0
        self._min_interval = 12.0  # Seconds between requests (5 req/min for anonymous)
        if self.auth:
            self._min_interval = 6.0  # 10 req/min for registered
    
    def _rate_limit(self):
        """Enforce rate limiting."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            sleep_time = self._min_interval - elapsed
            logger.debug(f"[OpenSky] Rate limiting: sleeping {sleep_time:.1f}s")
            time.sleep(sleep_time)
        self._last_request_time = time.time()
    
    def get_aircraft_position(self, callsign: str) -> Optional[Tuple[float, float, float, float, float]]:
        """
        Get current position of an aircraft by callsign.
        
        Args:
            callsign: Flight callsign (e.g., "UAL123", "SWA456")
            
        Returns:
            Tuple of (latitude, longitude, altitude_m, heading, speed_m_s) or None if not found
        """
        self._rate_limit()
        
        # Normalize callsign (OpenSky uses uppercase, padded to 8 chars)
        callsign_normalized = callsign.upper().strip()
        
        try:
            params = {}
            if self.auth:
                response = requests.get(self.BASE_URL, auth=self.auth, params=params, timeout=10)
            else:
                response = requests.get(self.BASE_URL, params=params, timeout=10)
            
            if response.status_code != 200:
                logger.warning(f"[OpenSky] API returned {response.status_code}")
                return None
            
            data = response.json()
            states = data.get("states", [])
            
            if not states:
                logger.debug("[OpenSky] No aircraft states returned")
                return None
            
            # Find matching aircraft by callsign
            # State vector indices:
            # 0: icao24, 1: callsign, 2: origin_country, 3: time_position,
            # 4: last_contact, 5: longitude, 6: latitude, 7: baro_altitude,
            # 8: on_ground, 9: velocity, 10: true_track, 11: vertical_rate,
            # 12: sensors, 13: geo_altitude, 14: squawk, 15: spi, 16: position_source
            
            for state in states:
                state_callsign = (state[1] or "").strip().upper()
                if state_callsign == callsign_normalized:
                    lat = state[6]
                    lon = state[5]
                    alt = state[7] or state[13]  # baro_altitude or geo_altitude
                    heading = state[10]  # true_track
                    speed = state[9]  # velocity in m/s
                    
                    if lat is not None and lon is not None:
                        logger.info(f"[OpenSky] Found {callsign}: ({lat:.4f}, {lon:.4f}) alt={alt}m hdg={heading}° spd={speed}m/s")
                        return (lat, lon, alt or 0, heading or 0, speed or 0)
            
            logger.debug(f"[OpenSky] Callsign {callsign} not found in {len(states)} states")
            return None
            
        except requests.exceptions.Timeout:
            logger.warning("[OpenSky] Request timed out")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"[OpenSky] Request failed: {e}")
            return None
        except Exception as e:
            logger.error(f"[OpenSky] Unexpected error: {e}")
            return None
    
    def get_aircraft_by_icao24(self, icao24: str) -> Optional[Tuple[float, float, float, float, float]]:
        """
        Get current position of an aircraft by ICAO24 hex code.
        
        Args:
            icao24: Aircraft ICAO24 transponder code (e.g., "abc123")
            
        Returns:
            Tuple of (latitude, longitude, altitude_m, heading, speed_m_s) or None if not found
        """
        self._rate_limit()
        
        icao24_normalized = icao24.lower().strip()
        
        try:
            params = {"icao24": icao24_normalized}
            if self.auth:
                response = requests.get(self.BASE_URL, auth=self.auth, params=params, timeout=10)
            else:
                response = requests.get(self.BASE_URL, params=params, timeout=10)
            
            if response.status_code != 200:
                logger.warning(f"[OpenSky] API returned {response.status_code}")
                return None
            
            data = response.json()
            states = data.get("states", [])
            
            if not states:
                logger.debug(f"[OpenSky] ICAO24 {icao24} not found")
                return None
            
            state = states[0]
            lat = state[6]
            lon = state[5]
            alt = state[7] or state[13]
            heading = state[10]
            speed = state[9]
            
            if lat is not None and lon is not None:
                logger.info(f"[OpenSky] Found {icao24}: ({lat:.4f}, {lon:.4f}) alt={alt}m")
                return (lat, lon, alt or 0, heading or 0, speed or 0)
            
            return None
            
        except Exception as e:
            logger.warning(f"[OpenSky] Error fetching {icao24}: {e}")
            return None


# Global instance
_opensky_client: Optional[OpenSkyClient] = None


def get_opensky_client() -> OpenSkyClient:
    """Get or create the global OpenSky client instance."""
    global _opensky_client
    if _opensky_client is None:
        import os
        username = os.getenv("OPENSKY_USERNAME")
        password = os.getenv("OPENSKY_PASSWORD")
        _opensky_client = OpenSkyClient(username, password)
    return _opensky_client
