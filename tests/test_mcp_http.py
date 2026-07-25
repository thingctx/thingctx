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
