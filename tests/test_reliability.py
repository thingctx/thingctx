"""HttpInvoker reliability + chaos.

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

import thingctx.reliability as reliability
from thingctx import HttpInvoker, TransportError
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
    inv = HttpInvoker(backoff=0)
    action, form = _af("PUT")

    result = await inv.invoke(action, form, {})

    assert result == {"ok": True}
    assert routed["calls"] == 3
    await inv.aclose()


async def test_transient_failures_exhaust_to_transport_error(routed):
    routed["responses"] = [httpx.Response(503)] * 3
    inv = HttpInvoker(backoff=0)
    action, form = _af("PUT")

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert ei.value.status == 503
    assert ei.value.attempts == 3
    assert ei.value.as_dict()["ok"] is False
    await inv.aclose()


async def test_non_retryable_4xx_raises_immediately(routed):
    routed["responses"] = [httpx.Response(404, text="nope")]
    inv = HttpInvoker(backoff=0)
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
    inv = HttpInvoker(backoff=0)
    action, form = _af("GET")

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert ei.value.status is None
    assert isinstance(ei.value.__cause__, httpx.ConnectError)
    assert routed["calls"] == 3
    await inv.aclose()


async def test_client_is_pooled_across_calls(routed):
    routed["responses"] = [httpx.Response(200, json={"n": 1}), httpx.Response(200, json={"n": 2})]
    inv = HttpInvoker(backoff=0)
    action, form = _af("GET")

    await inv.invoke(action, form, {})
    await inv.invoke(action, form, {})

    assert routed["clients"] == 1
    await inv.aclose()


async def test_context_manager_closes(routed):
    routed["responses"] = [httpx.Response(200, json={"ok": True})]
    async with HttpInvoker(backoff=0) as inv:
        action, form = _af("GET")
        await inv.invoke(action, form, {})
        client = inv._client
        assert client is not None
    assert client.is_closed


# --- idempotency guard -----------------------------------------------------


async def test_post_is_not_retried_by_default(routed):
    routed["responses"] = [httpx.Response(503), httpx.Response(200, json={"ok": True})]
    inv = HttpInvoker(backoff=0)
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
    inv = HttpInvoker(backoff=0, retry_non_idempotent=True)
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
        inv = HttpInvoker(backoff=0, retries=retries)
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
    inv = HttpInvoker(backoff=0, retries=2)
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
    inv = HttpInvoker(backoff=0)

    async def call():
        action, form = _af("GET")
        return await inv.invoke(action, form, {})

    results = await asyncio.gather(*(call() for _ in range(n)))

    assert all(r == {"ok": True} for r in results)
    assert routed["calls"] == n
    assert routed["clients"] == 1  # one client served all concurrent calls
    await inv.aclose()
