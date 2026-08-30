# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The streamable-http serve mode of the MCP bridge (thingctx-mcp --http).

Proves the remote transport is wired: a real MCP initialize handshake over HTTP
returns a session id and the server's capabilities. This is the path a cloud agent
runtime (Azure Foundry, Vertex) or a hosted gateway reaches by URL.

Also proves the caller boundary: when a token guard is configured, every bridged
call is authorized against the caller on the REQUEST (the validated bearer
token), not the server's own identity.
"""

from __future__ import annotations

import contextlib
import json

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("httpx")

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:lamp",
    "title": "Lamp",
    "securityDefinitions": {"n": {"scheme": "nosec"}},
    "security": ["n"],
    "actions": {"on": {"forms": [{"href": "local://lamp/on"}]}},
}


async def test_streamable_http_initializes():
    """A POST initialize over the streamable-http endpoint returns 200, a session id,
    and the server capabilities, driven in-process with an ASGI transport (no socket)."""
    import httpx
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from thingctx import ThingClient
    from thingctx.integrations.mcp import build_mcp_server

    server = build_mcp_server(ThingClient(tds=[TD], bindings=[]), name="Lamp")
    manager = StreamableHTTPSessionManager(app=server)

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/", app=handle)], lifespan=lifespan)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                "/",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                },
            )
    assert r.status_code == 200
    assert "mcp-session-id" in r.headers
    assert "capabilities" in r.text


def test_non_loopback_bind_warns_without_inbound_auth(capsys, monkeypatch):
    """The HTTP transport has no inbound auth, so a non-loopback bind must say so
    loudly at startup; a loopback bind stays quiet."""
    from thingctx.integrations.mcp import _check_http_exposure

    monkeypatch.delenv("THINGCTX_REQUIRE_AUTH", raising=False)
    _check_http_exposure("0.0.0.0")
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "no inbound authentication" in err.lower()
    assert "reverse proxy" in err
    for quiet in ("127.0.0.1", "localhost", "::1"):
        _check_http_exposure(quiet)
        assert capsys.readouterr().err == ""


def test_require_auth_refuses_non_loopback_bind(monkeypatch):
    """THINGCTX_REQUIRE_AUTH=1 turns the exposure warning into a startup error,
    while a loopback bind still starts."""
    from thingctx.integrations.mcp import _check_http_exposure

    monkeypatch.setenv("THINGCTX_REQUIRE_AUTH", "1")
    with pytest.raises(SystemExit) as exc:
        _check_http_exposure("0.0.0.0")
    assert "refusing" in str(exc.value)
    _check_http_exposure("127.0.0.1")  # loopback is never refused


# --------------------------------------------------------------------------- #
# Caller-aware authorization over streamable HTTP (issue #49)
# --------------------------------------------------------------------------- #

TD_PUMP = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:pump",
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {
        "read_speed": {
            "idempotent": True,
            "forms": [{"href": "local://read_speed", "op": ["invokeaction"]}],
        },
        "set_speed": {
            "forms": [{"href": "local://set_speed", "op": ["invokeaction"]}],
        },
    },
}


def _pump_device():
    from thingctx import LocalBinding

    return LocalBinding(
        {"read_speed": lambda: {"rpm": 1200}, "set_speed": lambda rpm=0: {"ok": True, "rpm": rpm}}
    )


def _pump_client(*, roles):
    """A guarded ThingClient whose PDP grants 'operator' ONLY read_speed."""
    from thingctx import ThingClient
    from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary

    vocab = build_vocabulary(ThingClient(tds=[TD_PUMP], bindings=[_pump_device()]).things)
    grants = LocalPolicyGrantSource({"operator": {("urn:demo:pump", "read_speed", "invokeaction")}})
    pdp = PolicyDecisionPoint(vocabulary=vocab, grant_source=grants)
    identity = {"sub": "the-mcp-server-session", "roles": roles}
    return ThingClient(
        tds=[TD_PUMP], bindings=[_pump_device()], pdp=pdp, identity=identity, authz_raise=False
    )


def _http_app(server, guard=None):
    """The ASGI app serve_http builds: session manager on /, optional guard wrap."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from thingctx.integrations.mcp import _guard_http_app

    manager = StreamableHTTPSessionManager(app=server)

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/", app=handle)], lifespan=lifespan)
    if guard is not None:
        return _guard_http_app(app, guard, allow_anonymous=True), app
    return app


def _mcp_headers(token=None, session_id=None):
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


async def _mcp_post(client, payload, token=None, session_id=None):
    return await client.post("/", headers=_mcp_headers(token, session_id), json=payload)


def _parse_response(r):
    """Parse either a JSON or SSE-framed MCP response into the JSON-RPC body."""
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"SSE response with no data line: {r.text[:400]}")
    return r.json()


async def _init_session(client, token):
    r = await _mcp_post(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        token=token,
    )
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    assert sid
    # notifications/initialized
    r2 = await _mcp_post(
        client,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        token=token,
        session_id=sid,
    )
    assert r2.status_code in (200, 202), r2.text
    return sid


async def _call_tool(client, sid, token, tool, args, *, call_id=10):
    r = await _mcp_post(
        client,
        {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
        token=token,
        session_id=sid,
    )
    assert r.status_code == 200, r.text
    body = _parse_response(r)
    result = body.get("result", {})
    content = result.get("content", [])
    return content[0]["text"] if content else json.dumps(result)


async def test_http_guard_authorizes_per_call_against_the_caller(keypair):
    """Two callers on the SAME bridged server get different decisions: the PDP
    grants 'operator' read_speed only, so an operator token reads and is denied
    the write, while a token with no operator role is denied even the read —
    even though the server's own identity is 'guest' (ungranted) throughout.
    Proves the gate runs against the request's caller, not the bridge identity."""
    pytest.importorskip("mcp")
    import httpx
    from conftest import AUDIENCE, TENANT

    from thingctx.identity import EntraGatewayGuard
    from thingctx.integrations.mcp import build_mcp_server

    guard = EntraGatewayGuard(tenant_id=TENANT, audience=AUDIENCE, jwks=keypair.jwks())
    operator = keypair.mint(scp="Things.Invoke", extra={"roles": ["operator"]})
    nobody = keypair.mint(scp="Things.Invoke")  # no roles claim

    server = build_mcp_server(_pump_client(roles=["guest"]), approve=None)
    app, base = _http_app(server, guard)
    transport = httpx.ASGITransport(app=app)
    async with base.router.lifespan_context(base):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # Caller 1: operator — read allowed ...
            sid = await _init_session(c, operator)
            out = await _call_tool(c, sid, operator, "pump__read_speed", {})
            assert "1200" in out, out
            # ... write denied (operator is granted read_speed only).
            out = await _call_tool(c, sid, operator, "pump__set_speed", {"rpm": 3000})
            assert "3000" not in out, f"denied write must not reach the device: {out}"

            # Caller 2: no operator role — even the read is denied, on the SAME server.
            sid2 = await _init_session(c, nobody)
            out = await _call_tool(c, sid2, nobody, "pump__read_speed", {})
            assert "1200" not in out, f"ungranted caller must not read: {out}"


async def test_http_guard_rejects_missing_and_forged_tokens(keypair, other_keypair):
    """Fail closed: a request with no bearer token (when anonymous is disallowed)
    or a forged token (wrong signing key) never reaches the MCP session layer."""
    pytest.importorskip("mcp")
    import httpx
    from conftest import AUDIENCE, TENANT

    from thingctx.identity import EntraGatewayGuard
    from thingctx.integrations.mcp import _guard_http_app, build_mcp_server

    guard = EntraGatewayGuard(tenant_id=TENANT, audience=AUDIENCE, jwks=keypair.jwks())
    server = build_mcp_server(_pump_client(roles=["operator"]), approve=None)
    base = _http_app(server, guard=None)
    # Rewrap with anonymous NOT allowed (the non-loopback posture).
    app = _guard_http_app(base, guard, allow_anonymous=False)
    transport = httpx.ASGITransport(app=app)
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    async with base.router.lifespan_context(base):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # Missing token -> 401, never initialized.
            r = await c.post("/", headers=_mcp_headers(), json=init_payload)
            assert r.status_code == 401, r.text
            assert "denied" in r.text.lower()
            # Forged token (attacker key) -> 401.
            forged = other_keypair.mint(scp="Things.Invoke", extra={"roles": ["operator"]})
            r = await c.post("/", headers=_mcp_headers(forged), json=init_payload)
            assert r.status_code == 401, r.text


def test_http_guard_from_env_picks_provider(monkeypatch):
    """THINGCTX_TOKEN_GUARD selects the guard provider; the loopback flag tracks
    the bind host; a missing required var forces a startup error."""
    from thingctx.integrations.mcp import _http_guard_from_env

    monkeypatch.delenv("THINGCTX_TOKEN_GUARD", raising=False)
    assert _http_guard_from_env("127.0.0.1") is None

    monkeypatch.setenv("THINGCTX_TOKEN_GUARD", "entra")
    monkeypatch.setenv("THINGCTX_ENTRA_TENANT_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("THINGCTX_ENTRA_AUDIENCE", "api://thingctx-gateway")
    guard, allow_anonymous = _http_guard_from_env("127.0.0.1") or (None, None)
    assert guard is not None and allow_anonymous is True
    from thingctx.identity import EntraGatewayGuard

    assert isinstance(guard, EntraGatewayGuard)

    monkeypatch.setenv("THINGCTX_TOKEN_GUARD", "cloudflare")
    monkeypatch.setenv("THINGCTX_CLOUDFLARE_TEAM", "myteam")
    monkeypatch.setenv("THINGCTX_CLOUDFLARE_AUDIENCE", "aud-tag")
    guard2, allow_anonymous2 = _http_guard_from_env("0.0.0.0") or (None, None)
    assert guard2 is not None and allow_anonymous2 is False
    from thingctx.identity import CloudflareAccessGuard

    assert isinstance(guard2, CloudflareAccessGuard)

    monkeypatch.setenv("THINGCTX_TOKEN_GUARD", "entra")
    monkeypatch.delenv("THINGCTX_ENTRA_TENANT_ID", raising=False)
    with pytest.raises(SystemExit):
        _http_guard_from_env("127.0.0.1")

    monkeypatch.setenv("THINGCTX_TOKEN_GUARD", "unknown")
    with pytest.raises(SystemExit):
        _http_guard_from_env("127.0.0.1")


class _FakeUvicorn:
    """Minimal uvicorn stand-in: records run() calls, never binds a socket."""

    def __init__(self):
        self.calls = []

    def run(self, app, **kwargs):
        self.calls.append((app, kwargs))
        raise SystemExit(0)


class _FakeRegistry:
    def fetch(self):
        return []


def test_http_guard_wraps_serve_http_and_main_wiring(monkeypatch):
    """serve_http with a guard wraps the ASGI app, and main() --http with
    THINGCTX_TOKEN_GUARD plumbs a guard into serve_http."""
    import sys

    import thingctx.integrations.mcp as mcp_mod

    fake = _FakeUvicorn()
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    monkeypatch.setattr(mcp_mod, "from_args", lambda sources: _FakeRegistry())
    monkeypatch.setattr(sys, "argv", ["thingctx-mcp", "--http", "demo.ai"])
    monkeypatch.setenv("THINGCTX_TOKEN_GUARD", "entra")
    monkeypatch.setenv("THINGCTX_ENTRA_TENANT_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("THINGCTX_ENTRA_AUDIENCE", "api://thingctx-gateway")
    monkeypatch.setenv("THINGCTX_REQUIRE_AUTH", "1")
    with pytest.raises(SystemExit):
        mcp_mod.main()
    assert fake.calls, "uvicorn.run was never called; serve_http did not reach the run step"

    # serve_http with guard + allow_anonymous explicitly True wraps the app.
    with pytest.raises(SystemExit):
        mcp_mod.serve_http(
            _FakeRegistry(), host="127.0.0.1", port=0, guard=object(), allow_anonymous=True
        )
    assert len(fake.calls) == 2, "serve_http with a guard must reach uvicorn.run"


def test_http_exposure_guard_early_return(monkeypatch):
    """THINGCTX_TOKEN_GUARD configured means no exposure warning/refusal on a
    non-loopback bind: inbound token validation is in place."""
    from thingctx.integrations.mcp import _check_http_exposure

    monkeypatch.setenv("THINGCTX_TOKEN_GUARD", "entra")
    monkeypatch.delenv("THINGCTX_REQUIRE_AUTH", raising=False)
    _check_http_exposure("0.0.0.0")  # must not warn or raise


def test_guard_http_app_passthrough_non_post_and_anonymous():
    """The guard wrapper passes through non-POST requests untouched, and lets a
    token-less request through when anonymous access is allowed."""
    import asyncio

    import httpx
    from starlette.responses import JSONResponse

    from thingctx.integrations.mcp import _guard_http_app

    seen = []

    async def base(scope, receive, send):
        seen.append(scope.get("method"))
        response = JSONResponse({"ok": True})
        await response(scope, receive, send)

    # Non-POST (GET) passes through even with a guard present.
    app = _guard_http_app(base, object(), allow_anonymous=False)

    async def run_get():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/")

    r = asyncio.run(run_get())
    assert r.status_code == 200 and seen == ["GET"], seen

    # Anonymous allowed: a POST with no bearer token passes through to the app.
    seen.clear()
    app2 = _guard_http_app(base, object(), allow_anonymous=True)

    async def run_post():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2), base_url="http://t"
        ) as c:
            return await c.post("/")

    r2 = asyncio.run(run_post())
    assert r2.status_code == 200 and seen == ["POST"], seen
