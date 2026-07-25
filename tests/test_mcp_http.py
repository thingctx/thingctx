# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The streamable-http serve mode of the MCP bridge (thingctx-mcp --http).

Proves the remote transport is wired: a real MCP initialize handshake over HTTP
returns a session id and the server's capabilities. This is the path a cloud agent
runtime (Azure Foundry, Vertex) or a hosted gateway reaches by URL.
"""

from __future__ import annotations

import contextlib

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
