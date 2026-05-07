"""Per-session sliding-window rate limiter.

Lightweight in-memory implementation: keeps a `deque` of recent timestamps per
session and rejects when the window count exceeds the limit. Single-process.
For multi-instance deployment, swap with a Redis-backed implementation behind
the same `try_acquire` signature.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from time import monotonic


class RateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._events: deque[float] = deque()

    def try_acquire(self) -> bool:
        now = self._clock()
        cutoff = now - self._window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        if len(self._events) >= self._limit:
            return False
        self._events.append(now)
        return True
