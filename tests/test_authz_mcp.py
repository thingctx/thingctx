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
* The LIMIT, made explicit, and it has two halves. MCP defines no caller identity
  in the protocol itself: no subject, actor, or on-behalf-of field on a tool call.
  Over HTTP an OAuth token does ride on every request, so a bridge served that way
  can read a per-call caller. Over stdio and the in-memory transport these tests
  use, there is no identity channel at all. Either way the gate here authorizes
  against the identity the bridged client was built with, a server-level identity.
  The final test marks that with xfail; it flips to a real assertion when the
  bridge takes a caller from the request instead.

What no transport supplies, and what the per-operation gate actually wants, is a
claim separating the human principal from the agent acting for them. MCP has no
such claim, and no per-operation authorization primitive: once a client is
authenticated it reaches every tool the server exposes.
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
    reason="MCP carries no caller identity in the protocol itself, only an OAuth token on "
    "each HTTP request, and this test drives the in-memory transport where there is no "
    "request to read one from; it flips to a real assertion when the bridge takes a "
    "per-call caller from the request",
    strict=True,
)
@pytest.mark.asyncio
async def test_mcp_per_call_caller_identity_reaches_the_gate():
    """FUTURE: a granted caller and an ungranted caller hitting the SAME bridged
    server must get different decisions. That needs a caller per call, which this
    transport cannot supply, so this is xfail(strict): it starts passing the moment
    the bridge resolves an identity per call, surfacing the change."""
    pytest.importorskip("mcp")
    from thingctx.integrations.mcp import build_mcp_server

    # The identity is fixed when the server is built, and the in-memory transport
    # carries no request to override it from, so both callers get the same answer.
    # Served over HTTP the bridge could read the request's token instead, which is
    # the change this test is waiting for.
    server = build_mcp_server(_guarded_client(roles=["guest"]), approve=None)
    # With a per-call caller we would present 'operator' here and expect success
    # despite the server default being 'guest'.
    out = await _call(server, "pump__read_speed")
    assert "1200" in out  # only reachable once per-call identity propagation exists
