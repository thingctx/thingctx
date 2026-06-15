"""A real streamable-HTTP MCP server for the pump, reusing 01's build_server.

Unlike stdio (spawned by the client per session), an HTTP MCP server is a
long-running process you operate; clients connect to it over the wire. The
measurement starts this once and then times only connect + initialize +
first call, the per-session latency against a warm server.

Env: PUMP_HTTP / PUMP_MQTT (device endpoints), MCP_PORT (listen port).
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _pump import PumpDevice  # noqa: E402


def build_app():
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    mod = importlib.import_module("01_mcp_baseline")
    pump = PumpDevice()
    server = mod.build_server(pump, os.environ["PUMP_HTTP"], os.environ["PUMP_MQTT"])
    manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)
    return uvicorn, app


if __name__ == "__main__":
    uvicorn, app = build_app()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["MCP_PORT"]), log_level="warning")
