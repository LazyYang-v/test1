"""Per-principal token buckets and SSE concurrency bounds."""

import pytest

from agentforge_api.rate_limit import StreamConcurrencyLimiter, TokenBucketLimiter


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_token_bucket_limits_capacity_and_reports_retry_after():
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate=2, burst=2, clock=clock)

    assert limiter.consume("user-a") == (True, 0)
    assert limiter.consume("user-a") == (True, 0)
    assert limiter.consume("user-a") == (False, 1)

    clock.advance(0.5)
    assert limiter.consume("user-a") == (True, 0)
    assert limiter.consume("user-a") == (False, 1)


def test_token_bucket_refills_to_capacity_and_isolates_principals():
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate=1, burst=2, clock=clock)

    assert limiter.consume("user-a")[0]
    assert limiter.consume("user-a")[0]
    assert not limiter.consume("user-a")[0]
    assert limiter.consume("user-b") == (True, 0)

    clock.advance(10)
    assert limiter.consume("user-a") == (True, 0)
    assert limiter.consume("user-a") == (True, 0)
    assert not limiter.consume("user-a")[0]


def test_token_bucket_bounds_state_without_resetting_active_quotas():
    clock = FakeClock()
    limiter = TokenBucketLimiter(
        rate=1, burst=1, clock=clock, max_entries=2, idle_timeout=5
    )

    assert limiter.consume("user-a")[0]
    assert limiter.consume("user-b")[0]
    assert len(limiter) == 2

    # A new principal cannot evict an active principal and reset its quota.
    assert limiter.consume("user-c") == (False, 5)
    assert len(limiter) == 2
    assert limiter.consume("user-a") == (False, 1)

    clock.advance(6)
    assert limiter.consume("user-c")[0]
    assert len(limiter) == 1


@pytest.mark.parametrize(
    "settings",
    [
        {"rate": 0, "burst": 1},
        {"rate": 1, "burst": 0},
        {"rate": 1, "burst": 1, "max_entries": 0},
    ],
)
def test_token_bucket_rejects_invalid_limits(settings):
    with pytest.raises(ValueError):
        TokenBucketLimiter(**settings)


def test_sse_stream_limiter_enforces_global_and_per_principal_limits():
    limiter = StreamConcurrencyLimiter(global_limit=3, per_principal_limit=2)

    assert limiter.try_acquire("user-a")
    assert limiter.try_acquire("user-a")
    assert not limiter.try_acquire("user-a")
    assert limiter.try_acquire("user-b")
    assert not limiter.try_acquire("user-c")

    limiter.release("user-a")
    assert limiter.try_acquire("user-c")
    limiter.release("user-a")
    limiter.release("user-b")
    limiter.release("user-c")


def test_sse_stream_limiter_frees_principal_slots_on_release():
    limiter = StreamConcurrencyLimiter(global_limit=1, per_principal_limit=1)
    assert limiter.try_acquire("user-a")
    assert not limiter.try_acquire("user-b")
    limiter.release("user-a")
    assert limiter.try_acquire("user-b")
