"""The trust gate holds over the MCP bridge (the Claude/Copilot CLI path):
call_tool routes through ThingClient.invoke, so risky tools are gated there
too. Plus a unit check of the elicitation approver's accept/deny/fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from thingctx import LocalInvoker, ThingClient

TD = {
    "@context": ["https://www.w3.org/2022/wot/td/v1.1", {"tc": "https://thingctx.dev/vocab#"}],
    "id": "urn:demo:vault:v1",
    "title": "Vault",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {
        "status": {"idempotent": True, "forms": [{"href": "local://status"}]},
        "wipe": {"@type": "tc:Destructive", "forms": [{"href": "local://wipe"}]},
    },
}


def _inv():
    return LocalInvoker({"status": lambda: {"ok": True}, "wipe": lambda: {"wiped": True}})


async def _call(server, tool, args=None):
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async with connect(server) as s:
        await s.initialize()
        res = await s.call_tool(tool, args or {})
        return res.content[0].text


@pytest.mark.asyncio
async def test_mcp_blocks_declared_destructive_when_denied():
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    server = build_mcp_server(ThingClient(tds=[TD], invokers=[_inv()]), approve=lambda req: False)
    assert "approval denied" in await _call(server, "vault.wipe")


@pytest.mark.asyncio
async def test_mcp_allows_declared_destructive_when_approved():
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    server = build_mcp_server(ThingClient(tds=[TD], invokers=[_inv()]), approve=lambda req: True)
    assert "wiped" in await _call(server, "vault.wipe")


@pytest.mark.asyncio
async def test_mcp_safe_action_not_gated():
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    seen = []
    server = build_mcp_server(
        ThingClient(tds=[TD], invokers=[_inv()]), approve=lambda req: seen.append(1) or False
    )
    assert "ok" in await _call(server, "vault.status")  # idempotent -> never gated
    assert seen == []


@pytest.mark.asyncio
async def test_default_elicit_keeps_existing_approver():
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    own = lambda req: True  # noqa: E731
    client = ThingClient(tds=[TD], invokers=[_inv()], approve=own)
    build_mcp_server(client)  # default approve="elicit" must not clobber it
    assert client._approve is own
    assert "wiped" in await _call(build_mcp_server(client), "vault.wipe")


@pytest.mark.asyncio
async def test_elicit_approver_accept_deny_and_fallback():
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import _elicit_approver
    from thingctx.trust import ApprovalRequest

    req = ApprovalRequest("vault.wipe", {}, "urn:demo:vault:v1", "wipe", "TD-declared")

    def server_with(action=None, raise_elicit=False, no_ctx=False):
        async def elicit(message, requestedSchema):
            if raise_elicit:
                raise RuntimeError("client has no elicitation capability")
            return SimpleNamespace(action=action)

        session = SimpleNamespace(elicit=elicit)

        class S:
            @property
            def request_context(self):
                if no_ctx:
                    raise LookupError("no active request")
                return SimpleNamespace(session=session)

        return S()

    assert await _elicit_approver(server_with(action="accept"))(req) is True
    assert await _elicit_approver(server_with(action="decline"))(req) is False
    assert await _elicit_approver(server_with(action="cancel"))(req) is False
    assert await _elicit_approver(server_with(raise_elicit=True))(req) is False
    assert await _elicit_approver(server_with(no_ctx=True))(req) is False
