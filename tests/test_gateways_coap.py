# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The CoAP north binding: the second reference driver on the gateway seam.

Proves the seam is NOT MQTT-shaped, all offline (no CoAP socket, no aiocoap for
the logic tests):
- it passes the same GatewayBinding CONTRACT the MQTT driver does;
- projection carries the driver's OWN ``covv:`` vocabulary and, crucially, does
  NOT emit ``mqv:retain`` (CoAP has no retain) -> driver-specific vocab, proven;
- it advertises exactly the capabilities its transport has (request/reply, event
  mirroring via Observe, QoS-aware), and not the ones it does not;
- the request/reply round-trip dispatches the neutral verbs to the native device
  through the engine, via the offline ``handle_request`` helper (no socket);
- authz on the bus: a Gateway over a guarded ThingClient denies an ungranted
  write at the handler, so CoAP is not an authz bypass.

Only the live-serve test needs aiocoap; everything above runs without it.
"""

from __future__ import annotations

import json

import pytest

from thingctx import ThingClient
from thingctx.authz import (
    LocalPolicyGrantSource,
    PolicyDecisionPoint,
    build_vocabulary,
)
from thingctx.bindings import LocalBinding
from thingctx.gateways import (
    READ,
    WRITE,
    Announces,
    EventMirroring,
    Gateway,
    GatewayBinding,
    PubSubOnly,
    QoSAware,
    RequestReply,
    ServeRequest,
)
from thingctx.gateways.builtin.coap import CoapGatewayBinding
from thingctx.testing import (
    assert_gateway_binding_contract,
    gateway_binding_capabilities,
)

THING_ID = "urn:demo:pump:v1"

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
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
    "actions": {"stop": {"forms": [{"href": "local://stop", "op": ["invokeaction"]}]}},
    "events": {"alarm": {"forms": [{"href": "local://alarm", "op": ["subscribeevent"]}]}},
}


class Pump:
    def __init__(self):
        self.v = 1200

    def get_rpm(self):
        return self.v

    def set_rpm(self, value):
        self.v = value
        return {"ok": True}

    def stop(self):
        return {"stopped": True}


def _client(device=None, *, pdp=None, identity=None):
    return ThingClient(
        tds=[TD], bindings=[LocalBinding(device or Pump())], pdp=pdp, identity=identity
    )


def _project(gw):
    from thingctx.gateways.north import _slug

    for t in gw.client.things:
        gw._projected[_slug(t)] = gw._project(t)


# --------------------------------------------------------------------------- #
# the CONTRACT: the CoAP driver satisfies the same GatewayBinding contract
# --------------------------------------------------------------------------- #


def test_coap_driver_passes_north_binding_contract():
    # No aiocoap needed: the contract checks shapes, it does not serve.
    assert_gateway_binding_contract(CoapGatewayBinding("localhost"))


# --------------------------------------------------------------------------- #
# capability-by-presence: it advertises exactly what CoAP can do
# --------------------------------------------------------------------------- #


def test_coap_driver_advertises_its_capabilities():
    cb = CoapGatewayBinding("localhost")
    assert isinstance(cb, GatewayBinding)
    assert isinstance(cb, RequestReply)  # a CoAP response is the reply
    assert isinstance(cb, EventMirroring)  # via CoAP Observe (RFC 7641)
    assert isinstance(cb, QoSAware)  # reads its own covv: terms
    # NOT these: it replies (not pub/sub-only), and this reference driver does not
    # add a separate birth/death announce (/.well-known/core is the discovery).
    assert not isinstance(cb, PubSubOnly)
    assert not isinstance(cb, Announces)


def test_coap_capability_report():
    caps = gateway_binding_capabilities(CoapGatewayBinding("localhost"))
    assert caps["request_reply"] is True
    assert caps["event_mirroring"] is True
    assert caps["qos_aware"] is True
    assert caps["pubsub_only"] is False
    assert caps["announces"] is False


def test_gateway_reflects_coap_capabilities():
    gw = Gateway(_client(), CoapGatewayBinding("localhost"))
    assert gw.can_reply is True
    assert gw.can_mirror is True


# --------------------------------------------------------------------------- #
# projection carries covv: vocab and NOT mqv:retain (driver-specific vocab)
# --------------------------------------------------------------------------- #


def test_projection_carries_covv_vocab_not_mqv_retain():
    gw = Gateway(_client(), CoapGatewayBinding("localhost"))
    _project(gw)
    td = gw.projected_tds["pump"]

    # a property read form: GET, JSON content-format, coap:// href, covv: vocab
    read_form = _form_for_op(td["properties"]["rpm"]["forms"], READ)
    assert read_form["href"] == "coap://localhost/tc/pump/props/rpm"
    assert read_form["covv:method"] == "GET"
    assert read_form["covv:contentFormat"] == 50
    # a property write form: PUT
    write_form = _form_for_op(td["properties"]["rpm"]["forms"], WRITE)
    assert write_form["covv:method"] == "PUT"
    # an action form: POST
    action_form = td["actions"]["stop"]["forms"][0]
    assert action_form["covv:method"] == "POST"

    # CoAP has NO retain: no mqv:retain (and no retain vocab of any kind) anywhere.
    for aff in (read_form, write_form, action_form):
        assert "mqv:retain" not in aff
        assert not any("retain" in k.lower() for k in aff)
        # and no mqv: vocab at all: this is a CoAP driver
        assert not any(k.startswith("mqv:") for k in aff)

    # the engine still replaced native security with the bus scheme
    assert td["security"] == ["bus_nosec"]


def test_events_project_as_observable():
    gw = Gateway(_client(), CoapGatewayBinding("localhost"))
    _project(gw)
    ev_form = gw.projected_tds["pump"]["events"]["alarm"]["forms"][0]
    # events use CoAP Observe: an observable GET resource, no retain
    assert ev_form["href"] == "coap://localhost/tc/pump/events/alarm"
    assert ev_form["covv:method"] == "GET"
    assert ev_form["covv:observe"] is True
    assert "mqv:retain" not in ev_form


def test_quality_terms_are_covv_and_have_no_retain():
    terms = CoapGatewayBinding("localhost").quality_terms()
    assert terms == ("covv:method", "covv:observe", "covv:contentFormat")
    assert all(t.startswith("covv:") for t in terms)
    assert not any("retain" in t for t in terms)


# --------------------------------------------------------------------------- #
# request/reply round-trip through the engine, via handle_request (no socket)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_handle_request_read_write_invoke_round_trip():
    cb = CoapGatewayBinding("localhost")
    gw = Gateway(_client(), cb)
    # serve() would need aiocoap; wire the gateway by hand for the offline path.
    cb._gateway = gw

    # GET props/rpm -> read
    got = await cb.handle_request("tc/pump/props/rpm", "GET")
    assert json.loads(got) == 1200

    # PUT props/rpm -> write, then read back the new value
    put = await cb.handle_request("tc/pump/props/rpm", "PUT", json.dumps({"value": 1800}))
    assert json.loads(put)["ok"] is True
    got2 = await cb.handle_request("tc/pump/props/rpm", "GET")
    assert json.loads(got2) == 1800

    # POST actions/stop -> invoke
    posted = await cb.handle_request("tc/pump/actions/stop", "POST", b"{}")
    assert json.loads(posted)["stopped"] is True

    await gw.client.aclose()


@pytest.mark.asyncio
async def test_reply_encodes_response_payload_not_a_side_topic():
    # reply() in CoAP returns the encoded response (no <topic>/reply publish).
    cb = CoapGatewayBinding("localhost")
    body = await cb.reply(ServeRequest("pump", "rpm", READ, correlation="tc/pump/props/rpm"), 42)
    assert isinstance(body, bytes)
    assert json.loads(body) == 42


@pytest.mark.asyncio
async def test_unknown_resource_is_an_honest_not_found():
    cb = CoapGatewayBinding("localhost")
    cb._gateway = Gateway(_client(), cb)
    body = await cb.handle_request("tc/pump/props/rpm", "DELETE")  # no CoAP DELETE mapping
    assert json.loads(body)["not_found"] is True
    await cb._gateway.client.aclose()


# --------------------------------------------------------------------------- #
# authz on the bus: an ungranted write is denied at the handler (no bypass)
# --------------------------------------------------------------------------- #


def _guarded_client(*, roles):
    vocab = build_vocabulary(_client().things)
    grants = LocalPolicyGrantSource({"operator": {(THING_ID, "rpm", "readproperty")}})
    pdp = PolicyDecisionPoint(vocabulary=vocab, grant_source=grants)
    return _client(pdp=pdp, identity={"sub": "a", "roles": roles})


@pytest.mark.asyncio
async def test_bus_authz_allows_granted_read_denies_ungranted_write():
    cb = CoapGatewayBinding("localhost")
    gw = Gateway(_guarded_client(roles=["operator"]), cb)
    cb._gateway = gw

    # granted read flows back on the response payload
    got = await cb.handle_request("tc/pump/props/rpm", "GET")
    assert json.loads(got) == 1200

    # ungranted write is denied by the engine's authz BEFORE the device; the denial
    # rides back on the same CoAP exchange, not a crash and not a silent success
    denied_body = await cb.handle_request("tc/pump/props/rpm", "PUT", json.dumps({"value": 9999}))
    denied = json.loads(denied_body)
    assert isinstance(denied, dict) and (
        "denied" in str(denied).lower() or "authoriz" in str(denied).lower()
    ), denied

    # device untouched: still 1200
    got2 = await cb.handle_request("tc/pump/props/rpm", "GET")
    assert json.loads(got2) == 1200

    await gw.client.aclose()


# --------------------------------------------------------------------------- #
# mirror_event offline: no observers -> nowhere to push (honest no-op)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_mirror_event_without_observers_is_a_noop():
    cb = CoapGatewayBinding("localhost")
    # no serve() ran, so no observable resource exists; mirroring must not raise
    await cb.mirror_event("pump", "alarm", {"level": "high"})


# --------------------------------------------------------------------------- #
# the live serve path: needs aiocoap. Only this test imports the CoAP stack.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_serve_stands_up_a_context_when_aiocoap_present():
    pytest.importorskip("aiocoap")
    cb = CoapGatewayBinding("localhost")
    gw = Gateway(_client(), cb)
    _project(gw)
    await gw.start()
    try:
        assert cb._context is not None
        # a real inbound would call handle_request; prove that path still works live
        got = await cb.handle_request("tc/pump/props/rpm", "GET")
        assert json.loads(got) == 1200
    finally:
        await gw.aclose()


def _form_for_op(forms, op):
    for f in forms:
        if op in f.get("op", []):
            return f
    raise AssertionError(f"no form for op {op!r} in {forms!r}")
