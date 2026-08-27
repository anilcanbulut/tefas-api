from tefas.ratelimit import OBSERVED_LIMIT, SlidingWindowLimiter, backoff_delay


class FakeClock:
    """Virtual time, so throttling tests run instantly instead of in minutes."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_limiter(rate=4, window=60.0):
    clock = FakeClock()
    return SlidingWindowLimiter(rate=rate, window=window, clock=clock,
                                sleeper=clock.sleep), clock


def test_requests_inside_the_allowance_do_not_wait():
    limiter, _ = make_limiter(rate=4)
    assert [limiter.acquire() for _ in range(4)] == [0.0] * 4


def test_the_request_over_the_allowance_waits_for_the_window():
    limiter, clock = make_limiter(rate=4, window=60.0)
    for _ in range(4):
        limiter.acquire()
    waited = limiter.acquire()
    assert waited >= 60.0
    assert clock.now >= 60.0


def test_allowance_returns_gradually_as_the_window_slides():
    limiter, clock = make_limiter(rate=4, window=60.0)
    for _ in range(4):
        clock.sleep(10)
        limiter.acquire()
    # The oldest hit was 40s ago, so the next slot frees 20s later, not 60.
    waited = limiter.acquire()
    assert 0 < waited <= 30


def test_default_rate_stays_under_the_observed_server_limit():
    from tefas.ratelimit import DEFAULT_RATE

    assert DEFAULT_RATE < OBSERVED_LIMIT


def test_penalise_stops_traffic_until_the_window_rolls():
    limiter, clock = make_limiter(rate=4, window=60.0)
    limiter.acquire()
    limiter.penalise()
    waited = limiter.acquire()
    assert waited >= 60.0


def test_backoff_starts_at_window_length_not_one_second():
    """Retrying a 429 sooner than the window cannot succeed, so the first
    backoff must be tens of seconds, not the usual one."""
    delay = backoff_delay(0, rng=lambda: 0.5)
    assert delay == 60.0


def test_backoff_grows_and_is_capped():
    delays = [backoff_delay(n, rng=lambda: 0.5) for n in range(6)]
    assert delays == sorted(delays)
    assert max(delays) <= 300.0


def test_backoff_jitter_spreads_concurrent_workers():
    low = backoff_delay(0, rng=lambda: 0.0)
    high = backoff_delay(0, rng=lambda: 1.0)
    assert low < 60.0 < high
