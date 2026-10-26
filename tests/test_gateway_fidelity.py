# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A consumer behind the MQTT gateway sees what the source Thing Description
declares, gets a long running action's final state as JSON, and can read a
property over MQTT."""

from __future__ import annotations

import asyncio
import json

import pytest

from thingctx import ThingClient, parse_thing
from thingctx.bindings import LocalBinding, MqttBinding
from thingctx.gateways import INVOKE, Gateway, ServeRequest
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding
from thingctx.lifecycle import ActionStatus

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:valve",
    "title": "Valve",
    "securityDefinitions": {"n": {"scheme": "nosec"}},
    "security": ["n"],
    "properties": {
        "level": {
            "type": "number",
            "description": "tank level in metres",
            "observable": True,
            "forms": [{"href": "local://level", "op": ["readproperty"]}],
        }
    },
    "actions": {
        "fill": {
            "description": "fill to a level",
            "synchronous": False,
            "input": {"type": "object", "properties": {"to": {"type": "number"}}},
            "output": {"type": "object", "properties": {"reached": {"type": "number"}}},
            "forms": [
                {"href": "slow://fill", "op": ["invokeaction", "queryaction", "cancelaction"]}
            ],
        }
    },
    "events": {
        "overflow": {
            "description": "the tank overflowed",
            "data": {"type": "string"},
            "forms": [{"href": "local://overflow", "op": ["subscribeevent"]}],
        }
    },
}


class SlowBinding:
    """A device whose action finishes on the second poll."""

    scheme = "slow"

    def with_things(self, things):
        return self

    async def invoke_async(self, action, form, arguments):
        return ActionStatus(
            status="running",
            href="slow://fill/1",
            form=form,
            thing_id=action.thing_id,
            name=action.name,
        )

    async def query_action(self, status):
        return ActionStatus(
            status="completed",
            output={"reached": 2.0},
            href=status.href,
            form=status.form,
            thing_id=status.thing_id,
            name=status.name,
        )


def test_projection_keeps_the_affordance_as_declared():
    client = ThingClient(
        tds=[TD],
        bindings=[LocalBinding({"level": lambda: 1.5}), SlowBinding()],
        approve_when="never",
    )
    gw = Gateway(client, MqttGatewayBinding("bus:1883"))
    for thing in client.things:
        gw._projected[thing.id.split(":")[-1]] = gw._project(thing)
    td = gw.projected_tds["valve"]
    fill = td["actions"]["fill"]
    assert fill["input"] == TD["actions"]["fill"]["input"]
    assert fill["output"] == TD["actions"]["fill"]["output"]
    assert fill["description"] == "fill to a level"
    assert fill["synchronous"] is False
    assert fill["forms"] == [
        {"href": "mqtt://bus:1883/tc/valve/actions/fill", "op": ["invokeaction"], "mqv:qos": "1"}
    ]
    level = td["properties"]["level"]
    assert (level["type"], level["description"], level["observable"]) == (
        "number",
        "tank level in metres",
        True,
    )
    assert level["forms"][0]["href"] == "mqtt://bus:1883/tc/valve/props/level"
    assert td["events"]["overflow"]["data"] == {"type": "string"}
    assert "local://" not in json.dumps(td)
    # the parsed model keeps each definition, as it already did for actions
    thing = parse_thing(TD)
    assert thing.properties["level"].raw["description"] == "tank level in metres"
    assert thing.events["overflow"].raw["data"] == {"type": "string"}


@pytest.mark.asyncio
async def test_dispatch_waits_for_a_long_running_action_and_replies_json():
    client = ThingClient(
        tds=[TD],
        bindings=[LocalBinding({"level": lambda: 1.5}), SlowBinding()],
        approve_when="never",
    )
    gw = Gateway(client, MqttGatewayBinding("bus:1883"))
    result = await gw.dispatch(ServeRequest("valve", "fill", INVOKE, {"to": 2.0}))
    assert result == {
        "status": "completed",
        "output": {"reached": 2.0},
        "error": None,
        "href": "slow://fill/1",
    }
    assert json.dumps(result)  # a reply the wire can carry
    await client.aclose()


class _Msg:
    def __init__(self, payload: bytes):
        self.payload = payload


class _Info:
    rc = 0

    def wait_for_publish(self, timeout=None):
        return None


class FakeBroker:
    """The slice of paho the binding uses. A retained value is delivered on
    subscribe; a request on a topic is answered on its reply topic."""

    def __init__(self, *, retained=None, answer=None):
        self.retained, self.answer = retained, answer
        self.subscriptions: list[str] = []
        self.publishes: list[tuple[str, str]] = []
        self.on_connect = self.on_message = None

    def reconnect_delay_set(self, **kw):
        pass

    def connect(self, host, port):
        pass

    def loop_start(self):
        self.on_connect(self, None, {}, 0)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)
        if self.retained is not None and not topic.endswith("/reply"):
            self.on_message(self, None, _Msg(json.dumps(self.retained).encode()))

    def publish(self, topic, payload, qos=0, retain=False, properties=None):
        self.publishes.append((topic, payload))
        if self.answer is not None:
            self.on_message(self, None, _Msg(json.dumps(self.answer).encode()))
        return _Info()


def _mqtt_td(retain: bool) -> dict:
    form = {"href": "mqtt://bus:1883/tank/level", "op": ["readproperty"], "mqv:retain": retain}
    return {
        **TD,
        "properties": {"level": {"type": "number", "forms": [form]}},
        "actions": {},
        "events": {},
    }


@pytest.mark.asyncio
async def test_mqtt_read_takes_the_retained_value():
    broker = FakeBroker(retained=1.5)
    client = ThingClient(
        tds=[_mqtt_td(True)],
        bindings=[MqttBinding(client_factory=lambda: broker)],
        approve_when="never",
    )
    assert await client.read_property("valve__level") == 1.5
    assert broker.subscriptions == ["tank/level"]
    assert broker.publishes == []


@pytest.mark.asyncio
async def test_mqtt_read_asks_on_the_topic_and_listens_on_reply():
    broker = FakeBroker(answer=1.5)
    client = ThingClient(
        tds=[_mqtt_td(False)],
        bindings=[MqttBinding(client_factory=lambda: broker)],
        approve_when="never",
    )
    assert await client.read_property("valve__level") == 1.5
    assert broker.subscriptions == ["tank/level/reply"]
    assert broker.publishes == [("tank/level", "")]


@pytest.mark.asyncio
async def test_mqtt_read_times_out_as_a_transport_error():
    from thingctx import TransportError

    broker = FakeBroker()
    client = ThingClient(
        tds=[_mqtt_td(False)],
        bindings=[MqttBinding(client_factory=lambda: broker, timeout=0.05)],
        approve_when="never",
    )
    with pytest.raises(TransportError):
        await client.read_property("valve__level")
    await asyncio.sleep(0)
