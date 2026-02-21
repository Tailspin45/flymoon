"""
Flight data caching module to reduce FlightAware API calls.

Caches flight search results for a configurable duration (default: 120 seconds).
This prevents redundant API calls when users refresh multiple times in quick succession.
"""

import time
from typing import Optional, Dict, List
from src import logger


class FlightDataCache:
    """Simple in-memory cache for flight data with TTL support."""

    # Maximum number of cache entries before forced cleanup
    MAX_CACHE_SIZE = 100

    def __init__(self, ttl_seconds: int = 120):
        """
        Initialize cache with time-to-live.

        Parameters
        ----------
        ttl_seconds : int
            How long cached data remains valid (default: 120 seconds)
        """
        self.ttl = ttl_seconds
        self._cache: Dict[str, dict] = {}
        self._stats = {"hits": 0, "misses": 0, "evictions": 0}

    def _make_key(self, bbox: tuple) -> str:
        """Generate cache key from bounding box."""
        return f"{bbox[0]:.4f},{bbox[1]:.4f},{bbox[2]:.4f},{bbox[3]:.4f}"

    def get(self, lat_ll: float, lon_ll: float, lat_ur: float, lon_ur: float) -> Optional[List[dict]]:
        """
        Retrieve cached flight data if still valid.

        Parameters
        ----------
        lat_ll, lon_ll : float
            Lower-left corner of bounding box
        lat_ur, lon_ur : float
            Upper-right corner of bounding box

        Returns
        -------
        List[dict] or None
            Cached flight data if valid, None if cache miss
        """
        key = self._make_key((lat_ll, lon_ll, lat_ur, lon_ur))
        
        if key not in self._cache:
            self._stats["misses"] += 1
            return None
        
        entry = self._cache[key]
        age = time.time() - entry["timestamp"]
        
        if age > self.ttl:
            # Expired - evict and return None
            del self._cache[key]
            self._stats["evictions"] += 1
            self._stats["misses"] += 1
            logger.debug(f"Cache expired for {key} (age: {age:.1f}s)")
            return None
        
        self._stats["hits"] += 1
        logger.info(f"Cache HIT for {key} (age: {age:.1f}s, ttl: {self.ttl}s)")
        return entry["data"]

    def _cleanup_expired(self) -> None:
        """Remove all expired entries from cache."""
        now = time.time()
        expired_keys = [
            key for key, entry in self._cache.items()
            if now - entry["timestamp"] > self.ttl
        ]
        for key in expired_keys:
            del self._cache[key]
            self._stats["evictions"] += 1
        if expired_keys:
            logger.debug(f"Cleaned up {len(expired_keys)} expired cache entries")

    def set(self, lat_ll: float, lon_ll: float, lat_ur: float, lon_ur: float,
            data: List[dict]) -> None:
        """
        Store flight data in cache.

        Parameters
        ----------
        lat_ll, lon_ll : float
            Lower-left corner of bounding box
        lat_ur, lon_ur : float
            Upper-right corner of bounding box
        data : List[dict]
            Flight data to cache
        """
        # Cleanup expired entries periodically to prevent memory leak
        if len(self._cache) >= self.MAX_CACHE_SIZE:
            self._cleanup_expired()

        key = self._make_key((lat_ll, lon_ll, lat_ur, lon_ur))
        self._cache[key] = {
            "data": data,
            "timestamp": time.time()
        }
        logger.debug(f"Cached flight data for {key}")

    def clear(self) -> None:
        """Clear all cached data."""
        self._cache.clear()
        logger.info("Flight cache cleared")

    def get_stats(self) -> dict:
        """Return cache statistics."""
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = (self._stats["hits"] / total * 100) if total > 0 else 0
        
        return {
            **self._stats,
            "total_requests": total,
            "hit_rate_percent": round(hit_rate, 1),
            "cache_size": len(self._cache)
        }


# Global cache instance - shared across all requests
_flight_cache = FlightDataCache(ttl_seconds=600)  # 10 minutes cache


def get_cache() -> FlightDataCache:
    """Get the global flight cache instance."""
    return _flight_cache
