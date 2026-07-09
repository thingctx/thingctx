# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The north-binding seam: the Gateway engine + a middleware driver.

Proves the modular seam offline (no broker):
- projection carries the driver's OWN namespaced vocabulary (mqv:), and the engine
  never puts a topic/qos on a form itself;
- the engine dispatches the neutral verbs to the native device and back;
- capability-by-presence: a driver is used for exactly the capabilities it
  implements, and an op a driver cannot carry errors EXPLICITLY (never silently
  flattens);
- authz on the bus: a Gateway over a guarded ThingClient enforces before the
  device is touched, so the middleware is not an authz bypass.
"""

from __future__ import annotations

import pytest

from thingctx import ThingClient
from thingctx.authz import (
    AuthorizationDenied,  # noqa: F401  (imported to assert its type is reachable)
    LocalPolicyGrantSource,
    PolicyDecisionPoint,
    build_vocabulary,
)
from thingctx.bindings import LocalBinding
from thingctx.gateways import (
    INVOKE,
    READ,
    WRITE,
    EventMirroring,
    Gateway,
    GatewayBinding,
    PubSubOnly,
    QoSAware,
    RequestReply,
    ServeRequest,
)
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding

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
# capability-by-presence
# --------------------------------------------------------------------------- #


def test_mqtt_driver_advertises_its_capabilities():
    mb = MqttGatewayBinding("bus:1883")
    assert isinstance(mb, GatewayBinding)
    assert isinstance(mb, RequestReply)
    assert isinstance(mb, EventMirroring)
    assert isinstance(mb, QoSAware)
    assert not isinstance(mb, PubSubOnly)


def test_gateway_reflects_driver_capabilities():
    gw = Gateway(_client(), MqttGatewayBinding("bus:1883"))
    assert gw.can_reply is True
    assert gw.can_mirror is True


def test_gateway_rejects_a_non_northbinding():
    with pytest.raises(TypeError):
        Gateway(_client(), object())


# --------------------------------------------------------------------------- #
# projection carries the driver's own vocabulary
# --------------------------------------------------------------------------- #


def test_projection_carries_mqv_vocab_not_engine_fields():
    gw = Gateway(_client(), MqttGatewayBinding("bus:1883"))
    _project(gw)
    form = gw.projected_tds["pump"]["properties"]["rpm"]["forms"][0]
    assert form["href"] == "mqtt://bus:1883/tc/pump/props/rpm"
    assert form["mqv:qos"] == 1  # driver's own namespaced vocab
    assert "mqv:retain" in form
    # the engine replaced native security with the bus scheme
    assert gw.projected_tds["pump"]["security"] == ["bus_nosec"]


# --------------------------------------------------------------------------- #
# the engine dispatches neutral verbs to the native device
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_read_write_invoke_round_trip():
    gw = Gateway(_client(), MqttGatewayBinding("bus:1883"))
    assert await gw.dispatch(ServeRequest("pump", "rpm", READ)) == 1200
    assert (await gw.dispatch(ServeRequest("pump", "rpm", WRITE, {"value": 1800})))["ok"] is True
    assert await gw.dispatch(ServeRequest("pump", "rpm", READ)) == 1800
    assert (await gw.dispatch(ServeRequest("pump", "stop", INVOKE, {})))["stopped"] is True
    await gw.client.aclose()


# --------------------------------------------------------------------------- #
# a pub/sub-only driver: a reply-bearing op errors explicitly, never flattens
# --------------------------------------------------------------------------- #


class _PubSubOnlyFace:
    """A minimal north binding with NO reply channel (fire-and-forget bus)."""

    scheme = "fireforget"
    is_pubsub_only = True

    def project_forms(self, thing, affordance, op):
        # only fire-and-forget ops get a form
        return [{"href": f"ff://bus/{affordance}", "op": [op]}] if op == INVOKE else []

    async def serve(self, engine):
        return None

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_pubsub_only_driver_errors_on_reply_bearing_op():
    gw = Gateway(_client(), _PubSubOnlyFace())
    assert gw.can_reply is False
    result = await gw.dispatch(ServeRequest("pump", "rpm", READ))
    # explicit error, not a silent flatten
    assert isinstance(result, dict) and result.get("no_reply_channel") is True


# --------------------------------------------------------------------------- #
# authz on the bus
# --------------------------------------------------------------------------- #


def _guarded_client(*, roles):
    vocab = build_vocabulary(_client().things)
    grants = LocalPolicyGrantSource({"operator": {(THING_ID, "rpm", "readproperty")}})
    pdp = PolicyDecisionPoint(vocabulary=vocab, grant_source=grants)
    return _client(pdp=pdp, identity={"sub": "a", "roles": roles})


@pytest.mark.asyncio
async def test_bus_authz_allows_granted_read_denies_ungranted_write():
    gw = Gateway(_guarded_client(roles=["operator"]), MqttGatewayBinding("bus:1883"))
    # granted read flows
    assert await gw.dispatch(ServeRequest("pump", "rpm", READ)) == 1200
    # ungranted write is denied by the engine's authz before the device
    denied = await gw.dispatch(ServeRequest("pump", "rpm", WRITE, {"value": 9999}))
    assert isinstance(denied, dict) and (
        "denied" in str(denied).lower() or "authoriz" in str(denied).lower()
    ), denied
    # device untouched: still 1200
    assert await gw.dispatch(ServeRequest("pump", "rpm", READ)) == 1200
    await gw.client.aclose()
