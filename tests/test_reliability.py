# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""HttpBinding reliability + chaos.

Two layers of testing here:

* deterministic logic tests (retry control flow, error normalization, pooling);
* chaos tests that inject randomized and timing-sensitive faults (random
  failure depth, backoff/Retry-After timing, every httpx timeout type, and
  concurrent load on the shared client).

Retries are gated to idempotent methods by default, so a POST is never silently
re-sent; that boundary is tested explicitly.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import httpx
import pytest

from thingctx import HttpBinding, TransportError, reliability
from thingctx.reliability import RetryPolicy, send_with_retry


def _af(method="GET", href="https://api.local/do"):
    action = SimpleNamespace(thing_id=None, idempotent=method.upper() in ("GET", "HEAD"))
    form = SimpleNamespace(href=href, raw={"htv:methodName": method})
    return action, form


@pytest.fixture
def routed(monkeypatch):
    """Drive every AsyncClient through a handler the test controls, and count
    requests and how many underlying clients get built (to prove pooling)."""
    state = {"responses": [], "calls": 0, "clients": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        nxt = state["responses"].pop(0)
        return nxt(request) if callable(nxt) else nxt

    real = httpx.AsyncClient

    def fake(*args, **kwargs):
        state["clients"] += 1
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake)
    return state


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace the backoff sleep with a recorder, so timing is asserted without
    real delays."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(reliability.asyncio, "sleep", fake_sleep)
    return slept


# --- deterministic logic ---------------------------------------------------


async def test_retries_then_succeeds(routed):
    routed["responses"] = [
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, json={"ok": True}),
    ]
    inv = HttpBinding(backoff=0)
    action, form = _af("PUT")

    result = await inv.invoke(action, form, {})

    assert result == {"ok": True}
    assert routed["calls"] == 3
    await inv.aclose()


async def test_transient_failures_exhaust_to_transport_error(routed):
    routed["responses"] = [httpx.Response(503)] * 3
    inv = HttpBinding(backoff=0)
    action, form = _af("PUT")

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert ei.value.status == 503
    assert ei.value.attempts == 3
    assert ei.value.as_dict()["ok"] is False
    await inv.aclose()


async def test_non_retryable_4xx_raises_immediately(routed):
    routed["responses"] = [httpx.Response(404, text="nope")]
    inv = HttpBinding(backoff=0)
    action, form = _af("GET")

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert ei.value.status == 404
    assert ei.value.attempts == 1
    assert routed["calls"] == 1
    await inv.aclose()


async def test_connection_error_is_retried_then_normalized(routed):
    def boom(_request):
        raise httpx.ConnectError("refused")

    routed["responses"] = [boom, boom, boom]
    inv = HttpBinding(backoff=0)
    action, form = _af("GET")

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert ei.value.status is None
    assert isinstance(ei.value.__cause__, httpx.ConnectError)
    assert routed["calls"] == 3
    await inv.aclose()


async def test_client_is_pooled_across_calls(routed):
    routed["responses"] = [httpx.Response(200, json={"n": 1}), httpx.Response(200, json={"n": 2})]
    inv = HttpBinding(backoff=0)
    action, form = _af("GET")

    await inv.invoke(action, form, {})
    await inv.invoke(action, form, {})

    assert routed["clients"] == 1
    await inv.aclose()


async def test_context_manager_closes(routed):
    routed["responses"] = [httpx.Response(200, json={"ok": True})]
    async with HttpBinding(backoff=0) as inv:
        action, form = _af("GET")
        await inv.invoke(action, form, {})
        client = inv._client
        assert client is not None
    assert client.is_closed


# --- idempotency guard -----------------------------------------------------


async def test_post_is_not_retried_by_default(routed):
    routed["responses"] = [httpx.Response(503), httpx.Response(200, json={"ok": True})]
    inv = HttpBinding(backoff=0)
    action, form = _af("POST")

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert ei.value.attempts == 1  # the write was sent once, not retried
    assert routed["calls"] == 1
    await inv.aclose()


async def test_post_retried_when_opted_in(routed):
    routed["responses"] = [
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, json={"ok": True}),
    ]
    inv = HttpBinding(backoff=0, retry_non_idempotent=True)
    action, form = _af("POST")

    result = await inv.invoke(action, form, {})

    assert result == {"ok": True}
    assert routed["calls"] == 3
    await inv.aclose()


# --- chaos: randomized fault depth -----------------------------------------


async def test_randomized_fault_depth(routed, no_sleep):
    """For a random number of leading failures, an idempotent call succeeds iff
    the failure count fits the retry budget, with exactly failures+1 attempts."""
    rng = random.Random(20260619)
    retries = 3
    for _ in range(120):
        fails = rng.randint(0, retries + 1)
        ok = httpx.Response(200, json={"ok": True})
        routed["responses"] = [httpx.Response(503) for _ in range(fails)] + [ok]
        routed["calls"] = 0
        inv = HttpBinding(backoff=0, retries=retries)
        action, form = _af("GET")
        if fails <= retries:
            assert await inv.invoke(action, form, {}) == {"ok": True}
            assert routed["calls"] == fails + 1
        else:
            with pytest.raises(TransportError):
                await inv.invoke(action, form, {})
            assert routed["calls"] == retries + 1
        await inv.aclose()


# --- chaos: backoff + Retry-After timing -----------------------------------


def test_backoff_schedule_is_exponential_and_capped():
    p = RetryPolicy(retries=5, backoff=0.1, jitter=0.0, max_backoff=1.0)
    assert p.delay(0) == pytest.approx(0.1)
    assert p.delay(1) == pytest.approx(0.2)
    assert p.delay(2) == pytest.approx(0.4)
    assert p.delay(10) == pytest.approx(1.0)  # capped at max_backoff


def test_jitter_stays_in_bounds():
    p = RetryPolicy(backoff=0.1, jitter=0.5)
    for _ in range(200):
        d = p.delay(0)
        assert 0.1 <= d <= 0.6


class _FakeClient:
    """A minimal stand-in whose .request replays a scripted sequence (responses
    or exceptions). Enough for send_with_retry, which only reads status/headers."""

    def __init__(self, sequence):
        self._seq = list(sequence)
        self.calls = 0

    async def request(self, method, url, **kwargs):
        self.calls += 1
        nxt = self._seq.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


async def test_sleeps_follow_exponential_backoff(no_sleep):
    client = _FakeClient([httpx.Response(503)] * 3 + [httpx.Response(200)])
    policy = RetryPolicy(retries=3, backoff=0.1, jitter=0.0)

    resp, attempts = await send_with_retry(client, "GET", "https://x/", policy=policy)

    assert resp.status_code == 200
    assert attempts == 4
    assert no_sleep == pytest.approx([0.1, 0.2, 0.4])


async def test_retry_after_header_is_honored(no_sleep):
    client = _FakeClient([httpx.Response(503, headers={"Retry-After": "2"}), httpx.Response(200)])
    policy = RetryPolicy(retries=3, backoff=0.1, jitter=0.0, max_backoff=5.0)

    await send_with_retry(client, "GET", "https://x/", policy=policy)

    assert no_sleep[0] == 2.0  # waited the server's Retry-After, not the backoff


# --- chaos: every timeout type ---------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("c"),
        httpx.ConnectTimeout("ct"),
        httpx.ReadTimeout("rt"),
        httpx.PoolTimeout("pt"),
        httpx.WriteError("we"),
    ],
)
async def test_all_transport_errors_are_retried_and_normalized(routed, no_sleep, exc):
    def boom(_request):
        raise exc

    routed["responses"] = [boom, boom, boom]
    inv = HttpBinding(backoff=0, retries=2)
    action, form = _af("GET")

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert type(ei.value.__cause__) is type(exc)
    assert routed["calls"] == 3
    await inv.aclose()


# --- chaos: concurrent load on the pooled client ---------------------------


async def test_concurrent_calls_share_one_pooled_client(routed):
    import asyncio

    n = 40
    routed["responses"] = [httpx.Response(200, json={"ok": True}) for _ in range(n)]
    inv = HttpBinding(backoff=0)

    async def call():
        action, form = _af("GET")
        return await inv.invoke(action, form, {})

    results = await asyncio.gather(*(call() for _ in range(n)))

    assert all(r == {"ok": True} for r in results)
    assert routed["calls"] == n
    assert routed["clients"] == 1  # one client served all concurrent calls
    await inv.aclose()


# --- issue #98: send_with_retry's own transport-error arm + retry_after -----
#
# The response-only scripts above drive send_with_retry through HttpBinding, so
# send_with_retry's *own* transport-error retry/raise arms (reliability.py
# 133-137) and retry_after's non-response / non-numeric branches (101-108) were
# never exercised by a direct call. These table-driven cases inject the clock
# via ``no_sleep`` and a scripted ``_FakeClient`` so every retry decision is
# asserted deterministically. Test-only — no production code changes.


async def test_send_with_retry_exhausts_transport_errors_then_raises(no_sleep):
    """Backoff-exhaustion branch: when every attempt raises at the transport
    level, the loop retries exactly ``policy.retries`` times, then raises after
    ``retries + 1`` attempts (not one more), propagating the LAST exception."""
    errors = [httpx.ConnectError(f"boom-{i}") for i in range(4)]
    client = _FakeClient(list(errors))
    policy = RetryPolicy(retries=3, backoff=0.1, jitter=0.0)

    with pytest.raises(TransportError) as ei:
        await send_with_retry(client, "GET", "https://x/", policy=policy)

    assert ei.value.attempts == 4  # retries + 1, never retries + 2
    assert ei.value.status is None  # a transport failure has no status
    assert client.calls == 4
    assert ei.value.__cause__ is errors[3]  # the final failure is the one raised
    assert no_sleep == pytest.approx([0.1, 0.2, 0.4])  # slept only between retries


async def test_send_with_retry_recovers_after_transport_error(no_sleep):
    """Retry-after-partial-failure: a transient transport error that clears on
    retry returns the success response and reports the true attempt count."""
    ok = httpx.Response(200, json={"ok": True})
    client = _FakeClient([httpx.ConnectError("transient"), ok])
    policy = RetryPolicy(retries=3, backoff=0.1, jitter=0.0)

    resp, attempts = await send_with_retry(client, "GET", "https://x/", policy=policy)

    assert resp is ok
    assert attempts == 2
    assert client.calls == 2
    assert no_sleep == pytest.approx([0.1])  # exactly one backoff between the tries


async def test_send_with_retry_returns_non_retryable_status_without_spending_budget(no_sleep):
    """Give-up path: a non-retryable status is returned on the first attempt;
    the remaining retry budget is left untouched and no backoff sleep happens."""
    resp404 = httpx.Response(404)
    client = _FakeClient([resp404])
    policy = RetryPolicy(retries=3, backoff=0.1, jitter=0.0)

    resp, attempts = await send_with_retry(client, "GET", "https://x/", policy=policy)

    assert resp is resp404
    assert attempts == 1
    assert client.calls == 1
    assert no_sleep == []  # short-circuited before any sleep


async def test_send_with_retry_transport_error_gives_up_on_zero_budget(no_sleep):
    """With ``retries=0`` a single transport failure short-circuits straight to
    a raise — the give-up arm is reached without any sleep."""
    client = _FakeClient([httpx.ConnectTimeout("no-budget")])
    policy = RetryPolicy(retries=0, backoff=0.1, jitter=0.0)

    with pytest.raises(TransportError) as ei:
        await send_with_retry(client, "GET", "https://x/", policy=policy)

    assert ei.value.attempts == 1
    assert client.calls == 1
    assert no_sleep == []  # zero budget -> never slept


@pytest.mark.parametrize(
    ("resp", "attempt", "expected"),
    [
        # retry_after's `resp is None` arm falls back to the backoff schedule.
        (None, 0, 0.1),
        (None, 2, 0.4),
        # A response without a Retry-After header also falls back to backoff.
        (httpx.Response(503), 1, 0.2),
        # A non-numeric Retry-After is ignored and falls back to backoff.
        (httpx.Response(503, headers={"Retry-After": "soon"}), 0, 0.1),
        # A sane numeric Retry-After is honored verbatim...
        (httpx.Response(503, headers={"Retry-After": "2"}), 0, 2.0),
        # ...but still capped at policy.max_backoff.
        (httpx.Response(503, headers={"Retry-After": "999"}), 0, 5.0),
    ],
)
def test_retry_after_response_and_fallback_branches(resp, attempt, expected):
    policy = RetryPolicy(retries=5, backoff=0.1, jitter=0.0, max_backoff=5.0)
    assert reliability.retry_after(resp, policy, attempt) == pytest.approx(expected)
