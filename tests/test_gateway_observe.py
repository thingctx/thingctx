# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A guarded MQTT gateway streams an observable property's changes the way it
streams an event: on an authorized request, to that caller's own stream topic."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from conftest import AUDIENCE, ISSUER_V2
from mqtt_fakes import Broker, FakeClient

from thingctx import ThingClient
from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary
from thingctx.bindings import LocalBinding, MqttBinding
from thingctx.gateways import Gateway
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding
from thingctx.identity import JwtGatewayGuard
from thingctx.validate import validate_td

KNOB_ID = "urn:demo:knob"
NATIVE = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": KNOB_ID,
    "title": "Knob",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "properties": {
        "position": {
            "type": "object",
            "observable": True,
            "readOnly": True,
            "forms": [{"href": "local://position", "op": ["readproperty", "observeproperty"]}],
        },
        "label": {"type": "string", "readOnly": True, "forms": [{"href": "local://label"}]},
    },
}
GRANTS = {
    "watcher": {(KNOB_ID, "position", "readproperty"), (KNOB_ID, "position", "observeproperty")},
    "reader": {(KNOB_ID, "position", "readproperty")},
}
BASIC = {"bus_basic": {"scheme": "basic"}}
STREAM = "tc/knob/props/position/observe/{}/stream"


async def _serve(
    monkeypatch: Any, keypair: Any, guarded: bool = True
) -> tuple[Broker, Gateway, LocalBinding]:
    broker = Broker()
    monkeypatch.setattr("paho.mqtt.client.Client", lambda *a, **k: FakeClient(broker))
    device = LocalBinding({"position": lambda: {"turns": 0}, "label": lambda: "front"})
    plain = ThingClient(tds=[NATIVE], bindings=[LocalBinding()])
    pdp = PolicyDecisionPoint(
        vocabulary=build_vocabulary(plain.things), grant_source=LocalPolicyGrantSource(GRANTS)
    )
    options: dict[str, Any] = {}
    if guarded:
        guard = JwtGatewayGuard(issuers=[ISSUER_V2], audience=AUDIENCE, jwks=keypair.jwks())
        options = {"guard": guard, "broker_binds_identity": True, "bus_security": BASIC}
    gateway = Gateway(
        ThingClient(tds=[NATIVE], bindings=[device], pdp=pdp),
        MqttGatewayBinding("bus:1883", **options),
    )
    await gateway.start()
    return broker, gateway, device


def _caller(broker: Broker, td: dict[str, Any], user: str, token: str) -> ThingClient:
    binding = MqttBinding(
        client_factory=lambda: FakeClient(broker),
        timeout=1,
        credentials={"bus_basic": (user, "pw"), "bus_bearer": token},
    )
    return ThingClient(tds=[td], bindings=[binding])


def _token(keypair: Any, user: str) -> str:
    """A token for ``user``, whose role is its own name."""
    return str(keypair.mint(roles=[user], extra={"sub": user}))


def _request(td: dict[str, Any]) -> dict[str, Any]:
    """The observe request as an action, the way a client asks for a stream."""
    href = next(
        f["href"] for f in td["properties"]["position"]["forms"] if "observeproperty" in f["op"]
    )
    return {
        **{k: td[k] for k in ("@context", "id", "securityDefinitions", "security", "uriVariables")},
        "actions": {
            "observe": {
                "output": {"type": "object"},
                "forms": [{"href": href.removesuffix("/stream")}],
            }
        },
    }


async def test_a_guarded_gateway_gives_an_observable_property_an_observe_form(monkeypatch, keypair):
    broker, gateway, _ = await _serve(monkeypatch, keypair)
    td = gateway.projected_tds["knob"]
    forms = {
        name: [(f["op"], f["href"]) for f in p["forms"]] for name, p in td["properties"].items()
    }
    assert forms == {
        "position": [
            (["readproperty"], "mqtt://bus:1883/tc/knob/props/position/read/{+mqttUserName}"),
            (
                ["observeproperty"],
                "mqtt://bus:1883/tc/knob/props/position/observe/{+mqttUserName}/stream",
            ),
        ],
        "label": [(["readproperty"], "mqtt://bus:1883/tc/knob/props/label/read/{+mqttUserName}")],
    }
    assert validate_td(td) == []
    server = next(c for c in broker.clients if c.subs)
    assert "tc/knob/props/position/observe/+" in server.subs
    assert "tc/knob/props/label/observe/+" not in server.subs
    await gateway.aclose()


async def test_an_open_gateway_gives_no_observe_form(monkeypatch, keypair):
    _, gateway, _ = await _serve(monkeypatch, keypair, guarded=False)
    ops = [f["op"] for f in gateway.projected_tds["knob"]["properties"]["position"]["forms"]]
    assert ops == [["readproperty"]]
    await gateway.aclose()


async def test_a_granted_caller_observes_changes_on_its_own_stream(monkeypatch, keypair):
    broker, gateway, device = await _serve(monkeypatch, keypair)
    td = gateway.projected_tds["knob"]
    caller = _caller(broker, td, "watcher", _token(keypair, "watcher"))
    asked = _caller(broker, _request(td), "watcher", _token(keypair, "watcher"))
    changes = await caller.subscribe("knob__position")
    assert await asked.invoke("knob__observe", {}) == {
        "subscribed": True,
        "stream": STREAM.format("watcher"),
    }
    await asyncio.sleep(0.05)
    device.emit("position", {"turns": 2, "pressed": True})
    assert await asyncio.wait_for(anext(changes), 1) == {"turns": 2, "pressed": True}
    await caller.aclose()
    await asked.aclose()
    await gateway.aclose()


async def test_a_caller_granted_only_reads_is_refused_the_stream(monkeypatch, keypair):
    broker, gateway, device = await _serve(monkeypatch, keypair)
    td = gateway.projected_tds["knob"]
    seen: list[Any] = []
    snoop = FakeClient(broker)
    snoop.on_message = lambda _c, _u, m: seen.append(json.loads(m.payload))
    snoop.connect()
    snoop.subscribe(STREAM.format("reader"))
    asked = _caller(broker, _request(td), "reader", _token(keypair, "reader"))
    answer = await asked.invoke("knob__observe", {})
    assert answer == {"error": "subscribe denied", "denied": True}
    device.emit("position", {"turns": 3})
    await asyncio.sleep(0.05)
    assert seen == []
    await asked.aclose()
    await gateway.aclose()
