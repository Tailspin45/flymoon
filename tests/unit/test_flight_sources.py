"""Pinning tests for the _SourceBackoff state machine in src.flight_sources.

Focuses on the backoff arithmetic + status surface. HTTP-level tests
(dedup-newer-wins, wall-clock timeout enforcement) are deferred to land
alongside roadmap §2.4 where they require a fake HTTP harness.
"""

import logging

from src import opensky
from src import flight_sources
from src.flight_sources import _SourceBackoff


def _freeze_time(monkeypatch, t):
    monkeypatch.setattr("src.flight_sources.time.time", lambda: t)


def test_backoff_schedule_exponential_with_cap(monkeypatch):
    """on_timeout must produce 60, 120, 240, 480, 960, 1920, 3600, 3600, 3600, 3600."""
    base = 100_000.0
    _freeze_time(monkeypatch, base)

    bo = _SourceBackoff("test")
    expected = [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600, 3600]
    observed = []
    for _ in range(10):
        bo.on_timeout()
        observed.append(bo._until - base)

    assert observed == expected
    assert bo._streak == 10


def test_on_success_resets_streak(monkeypatch):
    """A single on_success() must clear streak so the next timeout is 60s again."""
    base = 200_000.0
    _freeze_time(monkeypatch, base)

    bo = _SourceBackoff("test")
    for _ in range(5):
        bo.on_timeout()
    assert bo._streak == 5
    assert bo._until > base

    bo.on_success()
    assert bo._streak == 0
    assert bo._until == 0.0

    bo.on_timeout()
    assert (bo._until - base) == 60
    assert bo._streak == 1


def test_on_rate_limit_uses_fixed_duration_and_resets_streak(monkeypatch):
    """on_rate_limit must apply the fixed duration and zero the streak."""
    base = 300_000.0
    _freeze_time(monkeypatch, base)

    bo = _SourceBackoff("test")
    for _ in range(3):
        bo.on_timeout()
    assert bo._streak == 3

    bo.on_rate_limit(300)
    assert bo._streak == 0
    assert (bo._until - base) == 300


def test_in_backoff_false_when_deadline_past(monkeypatch):
    """A deadline in the past must not register as active backoff."""
    base = 400_000.0
    _freeze_time(monkeypatch, base)
    bo = _SourceBackoff("test")
    bo._until = base - 1
    assert bo.in_backoff() is False


def test_status_dict_shape(monkeypatch):
    """status() must return a dict with the documented keys and types."""
    base = 500_000.0
    _freeze_time(monkeypatch, base)

    bo = _SourceBackoff("test")
    bo.on_timeout()

    status = bo.status()
    assert set(status.keys()) == {"in_backoff", "backoff_remaining", "streak"}
    assert isinstance(status["in_backoff"], bool)
    assert isinstance(status["backoff_remaining"], int)
    assert isinstance(status["streak"], int)
    assert status["in_backoff"] is True
    assert status["streak"] == 1
    assert status["backoff_remaining"] == 60


def test_sources_down_warns_and_notifies_once_after_90s(monkeypatch, caplog):
    """Sustained all-required-sources backoff must emit exactly one signal."""
    now = {"t": 1_000_000.0}

    def _fake_time():
        return now["t"]

    monkeypatch.setattr("src.flight_sources.time.time", _fake_time)
    monkeypatch.setattr("src.opensky.time.time", _fake_time)

    sent = []

    async def _fake_send(msg):
        sent.append(msg)
        return True

    monkeypatch.setattr("src.telegram_notify.send_telegram_simple", _fake_send)

    # Put every required free source into backoff long enough to cover test time.
    future = now["t"] + 600
    flight_sources._bo_adsb_one._until = future
    flight_sources._bo_adsb_lol._until = future
    flight_sources._bo_adsb_fi._until = future
    opensky._backoff_until = future

    caplog.set_level(logging.WARNING)

    # t+0: starts outage timer, no alert yet.
    flight_sources.fetch_multi_source_positions(0.0, 0.0, 1.0, 1.0)
    assert sent == []

    # t+89: still below grace window, still no alert.
    now["t"] += 89
    flight_sources.fetch_multi_source_positions(0.0, 0.0, 1.0, 1.0)
    assert sent == []

    # t+91: first eligible emission.
    now["t"] += 2
    flight_sources.fetch_multi_source_positions(0.0, 0.0, 1.0, 1.0)

    # t+151: still down, must not flap.
    now["t"] += 60
    flight_sources.fetch_multi_source_positions(0.0, 0.0, 1.0, 1.0)

    warn_hits = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "SOURCES_DOWN" in r.getMessage()
    ]
    assert len(warn_hits) == 1
    assert len(sent) == 1
