# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A URI variable typed MQTT_USER_NAME holds the MQTT User Name the client connects
with: the client fills it in, verbatim, and the tool projection leaves it out."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mqtt_fakes import Broker, FakeClient
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from thingctx import ThingClient, TransportError
from thingctx.bindings import MqttBinding
from thingctx.thing import MQTT_USER_NAME

PUMP_ID = "urn:demo:pump"
USER_VAR = {"@type": MQTT_USER_NAME, "type": "string"}
BASIC = {"bus_basic": {"scheme": "basic"}}


def _pump(**extra: Any) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": PUMP_ID,
        "title": "Pump",
        "securityDefinitions": BASIC,
        "security": ["bus_basic"],
        "uriVariables": {"mqttUserName": USER_VAR},
        "actions": {
            "stop": {
                "output": {"type": "object"},
                "forms": [{"href": "mqtt://bus:1883/tc/pump/actions/stop/{+mqttUserName}"}],
            }
        },
        "properties": {
            "rpm": {
                "type": "integer",
                "forms": [{"href": "mqtt://bus:1883/tc/pump/props/rpm/{+mqttUserName}"}],
            }
        },
        "events": {
            "telemetry": {
                "forms": [
                    {"href": "mqtt://bus:1883/tc/pump/events/t/subscribe/{+mqttUserName}/stream"}
                ]
            }
        },
        **extra,
    }


class _Responder:
    """Answers every request under tc/pump and keeps the topics it was sent."""

    def __init__(self, broker: Broker) -> None:
        self.topics: list[str] = []
        self.client = FakeClient(broker)
        self.client.on_message = self._on_message
        self.client.connect()
        self.client.subscribe("tc/pump/actions/stop/+")
        self.client.subscribe("tc/pump/props/rpm/+")

    def _on_message(self, _c: Any, _u: Any, msg: Any) -> None:
        self.topics.append(msg.topic)
        reply = Properties(PacketTypes.PUBLISH)
        reply.CorrelationData = msg.properties.CorrelationData
        self.client.publish(f"{msg.topic}/reply", json.dumps({"ok": True}), properties=reply)


def _client(broker: Broker, td: dict, credentials: dict | None) -> tuple[ThingClient, list]:
    made: list[FakeClient] = []

    def factory() -> FakeClient:
        made.append(FakeClient(broker))
        return made[-1]

    binding = MqttBinding(client_factory=factory, timeout=1, credentials=credentials)
    return ThingClient(tds=[td], bindings=[binding]), made


def test_the_user_name_variable_is_not_a_tool_parameter() -> None:
    td = _pump()
    td["uriVariables"]["zone"] = {"type": "string"}
    td["actions"]["stop"]["forms"][0]["href"] += "/{zone}"
    params = ThingClient(tds=[td], bindings=[]).tool_specs[0]["function"]["parameters"]
    assert set(params["properties"]) == {"zone"}
    assert params["required"] == ["zone"]


@pytest.mark.parametrize(
    ("context", "term", "hidden"),
    [
        pytest.param({"tc": "https://thingctx.com/vocab#"}, "tc:MqttUserName", True, id="tc"),
        pytest.param({"tc": "https://example.org/other#"}, "tc:MqttUserName", False, id="other"),
        pytest.param({}, MQTT_USER_NAME, True, id="iri"),
    ],
)
def test_the_term_is_known_by_its_iri(context: dict, term: str, hidden: bool) -> None:
    td = _pump(**{"@context": ["https://www.w3.org/2022/wot/td/v1.1", context]})
    td["uriVariables"]["mqttUserName"] = {"@type": term, "type": "string"}
    params = ThingClient(tds=[td], bindings=[]).tool_specs[0]["function"]["parameters"]
    assert ("mqttUserName" in params.get("properties", {})) is not hidden


@pytest.mark.parametrize("user", ["alice", "carol@corp.example"])
async def test_each_request_goes_under_the_user_name_the_client_connects_with(user: str) -> None:
    broker = Broker()
    device = _Responder(broker)
    client, made = _client(broker, _pump(), {"bus_basic": (user, "pw")})
    assert await client.invoke("pump__stop", {}) == {"ok": True}
    assert await client.read_property("pump__rpm") == {"ok": True}
    stream = await client.subscribe("pump__telemetry")
    assert device.topics == [f"tc/pump/actions/stop/{user}", f"tc/pump/props/rpm/{user}"]
    assert made[-1].subs == {f"tc/pump/events/t/subscribe/{user}/stream"}
    assert all(c.auth == (user, "pw") for c in made)
    await stream.aclose()


@pytest.mark.parametrize("credentials", [None, {"bus_basic": ("", "pw")}], ids=["none", "empty"])
async def test_a_client_without_a_user_name_sends_nothing(credentials: dict | None) -> None:
    broker = Broker()
    device = _Responder(broker)
    client, made = _client(broker, _pump(), credentials)
    with pytest.raises(TransportError, match="none that can be one topic level"):
        await client.invoke("pump__stop", {})
    assert made == []
    assert device.topics == []


@pytest.mark.parametrize("user", ["a/b", "a+b", "#", "a?b", "a\nb", "a b", "reply"])
async def test_a_user_name_that_cannot_be_one_topic_level_is_refused(user: str) -> None:
    broker = Broker()
    client, made = _client(broker, _pump(), {"bus_basic": (user, "pw")})
    with pytest.raises(TransportError, match="none that can be one topic level"):
        await client.invoke("pump__stop", {})
    assert made == []


async def test_a_simple_expansion_still_puts_the_user_name_verbatim() -> None:
    td = _pump()
    td["actions"]["stop"]["forms"][0]["href"] = (
        "mqtt://bus:1883/tc/pump/actions/stop/{mqttUserName}"
    )
    broker = Broker()
    device = _Responder(broker)
    client, _ = _client(broker, td, {"bus_basic": ("carol@corp.example", "pw")})
    assert await client.invoke("pump__stop", {}) == {"ok": True}
    assert device.topics == ["tc/pump/actions/stop/carol@corp.example"]


async def test_an_argument_cannot_take_the_user_names_place() -> None:
    td = _pump()
    td["actions"]["stop"]["input"] = {
        "type": "object",
        "properties": {"mqttUserName": {"type": "string"}},
    }
    broker = Broker()
    device = _Responder(broker)
    client, made = _client(broker, td, {"bus_basic": ("alice", "pw")})
    with pytest.raises(TransportError, match="took the place"):
        await client.invoke("pump__stop", {"mqttUserName": "bob"})
    assert made == []
    assert device.topics == []
