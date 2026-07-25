# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The trust gate holds over the MCP bridge (the Claude/Copilot CLI path):
call_tool routes through ThingClient.invoke, so risky tools are gated there
too. Plus a unit check of the elicitation approver's accept/deny/fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from thingctx import LocalBinding, ThingClient

TD = {
    "@context": ["https://www.w3.org/2022/wot/td/v1.1", {"tc": "https://thingctx.dev/vocab#"}],
    "id": "urn:demo:vault:v1",
    "title": "Vault",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {
        "status": {"idempotent": True, "forms": [{"href": "local://status"}]},
        "wipe": {"@type": "tc:Destructive", "forms": [{"href": "local://wipe"}]},
        "nuke": {"@type": "tc:Destructive", "forms": [{"href": "local://nuke"}]},
    },
}


def _inv():
    return LocalBinding({"status": lambda: {"ok": True}, "wipe": lambda: {"wiped": True}})


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

    server = build_mcp_server(ThingClient(tds=[TD], bindings=[_inv()]), approve=lambda req: False)
    assert "approval denied" in await _call(server, "vault__wipe")


@pytest.mark.asyncio
async def test_mcp_allows_declared_destructive_when_approved():
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    server = build_mcp_server(ThingClient(tds=[TD], bindings=[_inv()]), approve=lambda req: True)
    assert "wiped" in await _call(server, "vault__wipe")


@pytest.mark.asyncio
async def test_gated_action_falls_back_to_approve_tool_when_client_cannot_elicit():
    """When the approver signals it cannot show a dialog, a gated action must NOT
    hang or silently deny: the bridge returns a needs_approval envelope + token,
    and calling the approve tool with that token runs the action. This is the
    human-in-the-loop for clients (e.g. Claude Desktop) without elicitation."""
    pytest.importorskip("mcp")
    import json

    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import _NeedsManualApproval, build_mcp_server

    # An approver standing in for a client that cannot elicit: it raises the
    # can't-ask signal instead of returning a verdict.
    def _cannot_elicit(req):
        raise _NeedsManualApproval()

    server = build_mcp_server(ThingClient(tds=[TD], bindings=[_inv()]), approve=_cannot_elicit)
    async with connect(server) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        assert "approve" in names  # the chat-native confirm tool is exposed
        # gated action -> parked, not hung, not denied
        first = json.loads((await s.call_tool("vault__wipe", {})).content[0].text)
        assert first["needs_approval"] is True
        token = first["approval_token"]
        # the user confirms -> approve(token) runs it
        ran = await s.call_tool("approve", {"approval_token": token})
        assert "wiped" in ran.content[0].text
        # the token is single-use: a second approve reports it is gone
        again = json.loads(
            (await s.call_tool("approve", {"approval_token": token})).content[0].text
        )
        assert "no pending approval" in again["error"]


@pytest.mark.asyncio
async def test_approval_tokens_are_random_and_expire(monkeypatch):
    """An approval token must be unguessable (not a shared counter another
    caller on the same transport could predict) and time-bounded: an entry
    older than the TTL is refused and dropped, never run."""
    pytest.importorskip("mcp")
    import json
    import re

    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations import mcp as mcp_mod

    def _cannot_elicit(req):
        raise mcp_mod._NeedsManualApproval()

    server = mcp_mod.build_mcp_server(
        ThingClient(tds=[TD], bindings=[_inv()]), approve=_cannot_elicit
    )
    async with connect(server) as s:
        await s.initialize()
        first = json.loads((await s.call_tool("vault__wipe", {})).content[0].text)
        second = json.loads((await s.call_tool("vault__wipe", {})).content[0].text)
        t1, t2 = first["approval_token"], second["approval_token"]
        assert t1 != t2
        for tok in (t1, t2):
            assert not re.fullmatch(r"approval-\d+", tok)
            assert len(tok) >= 32  # token_urlsafe(32) -> 43 chars of entropy
        # Age every pending entry past the TTL: an expired token is refused.
        monkeypatch.setattr(mcp_mod, "_APPROVAL_TTL_S", -1.0)
        expired = json.loads((await s.call_tool("approve", {"approval_token": t1})).content[0].text)
        assert "no pending approval" in expired["error"]
        # And the expired entries were dropped, not left redeemable: restoring
        # the TTL must not resurrect the second token.
        monkeypatch.setattr(mcp_mod, "_APPROVAL_TTL_S", 300.0)
        gone = json.loads((await s.call_tool("approve", {"approval_token": t2})).content[0].text)
        assert "no pending approval" in gone["error"]


@pytest.mark.asyncio
async def test_mcp_safe_action_not_gated():
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    seen = []
    server = build_mcp_server(
        ThingClient(tds=[TD], bindings=[_inv()]), approve=lambda req: seen.append(1) or False
    )
    assert "ok" in await _call(server, "vault__status")  # idempotent -> never gated
    assert seen == []


@pytest.mark.asyncio
async def test_default_elicit_keeps_existing_approver():
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    own = lambda req: True  # noqa: E731
    client = ThingClient(tds=[TD], bindings=[_inv()], approve=own)
    build_mcp_server(client)  # default approve="elicit" must not clobber it
    assert client._approve is own
    assert "wiped" in await _call(build_mcp_server(client), "vault__wipe")


@pytest.mark.asyncio
async def test_elicit_approver_accept_deny_and_fallback():
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import _elicit_approver, _NeedsManualApproval
    from thingctx.trust import ApprovalRequest

    req = ApprovalRequest("vault__wipe", {}, "urn:demo:vault:v1", "wipe", "TD-declared")

    def server_with(action=None, raise_elicit=False, no_ctx=False, can_elicit=True):
        async def elicit(message, requestedSchema):
            if raise_elicit:
                raise RuntimeError("client has no elicitation capability")
            return SimpleNamespace(action=action)

        # check_client_capability reports whether the client declared elicitation.
        session = SimpleNamespace(elicit=elicit, check_client_capability=lambda cap: can_elicit)

        class S:
            @property
            def request_context(self):
                if no_ctx:
                    raise LookupError("no active request")
                return SimpleNamespace(session=session)

        return S()

    # With an elicitation-capable client, the dialog answer is honored.
    assert await _elicit_approver(server_with(action="accept"))(req) is True
    assert await _elicit_approver(server_with(action="decline"))(req) is False
    assert await _elicit_approver(server_with(action="cancel"))(req) is False
    # No live session at all: deny (a gate with nobody to open stays shut).
    assert await _elicit_approver(server_with(no_ctx=True))(req) is False
    # Client cannot elicit -> raise _NeedsManualApproval so the bridge routes to
    # the approve-tool flow (rather than hanging or silently denying).
    with pytest.raises(_NeedsManualApproval):
        await _elicit_approver(server_with(can_elicit=False))(req)
    # Elicit unexpectedly fails at call time -> also route to the approve tool.
    with pytest.raises(_NeedsManualApproval):
        await _elicit_approver(server_with(raise_elicit=True))(req)


@pytest.mark.asyncio
async def test_bypass_replay_does_not_auto_approve_a_concurrent_call():
    """Replaying a user-approved call must not open the gate for OTHER calls in
    flight on the same shared client. Over a shared transport (e.g. --http),
    two gated calls can overlap; the approve-tool replay of one must satisfy the
    human-confirm for THAT call only, never for a fresh unrelated call landing
    inside its window. The fresh call must still require approval."""
    pytest.importorskip("mcp")
    import asyncio
    import json

    from mcp import types

    from thingctx.integrations.mcp import _NeedsManualApproval, build_mcp_server

    started = asyncio.Event()
    release = asyncio.Event()

    async def _wipe():
        # Hold the replay in flight so the fresh call overlaps its window.
        started.set()
        await release.wait()
        return {"wiped": True}

    def _cannot_elicit(req):
        raise _NeedsManualApproval()

    inv = LocalBinding({"wipe": _wipe, "nuke": lambda: {"nuked": True}})
    client = ThingClient(tds=[TD], bindings=[inv], approve=_cannot_elicit)
    server = build_mcp_server(client, tool_mode="flat")
    # Drive the raw request handler directly: the session harness serializes
    # calls, which would hide the race this test is about.
    handler = server.request_handlers[types.CallToolRequest]

    def _call(name, args=None):
        req = types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name=name, arguments=args or {}),
        )
        return handler(req)

    def _payload(result):
        return json.loads(result.root.content[0].text)

    parked = _payload(await _call("vault__wipe"))
    token = parked["approval_token"]
    replay = asyncio.create_task(_call("approve", {"approval_token": token}))
    await asyncio.wait_for(started.wait(), 2.0)
    # The fresh gated call lands while the bypass replay is in flight.
    fresh = _payload(await _call("vault__nuke"))
    assert fresh.get("needs_approval") is True  # NOT silently auto-approved
    assert "nuked" not in fresh
    release.set()
    assert _payload(await replay) == {"wiped": True}  # the replay still proceeds
