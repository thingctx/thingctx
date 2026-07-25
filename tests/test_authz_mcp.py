# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Authorization holds over the MCP bridge, and what it can and cannot carry.

The MCP bridge (the Claude/Copilot CLI path) routes every tool call through
``ThingClient.invoke``. The authorization gate is on that same method, so a
GUARDED client passed to ``build_mcp_server`` enforces the PDP for MCP clients
too: an ungranted operation is refused before the device is touched.

This file pins BOTH sides of the honest claim:

* PROVEN today: the gate fires over the bridge. Deny blocks the device; allow
  lets it through. Authorization is not bypassable by going through MCP.
* The LIMIT, made explicit: MCP's transport carries the CLIENT's session, not a
  per-tool-call caller identity. So the identity the gate authorizes against is
  the one the bridged client was built with (a server-level identity), NOT a
  fresh end-caller claim delivered by MCP per call. The final test marks that
  gap with xfail: it will flip to a real assertion the day an MCP identity-
  propagation extension delivers per-call caller claims to the bridge.
"""

from __future__ import annotations

import pytest

from thingctx import LocalBinding, ThingClient
from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary

THING_ID = "urn:demo:pump"

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": THING_ID,
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


def _device():
    return LocalBinding(
        {"read_speed": lambda: {"rpm": 1200}, "set_speed": lambda rpm=0: {"ok": True, "rpm": rpm}}
    )


def _guarded_client(*, roles):
    """A ThingClient whose PDP grants the given roles ONLY read_speed."""
    vocab = build_vocabulary(ThingClient(tds=[TD], bindings=[_device()]).things)
    grants = LocalPolicyGrantSource({"operator": {(THING_ID, "read_speed", "invokeaction")}})
    pdp = PolicyDecisionPoint(vocabulary=vocab, grant_source=grants)
    identity = {"sub": "the-mcp-server-session", "roles": roles}
    return ThingClient(
        tds=[TD], bindings=[_device()], pdp=pdp, identity=identity, authz_raise=False
    )


async def _call(server, tool, args=None):
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async with connect(server) as s:
        await s.initialize()
        res = await s.call_tool(tool, args or {})
        return res.content[0].text


@pytest.mark.asyncio
async def test_mcp_bridge_enforces_authz_allow():
    """A granted operation flows through the bridge to the device."""
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    server = build_mcp_server(_guarded_client(roles=["operator"]), approve=None)
    out = await _call(server, "pump__read_speed")
    assert "1200" in out, out


@pytest.mark.asyncio
async def test_mcp_bridge_enforces_authz_deny_before_device():
    """An ungranted operation is refused over the bridge; the device is not
    touched. This is the core claim: authorization is not bypassable via MCP."""
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    # 'operator' is granted read_speed only; set_speed has no grant -> deny.
    server = build_mcp_server(_guarded_client(roles=["operator"]), approve=None)
    out = await _call(server, "pump__set_speed", {"rpm": 3000})
    # The denial surfaces as text (authz_raise=False makes the envelope carry it);
    # the point is the device call never returned an ok/rpm result.
    assert "ok" not in out.lower() or "denied" in out.lower() or "not" in out.lower(), out
    assert "3000" not in out, f"denied write must not reach the device: {out}"


@pytest.mark.asyncio
async def test_mcp_bridge_authz_uses_server_level_identity_not_per_call():
    """PINS THE LIMIT: the identity the gate sees is the bridged client's own
    (server-level), because MCP carries the client session, not a per-call caller
    claim. A client granted nothing denies EVERY caller identically, since there
    is no per-call identity for MCP to differentiate on."""
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    # A client whose identity has NO operator role: the server-level identity
    # governs, so even read_speed (granted only to 'operator') is denied. There
    # is no MCP channel to present a different, granted caller per call.
    server = build_mcp_server(_guarded_client(roles=["guest"]), approve=None)
    out = await _call(server, "pump__read_speed")
    assert "1200" not in out, (
        "the gate authorized against the server-level identity (roles=['guest']); "
        f"MCP presented no per-call caller to override it: {out}"
    )


@pytest.mark.xfail(
    reason="MCP does not yet propagate a per-call caller identity to the bridge; "
    "this flips to a real assertion when an MCP identity-propagation extension ships",
    strict=True,
)
@pytest.mark.asyncio
async def test_mcp_per_call_caller_identity_reaches_the_gate():
    """FUTURE: when MCP carries a per-call caller identity, a granted caller and an
    ungranted caller hitting the SAME bridged server must get different decisions.
    Today MCP has no such channel, so this is xfail(strict): it will fail (and this
    test start passing) the moment the capability exists, surfacing the change."""
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    # One server, one bridged client. Today there is no way to hand it two
    # different validated caller identities per call, so we cannot make a granted
    # caller succeed while an ungranted caller is denied on the SAME server.
    server = build_mcp_server(_guarded_client(roles=["guest"]), approve=None)
    # If MCP propagated a per-call identity, we would present an 'operator' caller
    # here and expect success despite the server default being 'guest'.
    out = await _call(server, "pump__read_speed")
    assert "1200" in out  # only reachable once per-call identity propagation exists
