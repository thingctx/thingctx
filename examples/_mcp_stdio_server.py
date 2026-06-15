"""A real stdio MCP server for the pump, reusing 01's build_server, so the
measurement can spawn it as a separate process and time a true MCP cold
start (process + import + handshake), not the in-memory shortcut.

The device's live endpoints arrive via the environment (PUMP_HTTP /
PUMP_MQTT); local actions run against this process's own PumpDevice.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _pump import PumpDevice  # noqa: E402


async def serve() -> None:
    from mcp.server.stdio import stdio_server

    mod = importlib.import_module("01_mcp_baseline")
    pump = PumpDevice()
    server = mod.build_server(pump, os.environ["PUMP_HTTP"], os.environ["PUMP_MQTT"])
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(serve())
