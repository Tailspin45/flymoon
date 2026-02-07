import os
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

# Test data
TEST_DATA_PATH = "data/raw_flight_data_example.json"
POSSIBLE_TRANSITS_LOGFILENAME = "data/possible-transits/log_{date_}.csv"

# Astro data
ASTRO_EPHEMERIS = load("de421.bsp")
"""
The load function is used to load astronomical data, such as planetary ephemerides,
which are needed to calculate positions of celestial bodies.

This code loads the DE421 planetary ephemeris data from the Jet Propulsion Laboratory.
"""
EARTH_TIMESCALE = load.timescale()


# Window time
# 60 * top_min = 900 datapoints for each flight
TOP_MINUTE = 15
INTERVAL_IN_SECS = 1


# Transit
class Altitude(Enum):
    LOW = lambda x: x <= 15  # less or equal
    MEDIUM = lambda x: x <= 30  # less or equal
    MEDIUM_HIGH = lambda x: x <= 60  # less or equal
    HIGH = lambda x: x > 60  # greater than


class PossibilityLevel(Enum):
    UNLIKELY = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
