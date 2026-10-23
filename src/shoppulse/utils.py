"""Small reusable helpers: retry with backoff, row hashing, timestamps."""

from __future__ import annotations

import functools
import hashlib
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

log = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])


def retry(
    attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] | None = None,
) -> Callable[[F], F]:
    """Retry a flaky call (network, API) with exponential backoff.

    Only the exceptions listed are retried; anything else fails immediately.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            wait = delay
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == attempts:
                        log.error("%s failed after %d attempts: %s", func.__name__, attempts, exc)
                        raise
                    log.warning(
                        "%s attempt %d/%d failed (%s); retrying in %.1fs",
                        func.__name__,
                        attempt,
                        attempts,
                        exc,
                        wait,
                    )
                    (sleep or time.sleep)(wait)  # resolved at call time -> easy to patch in tests
                    wait *= backoff

        return wrapper  # type: ignore[return-value]

    return decorator


def row_hash(*values: Any) -> str:
    """Stable fingerprint of the tracked attributes of a row (used for SCD2 change detection)."""
    def norm(v: Any) -> str:
        if v is None or v != v:  # None or NaN
            return ""
        if isinstance(v, float) and v.is_integer():
            return str(int(v))  # 3.0 and 3 must hash the same (CSV round-trips change dtypes)
        return str(v).strip()

    text = "|".join(norm(v) for v in values)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def today_iso() -> str:
    return datetime.now().date().isoformat()
