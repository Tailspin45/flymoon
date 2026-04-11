import os
import shutil
from enum import Enum

from skyfield.api import load

# General
NUM_MINUTES_PER_HOUR = 60
NUM_SECONDS_PER_MIN = 60
EARTH_RADIOUS = 6371

# Notifications
TARGET_TO_EMOJI = {"moon": "🌙", "sun": "☀️", "both": "🌙☀️"}
MAX_NUM_ITEMS_TO_NOTIFY = 5

# Transit detection thresholds (configurable via .env)
# Assumes 1° target size for sun/moon (0.5° actual + 0.5° margin for near misses)
ALT_DIFF_THRESHOLD_TO_NOTIFY = float(os.getenv("ALT_THRESHOLD", "1.0"))
AZ_DIFF_THRESHOLD_TO_NOTIFY = float(os.getenv("AZ_THRESHOLD", "1.0"))

# Weather
WEATHER_CACHE_DURATION_MINUTES = 60
WEATHER_API_URL = "https://api.openweathermap.org/data/2.5/weather"
WEATHER_ICONS = {
    "clear": "☀️",
    "clouds": "☁️",
    "partly_cloudy": "⛅",
    "rain": "🌧️",
    "snow": "🌨️",
    "thunderstorm": "⛈️",
    "unknown": "❓",
}

# Flight data
API_URL = "https://aeroapi.flightaware.com/aeroapi/flights/search"
CHANGE_ELEVATION = {
    "C": "climbing",
    "D": "descending",
    "-": "level",
}


def get_aeroapi_key() -> str:
    return (
        os.getenv("AEROAPI_API_KEY")
        or os.getenv("AEROAPI_KEY")
        or os.getenv("FLIGHTAWARE_API_KEY")
        or ""
    )


def get_ffmpeg_path() -> str:
    """Return path to ffmpeg binary, or 'ffmpeg' if on PATH, or empty string."""
    env_path = os.getenv("FFMPEG_PATH", "")
    if env_path and os.path.isfile(env_path):
        return env_path
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")
    return ""


# Test data
TEST_DATA_PATH = "data/raw_flight_data_example.json"
POSSIBLE_TRANSITS_LOGFILENAME = "data/possible-transits/log_{date_}.csv"

# Transit event confirmation log — one row per live detection
TRANSIT_EVENTS_LOGFILENAME = "data/possible-transits/transit_events_{date_}.csv"
TRANSIT_EVENTS_FIELDS = [
    "timestamp",  # ISO-8601 UTC datetime of the detection
    "detected_flight_id",  # callsign matched by enrichment (or empty)
    "aircraft_type",  # from enrichment / same source as map table (or empty)
    "origin_country",  # ADS-B origin country when available (matches map subtitle)
    "predicted_flight_id",  # callsign from prediction (empty until T08/T09 cross-link)
    "prediction_sep_deg",  # best predicted angular separation (° or empty)
    "detection_confirmed",  # 1 = enrichment found a nearby aircraft, 0 = unconfirmed
    "confidence",  # 'strong' or 'weak' (detector signal-to-threshold ratio)
    "confidence_score",  # D3: numeric probability in [0,1] (sigmoid of SNR/ratio/track)
    "signal_a",  # raw Signal A value at detection time
    "signal_b",  # raw Signal B value at detection time (wavelet-detrended if pywt)
    "centre_ratio",  # inner/outer disc ratio at detection time
    "notes",  # free-text annotations (includes 'matched_filter' when D2 fires)
]

# Astro data
ASTRO_EPHEMERIS = load("de421.bsp")
"""
The load function is used to load astronomical data, such as planetary ephemerides,
which are needed to calculate positions of celestial bodies.

This code loads the DE421 planetary ephemeris data from the Jet Propulsion Laboratory.
"""
EARTH_TIMESCALE = load.timescale()


# Window time
# TOP_MINUTE * (60 / INTERVAL_IN_SECS) = datapoints for each flight
TOP_MINUTE = 15
INTERVAL_IN_SECS = 5  # 5-second sampling: 180 pts vs 900 — plenty for 1° threshold


# Transit
class Altitude(Enum):
    """Classify the celestial target's altitude in degrees above the horizon.

    Each value is a predicate called with the target's altitude in degrees
    (e.g. Sun/Moon elevation).  This is NOT aircraft elevation in metres.
    """

    LOW = lambda x: x <= 15  # less or equal
    MEDIUM = lambda x: x <= 30  # less or equal
    MEDIUM_HIGH = lambda x: x <= 60  # less or equal
    HIGH = lambda x: x > 60  # greater than


class PossibilityLevel(Enum):
    UNLIKELY = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
