"""Client-side throttling for the TEFAS gateway.

Measured behaviour (27 Aug 2026): five requests inside a rolling minute
succeed, the sixth returns ``HTTP 429`` immediately, and no ``Retry-After``
header is sent. The limit clears as soon as the window rolls -- no cooldown
penalty and no escalation were observed -- so the correct client behaviour is a
sliding-window throttle plus patient backoff, not a circuit breaker.
"""

import random
import threading
import time
from collections import deque

#: Requests per window the gateway actually allows before answering 429.
OBSERVED_LIMIT = 5

#: What this client defaults to. One below the ceiling, because the window is
#: shared with anything else calling TEFAS from the same address.
DEFAULT_RATE = 4

DEFAULT_WINDOW = 60.0

#: Backoff defaults. The base is a full window on purpose: a 429 means the
#: window is full, so retrying sooner cannot succeed.
DEFAULT_BACKOFF_BASE = 60.0
DEFAULT_BACKOFF_CAP = 300.0
DEFAULT_BACKOFF_JITTER = 0.25


class SlidingWindowLimiter:
    """Allow at most ``rate`` acquisitions per ``window`` seconds.

    Thread-safe: several threads may share one limiter and will queue behind
    each other rather than each keeping a private allowance.
    """

    def __init__(self, rate=DEFAULT_RATE, window=DEFAULT_WINDOW, clock=time.monotonic,
                 sleeper=time.sleep):
        if rate < 1:
            raise ValueError("rate must be at least 1")
        self.rate = rate
        self.window = window
        self._clock = clock
        self._sleep = sleeper
        self._hits = deque()
        self._lock = threading.Lock()

    def acquire(self):
        """Block until a request may be sent. Returns seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._evict(now)
                if len(self._hits) < self.rate:
                    self._hits.append(now)
                    return waited
                delay = self.window - (now - self._hits[0])
            # Sleep outside the lock so other threads can observe the window.
            delay = max(delay, 0.01)
            self._sleep(delay)
            waited += delay

    def _evict(self, now):
        cutoff = now - self.window
        while self._hits and self._hits[0] <= cutoff:
            self._hits.popleft()

    def penalise(self):
        """Record a 429 by filling the window.

        Called after the server rejects a request, so the limiter stops issuing
        until the window has fully rolled even if its own accounting disagreed
        with the gateway's.
        """
        with self._lock:
            now = self._clock()
            self._hits = deque([now] * self.rate)


def backoff_delay(attempt, base=DEFAULT_BACKOFF_BASE, cap=DEFAULT_BACKOFF_CAP,
                  jitter=DEFAULT_BACKOFF_JITTER, rng=random.random):
    """Seconds to wait before retry number ``attempt`` (0-indexed).

    Seeded at the length of the rate-limit window rather than the usual one
    second: a 429 here means the window is full, and retrying sooner cannot
    succeed. Jitter keeps concurrent workers from resynchronising.
    """
    delay = min(base * (2 ** attempt), cap)
    return delay * (1 - jitter + 2 * jitter * rng())
