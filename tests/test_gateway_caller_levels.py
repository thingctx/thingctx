# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A guarded MQTT gateway ends every request topic in its sender's MQTT User Name
and runs a request only when the token names that User Name as its subject. These
tests prove the gateway's half against a broker that enforces no topic rules; the
broker's half is the live test on a locked Mosquitto."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from conftest import AUDIENCE, ISSUER_V2
from mqtt_fakes import Broker, FakeClient
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from thingctx import ThingClient
from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary
from thingctx.bindings import LocalBinding, MqttBinding
from thingctx.gateways import Gateway
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding
from thingctx.identity import JwtGatewayGuard
from thingctx.thing import MQTT_USER_NAME
from thingctx.validate import validate_semantics, validate_td

PUMP_ID = "urn:demo:pump"
NATIVE = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": PUMP_ID,
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {"stop": {"output": {"type": "object"}, "forms": [{"href": "local://stop"}]}},
    "properties": {"rpm": {"type": "integer", "forms": [{"href": "local://rpm"}]}},
    "events": {"telemetry": {"forms": [{"href": "local://telemetry"}]}},
}
GRANTS = {
    "operator": {
        (PUMP_ID, "stop", "invokeaction"),
        (PUMP_ID, "rpm", "readproperty"),
        (PUMP_ID, "telemetry", "subscribeevent"),
    }
}
BASIC = {"bus_basic": {"scheme": "basic"}}


class _Pump:
    def __init__(self) -> None:
        self.stops = 0
        self.device = LocalBinding({"stop": self.stop, "rpm": lambda: 1200})

    def stop(self) -> dict:
        self.stops += 1
        return {"stopped": self.stops}


async def _serve(monkeypatch, keypair, **kw: Any) -> tuple[Broker, Gateway, _Pump]:
    broker = Broker()
    monkeypatch.setattr("paho.mqtt.client.Client", lambda *a, **k: FakeClient(broker))
    pump = _Pump()
    plain = ThingClient(tds=[NATIVE], bindings=[LocalBinding()])
    pdp = PolicyDecisionPoint(
        vocabulary=build_vocabulary(plain.things), grant_source=LocalPolicyGrantSource(GRANTS)
    )
    guard = JwtGatewayGuard(issuers=[ISSUER_V2], audience=AUDIENCE, jwks=keypair.jwks())
    binding = MqttGatewayBinding(
        "bus:1883", guard=guard, broker_binds_identity=True, bus_security=BASIC, **kw
    )
    gateway = Gateway(ThingClient(tds=[NATIVE], bindings=[pump.device], pdp=pdp), binding)
    await gateway.start()
    return broker, gateway, pump


def _caller(broker: Broker, td: dict, user: str, token: str | None) -> ThingClient:
    credentials: dict[str, Any] = {"bus_basic": (user, "pw")}
    if token is not None:
        credentials["bus_bearer"] = token
    binding = MqttBinding(
        client_factory=lambda: FakeClient(broker), timeout=1, credentials=credentials
    )
    return ThingClient(tds=[td], bindings=[binding])


def _token(keypair, sub: str) -> str:
    return keypair.mint(roles=["operator"], extra={"sub": sub})


def _snoop(broker: Broker, topic: str) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []
    client = FakeClient(broker)
    client.on_message = lambda _c, _u, m: seen.append((m.topic, json.loads(m.payload)))
    client.connect()
    client.subscribe(topic)
    return seen


async def test_a_guarded_gateway_names_the_sender_in_every_request_form(monkeypatch, keypair):
    broker, gateway, _ = await _serve(monkeypatch, keypair)
    td = gateway.projected_tds["pump"]
    assert td["uriVariables"] == {
        "mqttUserName": {
            "@type": MQTT_USER_NAME,
            "type": "string",
            "minLength": 1,
            "description": "The MQTT User Name the client connects with. A request "
            "runs only when its token's subject is this name.",
        }
    }
    forms = [
        f
        for kind in ("actions", "properties", "events")
        for a in td[kind].values()
        for f in a["forms"]
    ]
    assert [f["href"] for f in forms] == [
        "mqtt://bus:1883/tc/pump/actions/stop/{+mqttUserName}",
        "mqtt://bus:1883/tc/pump/props/rpm/read/{+mqttUserName}",
        "mqtt://bus:1883/tc/pump/props/rpm/write/{+mqttUserName}",
        "mqtt://bus:1883/tc/pump/events/telemetry/subscribe/{+mqttUserName}/stream",
    ]
    assert not any("mqv:retain" in f for f in forms)
    assert td["@context"] == "https://www.w3.org/2022/wot/td/v1.1"
    assert validate_td(td) == [] and validate_semantics(td) == []
    server = next(c for c in broker.clients if c.subs)
    assert server.subs == {
        "tc/pump/actions/stop/+",
        "tc/pump/props/rpm/read/+",
        "tc/pump/props/rpm/write/+",
        "tc/pump/events/telemetry/subscribe/+",
    }
    await gateway.aclose()


async def test_an_open_gateway_declares_retain_only_where_it_retains(monkeypatch):
    broker = Broker()
    monkeypatch.setattr("paho.mqtt.client.Client", lambda *a, **k: FakeClient(broker))
    pump = _Pump()
    gateway = Gateway(
        ThingClient(tds=[NATIVE], bindings=[pump.device]),
        MqttGatewayBinding("bus:1883", default_retain=True),
    )
    await gateway.start()
    td = gateway.projected_tds["pump"]
    # A property topic carries requests and never a value, so its forms claim no
    # retained value and a read is answered on the reply topic.
    assert all(f["mqv:retain"] is False for f in td["properties"]["rpm"]["forms"])
    reader = ThingClient(
        tds=[td], bindings=[MqttBinding(client_factory=lambda: FakeClient(broker), timeout=1)]
    )
    assert await reader.read_property("pump__rpm") == 1200
    # A mirrored event is published on its own topic, retained as its form says.
    assert td["events"]["telemetry"]["forms"][0]["mqv:retain"] is True
    await asyncio.sleep(0.05)
    pump.device.emit("telemetry", {"rpm": 1})
    await asyncio.sleep(0.05)
    assert broker.record("tc/pump/events/telemetry") == {"rpm": 1}
    await gateway.aclose()


async def test_an_open_gateway_projects_as_before(monkeypatch):
    broker = Broker()
    monkeypatch.setattr("paho.mqtt.client.Client", lambda *a, **k: FakeClient(broker))
    gateway = Gateway(
        ThingClient(tds=[NATIVE], bindings=[LocalBinding()]), MqttGatewayBinding("bus:1883")
    )
    await gateway.start()
    assert gateway.projected_tds["pump"] == {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": PUMP_ID,
        "title": "Pump",
        "securityDefinitions": {"bus_nosec": {"scheme": "nosec"}},
        "security": ["bus_nosec"],
        "actions": {
            "stop": {
                "output": {"type": "object"},
                "forms": [
                    {
                        "href": "mqtt://bus:1883/tc/pump/actions/stop",
                        "op": ["invokeaction"],
                        "mqv:qos": "1",
                    }
                ],
            }
        },
        "properties": {
            "rpm": {
                "type": "integer",
                "forms": [
                    {
                        "href": "mqtt://bus:1883/tc/pump/props/rpm",
                        "op": ["readproperty"],
                        "mqv:qos": "1",
                        "mqv:retain": False,
                    },
                    {
                        "href": "mqtt://bus:1883/tc/pump/props/rpm",
                        "op": ["writeproperty"],
                        "mqv:qos": "1",
                        "mqv:retain": False,
                    },
                ],
            }
        },
        "events": {
            "telemetry": {
                "forms": [
                    {
                        "href": "mqtt://bus:1883/tc/pump/events/telemetry",
                        "op": ["subscribeevent"],
                        "mqv:qos": "1",
                        "mqv:retain": False,
                    }
                ]
            }
        },
    }
    await gateway.aclose()


@pytest.mark.parametrize("user", ["alice", "carol@corp.example"])
async def test_a_client_sends_each_request_under_its_own_user_name(monkeypatch, keypair, user):
    broker, gateway, pump = await _serve(monkeypatch, keypair)
    td = gateway.projected_tds["pump"]
    replies = _snoop(broker, "tc/pump/actions/stop/+/reply")
    client = _caller(broker, td, user, _token(keypair, user))
    assert await client.invoke("pump__stop", {}) == {"stopped": 1}
    assert await client.read_property("pump__rpm") == 1200
    assert replies == [(f"tc/pump/actions/stop/{user}/reply", {"stopped": 1})]
    await gateway.aclose()


def _publish(broker: Broker, topic: str, body: str, token: str) -> None:
    request = Properties(PacketTypes.PUBLISH)
    request.CorrelationData = b"c-1"
    request.UserProperty = ("authorization", f"Bearer {token}")
    sender = FakeClient(broker)
    sender.connect()
    sender.publish(topic, body, properties=request)


async def test_a_guarded_read_and_write_each_have_a_topic_of_their_own(monkeypatch, keypair):
    """The topic names the operation: a read runs as readproperty whatever the
    payload, and a write runs as writeproperty, so a policy, and a broker rule, can
    grant one without the other."""
    broker, gateway, _ = await _serve(monkeypatch, keypair)
    replies = _snoop(broker, "tc/pump/props/rpm/+/alice/reply")
    token = _token(keypair, "alice")

    _publish(broker, "tc/pump/props/rpm/read/alice", '{"value": 9}', token)
    _publish(broker, "tc/pump/props/rpm/write/alice", '{"value": 9}', token)
    _publish(broker, "tc/pump/props/rpm/write/alice", "", token)
    await asyncio.sleep(0.05)

    read, write, empty = replies
    assert read == ("tc/pump/props/rpm/read/alice/reply", 1200)
    assert write[0] == "tc/pump/props/rpm/write/alice/reply"
    assert write[1]["denied"] is True
    assert "writeproperty" in json.dumps(write[1])
    assert empty == (
        "tc/pump/props/rpm/write/alice/reply",
        {"error": 'a write carries its value as {"value": ...}'},
    )
    await gateway.aclose()


async def test_a_read_only_property_has_no_write_topic(monkeypatch, keypair):
    monkeypatch.setitem(
        NATIVE["properties"], "rpm", {**NATIVE["properties"]["rpm"], "readOnly": True}
    )
    broker, gateway, _ = await _serve(monkeypatch, keypair)
    forms = gateway.projected_tds["pump"]["properties"]["rpm"]["forms"]
    assert [f["href"] for f in forms] == ["mqtt://bus:1883/tc/pump/props/rpm/read/{+mqttUserName}"]
    server = next(c for c in broker.clients if c.subs)
    assert "tc/pump/props/rpm/write/+" not in server.subs
    await gateway.aclose()


async def test_a_token_for_another_name_is_refused_before_the_device_runs(monkeypatch, keypair):
    broker, gateway, pump = await _serve(monkeypatch, keypair)
    td = gateway.projected_tds["pump"]
    replies = _snoop(broker, "tc/pump/actions/stop/+/reply")
    thief = _caller(broker, td, "bob", _token(keypair, "alice"))
    assert await thief.invoke("pump__stop", {}) == {
        "error": "authentication required",
        "denied": True,
    }
    assert pump.stops == 0
    assert [topic for topic, _ in replies] == ["tc/pump/actions/stop/bob/reply"]
    await gateway.aclose()


async def test_an_empty_level_names_no_one(monkeypatch, keypair):
    broker, gateway, pump = await _serve(monkeypatch, keypair)
    request = Properties(PacketTypes.PUBLISH)
    request.CorrelationData = b"c-1"
    request.UserProperty = ("authorization", f"Bearer {_token(keypair, '')}")
    sender = FakeClient(broker)
    sender.connect()
    sender.publish("tc/pump/actions/stop/", "{}", properties=request)
    await asyncio.sleep(0.05)
    assert pump.stops == 0
    await gateway.aclose()


async def test_each_caller_gets_its_own_stream_and_a_repeat_replaces_only_its_own(
    monkeypatch, keypair
):
    broker, gateway, pump = await _serve(monkeypatch, keypair)
    request = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": PUMP_ID,
        "securityDefinitions": gateway.projected_tds["pump"]["securityDefinitions"],
        "security": gateway.projected_tds["pump"]["security"],
        "uriVariables": gateway.projected_tds["pump"]["uriVariables"],
        "actions": {
            "subscribe": {
                "output": {"type": "object"},
                "forms": [
                    {"href": "mqtt://bus:1883/tc/pump/events/telemetry/subscribe/{+mqttUserName}"}
                ],
            }
        },
    }
    streams = {
        user: _snoop(broker, f"tc/pump/events/telemetry/subscribe/{user}/stream")
        for user in ("alice", "bob")
    }
    for user in ("alice", "bob", "alice"):
        answer = await _caller(broker, request, user, _token(keypair, user)).invoke(
            "pump__subscribe", {"stream": "tc/pump/td"}
        )
        assert answer == {
            "subscribed": True,
            "stream": f"tc/pump/events/telemetry/subscribe/{user}/stream",
        }
    await asyncio.sleep(0.05)
    pump.device.emit("telemetry", {"rpm": 1})
    await asyncio.sleep(0.05)
    assert streams == {
        "alice": [("tc/pump/events/telemetry/subscribe/alice/stream", {"rpm": 1})],
        "bob": [("tc/pump/events/telemetry/subscribe/bob/stream", {"rpm": 1})],
    }
    await gateway.aclose()
