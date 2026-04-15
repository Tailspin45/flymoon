"""Pinning tests for the _SourceBackoff state machine in src.flight_sources.

Focuses on the backoff arithmetic + status surface. HTTP-level tests
(all-sources-down signal, dedup-newer-wins, wall-clock timeout enforcement)
are deferred to land alongside roadmap §2.4 where they require a fake
HTTP harness.
"""

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
