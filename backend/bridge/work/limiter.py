"""A token bucket per provider, shared by every worker on this host.

Phase D's `LLM_MAX_RETRIES` handles one call that gets a 429. It does not handle
eight workers all backfilling descriptions against one provider account, which is
what phase F makes possible for the first time: each of them independently
retries, each retry adds load, and the provider's rate limit turns into a
thundering herd that makes the problem worse the harder everyone tries.

So: a bucket refills at a fixed rate, a call takes a token or waits, and — the
part that matters — **a 429 shrinks the bucket for everybody**, not just for the
caller that saw it. One worker discovering the limit slows down all of them,
which is the only way a shared quota can be respected by processes that cannot
see each other's traffic.

Deliberately in-process. Cross-host coordination would want the limiter in
Postgres alongside the queue, and that is the right shape when there is more than
one host; today `bridge worker` runs on one, and an in-process bucket that
actually works beats a distributed one that is mostly untested.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .. import config


@dataclass
class TokenBucket:
    """Classic token bucket, with a capacity that shrinks when the provider says so."""

    rate_per_second: float
    capacity: float
    tokens: float = field(init=False)
    updated_at: float = field(default_factory=time.monotonic)
    #: multiplier applied to the refill rate; 1.0 normally, lower after a 429
    throttle: float = 1.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated_at
        self.updated_at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_second * self.throttle)

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        """Block until ``tokens`` are available. False if ``timeout`` elapses first."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True
                # how long until enough tokens exist, at the current rate
                effective = max(1e-6, self.rate_per_second * self.throttle)
                wait = (tokens - self.tokens) / effective
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 0.5))

    def penalise(self, factor: float = 0.5, floor: float = 0.05) -> float:
        """A 429 arrived: slow everyone down, not just whoever saw it."""
        with self._lock:
            self.throttle = max(floor, self.throttle * factor)
            self.tokens = 0.0
            return self.throttle

    def recover(self, step: float = 1.25) -> float:
        """A success arrived: ease back toward full rate.

        Gradual on purpose. Snapping back to full speed after one good response
        is how a system oscillates between hammering a provider and being
        throttled by it.
        """
        with self._lock:
            self.throttle = min(1.0, self.throttle * step)
            return self.throttle


class ProviderLimiter:
    """One bucket per provider name, created on first use."""

    def __init__(self, rate_per_second: float | None = None, capacity: float | None = None) -> None:
        self.rate = rate_per_second if rate_per_second is not None else config.WORKER_RATE_PER_SECOND
        self.capacity = capacity if capacity is not None else config.WORKER_RATE_BURST
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def bucket(self, provider: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(provider)
            if bucket is None:
                bucket = TokenBucket(rate_per_second=self.rate, capacity=self.capacity)
                self._buckets[provider] = bucket
            return bucket

    def acquire(self, provider: str, timeout: float | None = None) -> bool:
        return self.bucket(provider).acquire(timeout=timeout)

    def on_response(self, provider: str, error: BaseException | None = None) -> None:
        """Feed the outcome back. A 429 (or a quota error) throttles; success recovers."""
        bucket = self.bucket(provider)
        if error is None:
            bucket.recover()
            return
        message = f"{error.__class__.__name__}: {error}".lower()
        if "429" in message or "resource_exhausted" in message or "rate" in message:
            bucket.penalise()

    def status(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {
                name: {"tokens": round(bucket.tokens, 2), "throttle": round(bucket.throttle, 3)}
                for name, bucket in sorted(self._buckets.items())
            }


#: The process-wide limiter. Workers share it; that sharing is the whole point.
LIMITER = ProviderLimiter()
