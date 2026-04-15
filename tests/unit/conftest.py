"""Shared pytest fixtures for unit tests.

Ensures:
  * src.* imports work without the caller setting PYTHONPATH.
  * Module-level global state (filters, caches, backoff timers, singletons)
    cannot leak between tests.
"""

import sys
from pathlib import Path

# Make the repo root importable so tests can `from src import ...`
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_imm_filters():
    """Empty src.imm_kalman._filters around every test."""
    from src import imm_kalman

    imm_kalman._filters.clear()
    yield
    imm_kalman._filters.clear()


@pytest.fixture(autouse=True)
def _reset_opensky_cache():
    """Empty src.opensky._cache and clear backoff state around every test."""
    from src import opensky

    opensky._cache.clear()
    opensky._backoff_until = 0.0
    yield
    opensky._cache.clear()
    opensky._backoff_until = 0.0


@pytest.fixture(autouse=True)
def _reset_flight_source_backoffs():
    """Reset the five module-level _SourceBackoff instances around every test."""
    from src import flight_sources

    for bo in (
        flight_sources._bo_adsb_one,
        flight_sources._bo_adsb_lol,
        flight_sources._bo_adsb_fi,
        flight_sources._bo_adsbx,
        flight_sources._bo_local,
    ):
        bo._until = 0.0
        bo._streak = 0
    flight_sources._multi_source_cache.clear()
    flight_sources._multi_source_cache_ts.clear()
    flight_sources._all_sources_down_since = None
    flight_sources._all_sources_down_notified = False
    yield
    for bo in (
        flight_sources._bo_adsb_one,
        flight_sources._bo_adsb_lol,
        flight_sources._bo_adsb_fi,
        flight_sources._bo_adsbx,
        flight_sources._bo_local,
    ):
        bo._until = 0.0
        bo._streak = 0
    flight_sources._multi_source_cache.clear()
    flight_sources._multi_source_cache_ts.clear()
    flight_sources._all_sources_down_since = None
    flight_sources._all_sources_down_notified = False


@pytest.fixture(autouse=True)
def _reset_classifier_singleton():
    """Reset the transit_classifier module-level singleton around every test."""
    from src import transit_classifier

    transit_classifier._classifier = None
    yield
    transit_classifier._classifier = None
