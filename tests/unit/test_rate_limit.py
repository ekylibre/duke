from __future__ import annotations

import pytest

from duke.transport.rate_limit import RateLimiter


def test_allows_up_to_limit() -> None:
    clock = iter([0.1, 0.2, 0.3, 0.4])
    rl = RateLimiter(limit=3, window_seconds=60, clock=lambda: next(clock))
    assert rl.try_acquire() is True
    assert rl.try_acquire() is True
    assert rl.try_acquire() is True
    assert rl.try_acquire() is False  # 4th in same window


def test_window_slides() -> None:
    times = iter([0.0, 0.1, 0.2, 65.0, 65.1])  # 4th and 5th outside window
    rl = RateLimiter(limit=3, window_seconds=60, clock=lambda: next(times))
    assert rl.try_acquire()
    assert rl.try_acquire()
    assert rl.try_acquire()
    assert rl.try_acquire()  # window slid, oldest evicted
    assert rl.try_acquire()


def test_invalid_limit_rejected() -> None:
    with pytest.raises(ValueError):
        RateLimiter(limit=0)
