"""Retry with capped exponential backoff and jitter.

Used at startup, where the database commonly comes up a few seconds after
the application under Docker Compose or Kubernetes. Jitter keeps replicas
that start together from retrying in lock-step.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


def backoff_delays(attempts: int, base: float, maximum: float) -> list[float]:
    """Delay before each retry, without jitter: base, 2*base, 4*base, ... capped at maximum."""
    return [min(base * 2**i, maximum) for i in range(attempts - 1)]


def _default_jitter(delay: float) -> float:
    return random.uniform(0, delay / 2)  # nosec B311 - timing jitter, not cryptography


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float], float] = _default_jitter,
) -> T:
    delays = backoff_delays(attempts, base_delay, max_delay)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == attempts:
                logger.error("giving up after %d attempts: %s", attempts, exc)
                raise
            wait = delays[attempt - 1] + jitter(delays[attempt - 1])
            logger.warning("attempt %d/%d failed (%s); retrying in %.2fs",
                           attempt, attempts, exc.__class__.__name__, wait)
            sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover
