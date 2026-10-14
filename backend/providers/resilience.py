"""Retry with exponential backoff + jitter, and a per-provider circuit breaker."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from backend.providers.base import ProviderError, ProviderUnavailable

T = TypeVar("T")


@dataclass
class CircuitBreaker:
    threshold: int = 5
    reset_after: float = 30.0
    failures: int = 0
    opened_at: float | None = None
    half_open_probe: bool = False
    clock: Callable[[], float] = field(default=time.monotonic)

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.reset_after:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self.half_open_probe:
            self.half_open_probe = True
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.half_open_probe = False

    def record_failure(self) -> None:
        self.failures += 1
        self.half_open_probe = False
        if self.failures >= self.threshold or self.opened_at is not None:
            self.opened_at = self.clock()


class BreakerRegistry:
    def __init__(self, threshold: int, reset_after: float) -> None:
        self.threshold = threshold
        self.reset_after = reset_after
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, key: str) -> CircuitBreaker:
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(threshold=self.threshold, reset_after=self.reset_after)
            self._breakers[key] = breaker
        return breaker

    def snapshot(self) -> dict[str, str]:
        return {key: b.state for key, b in self._breakers.items()}


def backoff_delay(attempt: int, base: float, cap: float, retry_after: float | None = None) -> float:
    if retry_after is not None:
        return min(cap, max(0.0, retry_after))
    exp = min(cap, base * (2 ** attempt))
    return exp / 2 + random.uniform(0, exp / 2)  # "equal jitter"


async def call_with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    breaker: CircuitBreaker,
    max_retries: int,
    base: float,
    cap: float,
    on_retry: Callable[[int, ProviderError, float], Awaitable[None]] | None = None,
) -> tuple[T, int]:
    """Run ``fn`` with retries. Returns (result, attempts). Partial-stream failures are not retried
    here: the caller decides (it must reset streamed output before trying another binding)."""
    attempt = 0
    while True:
        if not breaker.allow():
            raise ProviderUnavailable("circuit breaker open for this provider", retryable=False)
        try:
            result = await fn()
        except ProviderError as exc:
            if exc.retryable or exc.status is None:
                breaker.record_failure()
            if not exc.retryable or exc.partial or attempt >= max_retries:
                raise
            delay = backoff_delay(attempt, base, cap, exc.retry_after)
            if on_retry is not None:
                await on_retry(attempt + 1, exc, delay)
            await asyncio.sleep(delay)
            attempt += 1
            continue
        breaker.record_success()
        return result, attempt + 1
