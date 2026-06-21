"""Cheap reliability for HTTP invokers: bounded retries with exponential
backoff and jitter, plus one normalized error shape.

A flaky network or a momentarily overloaded device should not surface as a
raw, transport-specific exception. ``send_with_retry`` retries the transient
failures (connection errors, timeouts, 429, 5xx) a small number of times, then
raises a single ``TransportError`` carrying the method, URL, status, and how
many attempts were made.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

# Transient HTTP statuses worth retrying: request timeout, rate limit, and the
# server-side 5xx family that commonly clears on a second try.
DEFAULT_RETRY_STATUSES: tuple[int, ...] = (408, 429, 500, 502, 503, 504)

# Methods that are safe to auto-retry: re-sending them cannot change server
# state beyond a single application. POST/PATCH are excluded by default because
# a retry could double-submit a side effect.
IDEMPOTENT_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE"})


@dataclass
class RetryPolicy:
    """How many times to retry a transient failure and how long to wait."""

    retries: int = 2  # extra attempts after the first (so up to 3 total)
    backoff: float = 0.2  # base seconds, doubled each attempt
    max_backoff: float = 5.0
    jitter: float = 0.1  # random 0..jitter seconds added, to avoid thundering herd
    retry_statuses: tuple[int, ...] = DEFAULT_RETRY_STATUSES

    def delay(self, attempt: int) -> float:
        return min(self.backoff * (2**attempt), self.max_backoff) + random.uniform(0, self.jitter)


class TransportError(Exception):
    """A single error shape for transport failures after retries are spent.

    Carries enough to act on (``status``, ``attempts``) and ``as_dict()`` for
    callers that prefer to hand the model a structured error rather than raise.
    """

    def __init__(
        self,
        method: str,
        url: str,
        *,
        status: int | None = None,
        attempts: int = 1,
        detail: str = "",
        cause: BaseException | None = None,
    ) -> None:
        self.method = method
        self.url = url
        self.status = status
        self.attempts = attempts
        self.detail = detail
        where = str(status) if status is not None else (type(cause).__name__ if cause else "error")
        msg = f"{method} {url} failed after {attempts} attempt(s): {where}"
        if detail:
            msg = f"{msg} {detail}"
        super().__init__(msg.strip())
        if cause is not None:
            self.__cause__ = cause

    def as_dict(self) -> dict:
        return {
            "ok": False,
            "error": {
                "method": self.method,
                "url": self.url,
                "status": self.status,
                "attempts": self.attempts,
                "detail": self.detail,
            },
        }


def _retry_after(resp, policy: RetryPolicy, attempt: int) -> float:
    """Honor a numeric ``Retry-After`` header (429/503) if present and sane,
    otherwise fall back to the policy's backoff schedule."""
    if resp is not None:
        ra = resp.headers.get("retry-after", "")
        if ra.isdigit():
            return min(float(ra), policy.max_backoff)
    return policy.delay(attempt)


async def send_with_retry(
    client, method: str, url: str, *, policy: RetryPolicy, retries: int | None = None, **kwargs
):
    """Send a request, retrying transient failures per ``policy``. Returns
    ``(response, attempts)``; raises ``TransportError`` if every attempt fails
    at the transport level (the caller decides what to do with a bad status).

    ``retries`` overrides ``policy.retries`` for this call -- the invoker passes
    ``0`` for non-idempotent methods so a write is never silently re-sent."""
    import httpx

    max_retries = policy.retries if retries is None else retries
    for attempt in range(max_retries + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:  # connection + timeout errors
            if attempt < max_retries:
                await asyncio.sleep(policy.delay(attempt))
                continue
            raise TransportError(method, url, attempts=attempt + 1, cause=exc) from exc
        if resp.status_code in policy.retry_statuses and attempt < max_retries:
            await asyncio.sleep(_retry_after(resp, policy, attempt))
            continue
        return resp, attempt + 1
    raise AssertionError("unreachable")  # pragma: no cover
