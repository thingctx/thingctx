# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""In-memory MCP client/server lifecycle for bridge integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams


@asynccontextmanager
async def connect(
    server: Any,
    *,
    elicitation_callback: Any = None,
    message_handler: Any = None,
) -> AsyncIterator[ClientSession]:
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        server_done = anyio.Event()

        async def run_server() -> None:
            try:
                await server.run(
                    server_read,
                    server_write,
                    server.create_initialization_options(),
                )
            finally:
                server_done.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_server)
            try:
                async with ClientSession(
                    client_read,
                    client_write,
                    elicitation_callback=elicitation_callback,
                    message_handler=message_handler,
                ) as session:
                    yield session
            finally:
                await client_write.aclose()
                await server_write.aclose()
                with anyio.move_on_after(2):
                    await server_done.wait()
                if not server_done.is_set():
                    task_group.cancel_scope.cancel()
