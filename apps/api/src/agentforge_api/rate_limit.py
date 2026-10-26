"""Bounded in-memory token buckets for authenticated API principals."""

from __future__ import annotations

import math
import time
import threading
from collections import OrderedDict
from collections.abc import Callable


class StreamConcurrencyLimiter:
    """Bound active SSE streams globally and per authenticated principal."""

    def __init__(self, *, global_limit: int = 100, per_principal_limit: int = 5) -> None:
        if global_limit <= 0 or per_principal_limit <= 0:
            raise ValueError("stream limits must be positive")
        self.global_limit = global_limit
        self.per_principal_limit = per_principal_limit
        self._active: dict[str, int] = {}
        self._lock = threading.Lock()

    def try_acquire(self, principal_id: str) -> bool:
        with self._lock:
            total = sum(self._active.values())
            current = self._active.get(principal_id, 0)
            if total >= self.global_limit or current >= self.per_principal_limit:
                return False
            self._active[principal_id] = current + 1
            return True

    def release(self, principal_id: str) -> None:
        with self._lock:
            current = self._active.get(principal_id, 0)
            if current <= 1:
                self._active.pop(principal_id, None)
            else:
                self._active[principal_id] = current - 1


class TokenBucketLimiter:
    """A single-process limiter; callers should use authenticated principal IDs."""

    def __init__(
        self,
        *,
        rate: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 10_000,
        idle_timeout: float = 3600,
    ) -> None:
        if rate <= 0 or not math.isfinite(rate):
            raise ValueError("rate must be a finite positive number")
        if burst <= 0:
            raise ValueError("burst must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if idle_timeout <= 0 or not math.isfinite(idle_timeout):
            raise ValueError("idle_timeout must be a finite positive number")
        self.rate = float(rate)
        self.burst = int(burst)
        self.max_entries = int(max_entries)
        self.idle_timeout = float(idle_timeout)
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._buckets)

    def consume(self, principal_id: str) -> tuple[bool, int]:
        """Consume one token and return ``(allowed, retry_after_seconds)``."""
        now = self._clock()
        self._evict_idle(now)
        bucket = self._buckets.pop(principal_id, None)
        if bucket is None:
            if len(self._buckets) >= self.max_entries:
                retry_after = max(
                    1,
                    math.ceil(
                        min(
                            last_seen + self.idle_timeout - now
                            for _tokens, _updated_at, last_seen in self._buckets.values()
                        )
                    ),
                )
                return False, retry_after
            tokens = float(self.burst)
            updated_at = now
        else:
            tokens, updated_at, _last_seen = bucket
            tokens = min(self.burst, tokens + max(0.0, now - updated_at) * self.rate)

        if tokens >= 1:
            tokens -= 1
            self._buckets[principal_id] = (tokens, now, now)
            self._trim()
            return True, 0

        retry_after = max(1, math.ceil((1 - tokens) / self.rate))
        self._buckets[principal_id] = (tokens, now, now)
        self._trim()
        return False, retry_after

    def _evict_idle(self, now: float) -> None:
        expired = [
            key
            for key, (_tokens, _updated_at, last_seen) in self._buckets.items()
            if now - last_seen >= self.idle_timeout
        ]
        for key in expired:
            del self._buckets[key]

    def _trim(self) -> None:
        while len(self._buckets) > self.max_entries:
            self._buckets.popitem(last=False)
