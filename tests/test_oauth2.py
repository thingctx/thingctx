"""OAuth2 client-credentials: fetch a token, cache it, attach it as bearer."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from thingctx import HttpInvoker, parse_thing

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:dev:svc",
    "title": "svc",
    "securityDefinitions": {
        "oauth": {
            "scheme": "oauth2",
            "flow": "client_credentials",
            "token": "https://auth.local/token",
            "scopes": ["read"],
        }
    },
    "security": ["oauth"],
    "actions": {"do": {"forms": [{"href": "https://api.local/api/do", "htv:methodName": "POST"}]}},
}


@pytest.fixture
def mock_http(monkeypatch):
    """Route every httpx.AsyncClient through one mock transport that issues a
    token at the token endpoint and echoes the auth header at the API."""
    state = {"token_calls": 0, "seen_auth": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.local":
            state["token_calls"] += 1
            body = request.content.decode()
            assert "grant_type=client_credentials" in body
            assert "client_id=cid" in body
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
        state["seen_auth"].append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient

    def fake(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake)
    return state


def _action_form():
    action = SimpleNamespace(thing_id="urn:dev:svc", idempotent=False)
    form = SimpleNamespace(href="https://api.local/api/do", raw={"htv:methodName": "POST"})
    return action, form


async def test_token_fetched_and_attached(mock_http):
    thing = parse_thing(TD)
    inv = HttpInvoker(
        credentials={"urn:dev:svc": {"client_id": "cid", "client_secret": "sec"}}
    ).with_security(thing)
    action, form = _action_form()

    result = await inv.invoke(action, form, {"x": 1})

    assert result == {"ok": True}
    assert mock_http["seen_auth"] == ["Bearer tok-123"]


async def test_token_is_cached_across_calls(mock_http):
    thing = parse_thing(TD)
    inv = HttpInvoker(
        credentials={"urn:dev:svc": {"client_id": "cid", "client_secret": "sec"}}
    ).with_security(thing)
    action, form = _action_form()

    await inv.invoke(action, form, {})
    await inv.invoke(action, form, {})

    assert mock_http["token_calls"] == 1  # second call reused the cached token


async def test_password_grant_sends_resource_owner_creds(monkeypatch):
    """The password grant carries the resource owner's username/password
    alongside the client credentials in the token request."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.local":
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "pw-tok", "expires_in": 3600})
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    td = {
        **TD,
        "securityDefinitions": {
            "oauth": {
                "scheme": "oauth2",
                "flow": "password",
                "token": "https://auth.local/token",
                "scopes": ["read"],
            }
        },
    }
    thing = parse_thing(td)
    inv = HttpInvoker(
        credentials={
            "urn:dev:svc": {
                "client_id": "cid",
                "client_secret": "sec",
                "username": "alice",
                "password": "pw",
            }
        }
    ).with_security(thing)
    action, form = _action_form()

    await inv.invoke(action, form, {})

    assert "grant_type=password" in seen["body"]
    assert "username=alice" in seen["body"]
    assert "password=pw" in seen["body"]
    assert seen["auth"] == "Bearer pw-tok"


async def test_static_token_used_directly(mock_http):
    """A plain-string credential with no token endpoint is used as a bearer
    token (no client-credentials exchange)."""
    td = {**TD, "securityDefinitions": {"oauth": {"scheme": "oauth2"}}}
    thing = parse_thing(td)
    inv = HttpInvoker(credentials={"urn:dev:svc": "already-a-token"}).with_security(thing)
    action, form = _action_form()

    await inv.invoke(action, form, {})

    assert mock_http["token_calls"] == 0
    assert mock_http["seen_auth"] == ["Bearer already-a-token"]


async def test_expired_token_is_refetched(monkeypatch):
    """A token whose ``expires_in`` has lapsed is not reused; the next call
    fetches a fresh one."""
    calls = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.local":
            calls["token"] += 1
            # expires_in=0 -> always past the safety margin, so never cacheable
            return httpx.Response(200, json={"access_token": "t", "expires_in": 0})
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    thing = parse_thing(TD)
    inv = HttpInvoker(
        credentials={"urn:dev:svc": {"client_id": "cid", "client_secret": "sec"}}
    ).with_security(thing)
    action, form = _action_form()

    await inv.invoke(action, form, {})
    await inv.invoke(action, form, {})

    assert calls["token"] == 2  # expired between calls, so re-fetched


async def test_token_endpoint_failure_propagates(monkeypatch):
    """If the token endpoint refuses (bad client), the failure surfaces rather
    than silently calling the API without auth."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.local":
            return httpx.Response(401, json={"error": "invalid_client"})
        return httpx.Response(200, json={"ok": True})  # should never be reached

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    thing = parse_thing(TD)
    inv = HttpInvoker(
        credentials={"urn:dev:svc": {"client_id": "cid", "client_secret": "bad"}}
    ).with_security(thing)
    action, form = _action_form()

    with pytest.raises(httpx.HTTPStatusError):
        await inv.invoke(action, form, {})


def test_client_creds_parsing():
    from thingctx.auth import OAuth2ClientCredentialsAuth

    parse = OAuth2ClientCredentialsAuth._creds
    assert parse("id:secret") == ("id", "secret")
    assert parse(("id", "secret")) == ("id", "secret")
    assert parse({"client_id": "id", "client_secret": "secret"}) == ("id", "secret")
