"""One place to call Claude, with retries on transient failures.

Both extraction and plan generation go through create_message so a blip
(overload, rate limit, timeout, dropped connection) retries with backoff instead
of failing the whole ingest. Anything non-transient is surfaced as a MiseError.
"""

import time

from anthropic import Anthropic

from .errors import MiseError

try:  # exception classes have moved around across SDK versions
    from anthropic import (
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
    )

    _RETRYABLE: tuple = (
        RateLimitError,
        APITimeoutError,
        APIConnectionError,
        InternalServerError,
    )
except Exception:  # pragma: no cover - depends on installed version
    _RETRYABLE = ()


def is_retryable(exc: Exception) -> bool:
    return bool(_RETRYABLE) and isinstance(exc, _RETRYABLE)


def create_message(api_key: str, *, max_retries: int = 3, **kwargs):
    """Call client.messages.create with retry/backoff on transient errors."""
    client = Anthropic(api_key=api_key)
    delay = 1.5
    for attempt in range(1, max_retries + 1):
        try:
            return client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if is_retryable(exc) and attempt < max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise MiseError(f"Claude request failed: {exc}")
