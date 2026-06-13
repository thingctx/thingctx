"""The MCP bridge: a TD becomes MCP tools, callable over a real session."""

from __future__ import annotations

import pytest

from thingctx import LocalInvoker, ThingClient

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:pump:v1",
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {
        "status": {"idempotent": True, "forms": [{"href": "local://status"}]},
        "set_speed": {
            "input": {"type": "object", "properties": {"rpm": {"type": "integer"}}},
            "forms": [{"href": "local://set_speed"}],
        },
    },
}


@pytest.mark.asyncio
async def test_td_becomes_callable_mcp_tools():
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    inv = LocalInvoker(
        {"status": lambda: {"rpm": 0}, "set_speed": lambda rpm=0: {"ok": True, "rpm": rpm}}
    )
    server = build_mcp_server(ThingClient(tds=[TD], invokers=[inv]), name="pump")
    async with connect(server) as s:
        await s.initialize()
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert "pump.set_speed" in tools
        # the risk hints come from the TD's own semantics
        assert tools["pump.status"].annotations.readOnlyHint is True
        # call a tool for real
        res = await s.call_tool("pump.set_speed", {"rpm": 1200})
        assert "1200" in res.content[0].text
