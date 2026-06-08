"""Thread-safe rate limiting helpers for source adapters."""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    """Simple thread-safe sliding-window limiter."""

    def __init__(self, max_requests: int, window_seconds: float = 60.0) -> None:
        self.max_requests = max(int(max_requests), 1)
        self.window_seconds = float(window_seconds)
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a request slot is available and return wait seconds used."""
        wait_seconds = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return wait_seconds

                wait_seconds = max(self.window_seconds - (now - self._timestamps[0]) + 0.01, 0.01)
            time.sleep(wait_seconds)

