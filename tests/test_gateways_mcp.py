# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The MCP north binding: a RICH, protocol-specific driver on the gateway seam.

MCP is not a plain address-mapped bus. It has tools/resources/prompts/elicitation
a bus lacks. This proves the north seam carries that richness honestly:

- capability-by-presence: the driver advertises GatewayBinding + RequestReply, and
  does NOT falsely claim EventMirroring / PubSubOnly / QoSAware / Announces (it
  wires no event->notification, has no reply-less bus, no per-message QoS);
- projection carries the driver's OWN ``mcpv:`` vocabulary (kind, annotations),
  never an engine field, so MCP's specifics ride opaquely through the engine;
- authz on the bus: driving a tool THROUGH the composed MCP server still enforces
  the PDP, because the server routes through ``client.invoke`` (the same gate),
  so the MCP seam is not an authorization bypass. Mirrors tests/test_authz_mcp.py.
"""

from __future__ import annotations

import pytest

from thingctx import ThingClient
from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary
from thingctx.bindings import LocalBinding
from thingctx.gateways import (
    SUBSCRIBE,
    Announces,
    EventMirroring,
    Gateway,
    GatewayBinding,
    PubSubOnly,
    QoSAware,
    RequestReply,
)
from thingctx.gateways.builtin.mcp import McpGatewayBinding

THING_ID = "urn:demo:pump:v1"

TD = {
    "@context": ["https://www.w3.org/2022/wot/td/v1.1", {"tc": "https://thingctx.dev/vocab#"}],
    "id": THING_ID,
    "title": "Pump",
    "securityDefinitions": {"n": {"scheme": "nosec"}},
    "security": ["n"],
    "properties": {
        "rpm": {
            "type": "integer",
            "forms": [{"href": "local://rpm", "op": ["readproperty", "writeproperty"]}],
        },
    },
    "actions": {
        "read_speed": {
            "idempotent": True,
            "forms": [{"href": "local://read_speed", "op": ["invokeaction"]}],
        },
        "set_speed": {"forms": [{"href": "local://set_speed", "op": ["invokeaction"]}]},
    },
    "events": {"alarm": {"forms": [{"href": "local://alarm", "op": ["subscribeevent"]}]}},
}


def _device():
    return LocalBinding(
        {
            "rpm": lambda value=None: 1200 if value is None else {"ok": True},
            "read_speed": lambda: {"rpm": 1200},
            "set_speed": lambda rpm=0: {"ok": True, "rpm": rpm},
        }
    )


def _client(*, pdp=None, identity=None):
    return ThingClient(
        tds=[TD],
        bindings=[_device()],
        pdp=pdp,
        identity=identity,
        authz_raise=False,
    )


def _project(gw):
    from thingctx.gateways.north import _slug

    for t in gw.client.things:
        gw._projected[_slug(t)] = gw._project(t)


# --------------------------------------------------------------------------- #
# capability-by-presence: advertise RequestReply, and NOTHING it does not do
# --------------------------------------------------------------------------- #


def test_mcp_driver_advertises_request_reply_only():
    mb = McpGatewayBinding()
    # It IS a north binding and it DOES reply (MCP is request/reply).
    assert isinstance(mb, GatewayBinding)
    assert isinstance(mb, RequestReply)
    # It must NOT falsely claim capabilities it has not wired.
    assert not isinstance(mb, EventMirroring)  # no event->MCP-notification wired
    assert not isinstance(mb, PubSubOnly)  # it replies; it is not fire-and-forget
    assert not isinstance(mb, QoSAware)  # MCP has no per-message QoS
    assert not isinstance(mb, Announces)  # the MCP server's own listings are discovery


def test_gateway_reflects_mcp_capabilities():
    gw = Gateway(_client(), McpGatewayBinding())
    assert gw.can_reply is True  # RequestReply present
    assert gw.can_mirror is False  # EventMirroring deliberately absent


def test_gateway_accepts_mcp_driver_rejects_non_northbinding():
    Gateway(_client(), McpGatewayBinding())  # constructs: it satisfies GatewayBinding
    with pytest.raises(TypeError):
        Gateway(_client(), object())


# --------------------------------------------------------------------------- #
# projection carries the driver's OWN mcpv: vocabulary, not an engine field
# --------------------------------------------------------------------------- #


def test_projection_carries_mcpv_vocab():
    gw = Gateway(_client(), McpGatewayBinding("fleet"))
    _project(gw)
    td = gw.projected_tds["pump"]

    # An action projects to an mcp:// tool href carrying mcpv: vocab.
    tool_form = td["actions"]["set_speed"]["forms"][0]
    assert tool_form["href"] == "mcp://fleet/tools/pump.set_speed"
    assert tool_form["mcpv:kind"] == "tool"
    ann = tool_form["mcpv:annotations"]
    # set_speed is not idempotent -> destructive hint, not read-only.
    assert ann["destructiveHint"] is True
    assert ann["readOnlyHint"] is False

    # An idempotent action is read-only, not destructive.
    ro = td["actions"]["read_speed"]["forms"][0]["mcpv:annotations"]
    assert ro["idempotentHint"] is True
    assert ro["destructiveHint"] is False
    assert ro["readOnlyHint"] is True

    # A property projects to an mcp:// resource href with mcpv:kind = resource.
    prop_form = td["properties"]["rpm"]["forms"][0]
    assert prop_form["href"].startswith("mcp://fleet/resources/pump.rpm")
    assert prop_form["mcpv:kind"] == "resource"

    # No engine field leaked onto a form; mcpv: is the only namespaced vocab.
    assert not any(k.startswith("mqv:") for k in tool_form)


def test_subscribeevent_gets_no_form_honest_td():
    # MCP wires no live event push, so an event op yields no form (honest TD).
    mb = McpGatewayBinding()
    gw = Gateway(_client(), mb)
    thing = gw.client.things[0]
    assert mb.project_forms(thing, "alarm", SUBSCRIBE) == []


# --------------------------------------------------------------------------- #
# COMPOSE with the existing bridge: serve builds the real MCP server
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_serve_composes_the_existing_mcp_server():
    pytest.importorskip("mcp")
    mb = McpGatewayBinding("fleet")
    gw = Gateway(_client(), mb)
    await gw.start()
    # serve() stood up the composed MCP server over the fleet's client.
    assert mb.server is not None
    await mb.aclose()
    assert mb.server is None
    await gw.client.aclose()


# --------------------------------------------------------------------------- #
# authz on the bus: driving THROUGH the MCP server still enforces the PDP
# --------------------------------------------------------------------------- #


def _guarded_client(*, roles):
    """A ThingClient whose PDP grants ``operator`` ONLY read_speed."""
    vocab = build_vocabulary(_client().things)
    grants = LocalPolicyGrantSource({"operator": {(THING_ID, "read_speed", "invokeaction")}})
    pdp = PolicyDecisionPoint(vocabulary=vocab, grant_source=grants)
    return _client(pdp=pdp, identity={"sub": "the-gateway", "roles": roles})


async def _call_via_server(server, tool, args=None):
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async with connect(server) as s:
        await s.initialize()
        res = await s.call_tool(tool, args or {})
        return res.content[0].text


@pytest.mark.asyncio
async def test_driving_through_mcp_server_enforces_authz():
    """A guarded client whose PDP denies set_speed: the denial holds when the tool
    is driven through the composed MCP server. Authorization is not bypassable by
    going through the MCP seam, because the server routes through client.invoke."""
    pytest.importorskip("mcp")

    mb = McpGatewayBinding("fleet", approve=None)
    gw = Gateway(_guarded_client(roles=["operator"]), mb)
    await gw.start()
    server = mb.server
    assert server is not None

    # Granted read_speed flows through to the device.
    granted = await _call_via_server(server, "pump.read_speed")
    assert "1200" in granted, granted

    # Ungranted set_speed is refused by the gate before the device; the denied
    # value never reaches it.
    denied = await _call_via_server(server, "pump.set_speed", {"rpm": 3000})
    assert "3000" not in denied, f"denied write must not reach the device: {denied}"
    assert (
        "ok" not in denied.lower() or "denied" in denied.lower() or "not" in denied.lower()
    ), denied

    await mb.aclose()
    await gw.client.aclose()
