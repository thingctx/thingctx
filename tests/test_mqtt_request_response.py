# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""MQTT 5 request/response between MqttBinding and the MQTT gateway, against
fake paho clients: a request names its Response Topic and Correlation Data, and
a reply copies the data back."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from thingctx import ThingClient, TransportError
from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary
from thingctx.bindings import LocalBinding, MqttBinding
from thingctx.gateways import Gateway, ServeRequest
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding

SAME = object()  # a reply that copies the request's Correlation Data back

ACTION = SimpleNamespace(name="stop", output_schema={"type": "object"})
FORM = SimpleNamespace(href="mqtt://bus:1883/tc/pump/actions/stop", raw={})


class _Bus:
    """The slice of paho the binding uses. Each publish is recorded, then a
    subscribed client gets the scripted replies, given as (body, correlation
    data)."""

    def __init__(self, *replies: tuple[Any, Any]):
        self.replies = replies
        self.published: list[tuple[str, str, Any]] = []
        self.subscriptions: list[str] = []
        self.on_connect: Any = None
        self.on_message: Any = None

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

    def publish(self, topic, payload, qos=0, retain=False, properties=None):
        self.published.append((topic, payload, properties))
        sent = getattr(properties, "CorrelationData", None)
        for body, data in self.replies if self.subscriptions else ():
            props = SimpleNamespace(CorrelationData=sent if data is SAME else data)
            msg = SimpleNamespace(payload=json.dumps(body).encode(), properties=props)
            self.on_message(self, None, msg)
        return SimpleNamespace(wait_for_publish=lambda timeout=None: None)


async def test_a_request_names_its_reply_topic_and_correlation_data():
    bus = _Bus(({"stopped": True}, SAME))
    binding = MqttBinding(client_factory=lambda: bus)
    assert await binding.invoke(ACTION, FORM, {}) == {"stopped": True}
    topic, _, props = bus.published[0]
    assert (topic, props.ResponseTopic) == ("tc/pump/actions/stop", "tc/pump/actions/stop/reply")
    assert len(props.CorrelationData) == 16
    assert bus.subscriptions == ["tc/pump/actions/stop/reply"]


async def test_each_request_carries_its_own_correlation_data():
    bus = _Bus(({"stopped": True}, SAME))
    binding = MqttBinding(client_factory=lambda: bus)
    await binding.invoke(ACTION, FORM, {})
    await binding.invoke(ACTION, FORM, {})
    first, second = (props.CorrelationData for _, _, props in bus.published)
    assert first != second


async def test_a_reply_to_another_request_is_skipped():
    bus = _Bus(({"stopped": "theirs"}, b"another request"), ({"stopped": True}, SAME))
    binding = MqttBinding(client_factory=lambda: bus)
    assert await binding.invoke(ACTION, FORM, {}) == {"stopped": True}


async def test_a_reply_without_correlation_data_is_taken():
    bus = _Bus(({"stopped": True}, None))
    binding = MqttBinding(client_factory=lambda: bus)
    assert await binding.invoke(ACTION, FORM, {}) == {"stopped": True}


async def test_a_publish_without_output_is_not_a_request():
    bus = _Bus()
    action = SimpleNamespace(name="stop", output_schema=None)
    await MqttBinding(client_factory=lambda: bus).invoke(action, FORM, {})
    assert bus.published[0][2] is None


async def test_requests_run_on_mqtt5(monkeypatch):
    asked: list[bool] = []

    def new_client(self, v5=False):
        asked.append(v5)
        return _Bus(({"stopped": True}, SAME))

    monkeypatch.setattr(MqttBinding, "_new_client", new_client)
    binding = MqttBinding()
    await binding.invoke(ACTION, FORM, {})
    await binding.invoke(SimpleNamespace(name="stop", output_schema=None), FORM, {})
    assert asked == [True, False]


async def test_a_property_read_is_a_request():
    bus = _Bus((7, b"another request"), (1200, SAME))
    prop = SimpleNamespace(name="rpm")
    form = SimpleNamespace(href="mqtt://bus:1883/tc/pump/props/rpm", raw={})
    assert await MqttBinding(client_factory=lambda: bus).read(prop, form) == 1200
    topic, payload, props = bus.published[0]
    assert (topic, payload) == ("tc/pump/props/rpm", "")
    assert props.ResponseTopic == "tc/pump/props/rpm/reply"


class _Unserved(_Bus):
    """A broker whose PUBACK carries ``reason``, before any reply is scripted."""

    def __init__(self, reason: int, *replies: tuple[Any, Any]):
        super().__init__(*replies)
        self.reason = reason
        self.on_publish: Any = None

    def publish(self, topic, payload, qos=0, retain=False, properties=None):
        sent = super().publish(topic, payload, qos, retain, properties)
        self.on_publish(self, None, 1, SimpleNamespace(value=self.reason), None)
        return sent


async def test_a_request_expires_when_the_caller_stops_waiting():
    bus = _Bus(({"stopped": True}, SAME))
    await MqttBinding(client_factory=lambda: bus, timeout=2.5).invoke(ACTION, FORM, {})
    assert bus.published[0][2].MessageExpiryInterval == 3


async def test_a_request_nobody_subscribes_to_fails_at_once():
    binding = MqttBinding(client_factory=lambda: _Unserved(0x10), timeout=30)
    started = asyncio.get_running_loop().time()
    with pytest.raises(TransportError, match="no subscriber on tc/pump/actions/stop"):
        await binding.invoke(ACTION, FORM, {})
    assert asyncio.get_running_loop().time() - started < 1


async def test_a_request_the_broker_refuses_fails_at_once():
    binding = MqttBinding(client_factory=lambda: _Unserved(0x87), timeout=30)
    started = asyncio.get_running_loop().time()
    with pytest.raises(TransportError, match="refused the publish on tc/pump/actions/stop"):
        await binding.invoke(ACTION, FORM, {})
    assert asyncio.get_running_loop().time() - started < 1


async def test_a_refused_request_carries_the_puback_reason_code_as_its_status():
    binding = MqttBinding(client_factory=lambda: _Unserved(0x87), timeout=30)
    with pytest.raises(TransportError) as caught:
        await binding.invoke(ACTION, FORM, {})
    assert caught.value.status == 0x87


_CARRIED = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:pump",
    "title": "Pump",
    "securityDefinitions": {
        "bus_bearer": {"scheme": "bearer", "in": "header", "name": "authorization"}
    },
    "security": ["bus_bearer"],
    "actions": {"prime": {"forms": [{"href": "mqtt://bus:1883/tc/pump/actions/prime"}]}},
}


async def test_a_call_that_carries_a_token_awaits_the_answer_without_an_output():
    bus = _Bus(({"denied": True}, SAME))
    binding = MqttBinding(client_factory=lambda: bus, credentials={"bus_bearer": "tok"})
    client = ThingClient(tds=[_CARRIED], bindings=[binding])
    assert await client.invoke("pump__prime", {}) == {"denied": True}


async def test_a_call_that_carries_a_token_fails_at_once_when_the_broker_refuses():
    binding = MqttBinding(
        client_factory=lambda: _Unserved(0x87), timeout=30, credentials={"bus_bearer": "tok"}
    )
    client = ThingClient(tds=[_CARRIED], bindings=[binding])
    with pytest.raises(TransportError, match="refused the publish on tc/pump/actions/prime"):
        await client.invoke("pump__prime", {})


async def test_a_read_nobody_subscribes_to_fails_at_once():
    prop = SimpleNamespace(name="rpm")
    form = SimpleNamespace(href="mqtt://bus:1883/tc/pump/props/rpm", raw={})
    binding = MqttBinding(client_factory=lambda: _Unserved(0x10), timeout=30)
    with pytest.raises(TransportError, match="no subscriber on tc/pump/props/rpm") as caught:
        await binding.read(prop, form)
    assert caught.value.method == "READ"


async def test_a_successful_puback_waits_for_the_reply():
    bus = _Unserved(0x00, ({"stopped": True}, SAME))
    result = await MqttBinding(client_factory=lambda: bus).invoke(ACTION, FORM, {})
    assert result == {"stopped": True}


# --------------------------------------------------------------------------- #
# the gateway side
# --------------------------------------------------------------------------- #

THING_ID = "urn:demo:pump"

PUMP = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": THING_ID,
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {
        "stop": {
            "output": {"type": "object"},
            "forms": [{"href": "local://stop", "op": ["invokeaction"]}],
        }
    },
}


class _Pump:
    def stop(self) -> dict:
        return {"stopped": True}


class _Paho:
    """The slice of paho the gateway uses; records what it publishes and answers
    the CONNECT and each SUBSCRIBE as a broker would."""

    def __init__(self):
        self.sent: list[tuple[str, Any, Any]] = []
        self.on_message: Any = None
        self.on_connect: Any = None
        self.on_subscribe: Any = None
        self._mid = 0

    def connect(self, host, port):
        pass

    def loop_start(self):
        self.on_connect(self, None, {}, SimpleNamespace(value=0, is_failure=False), None)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self._mid += 1
        granted = SimpleNamespace(value=qos, is_failure=False)
        self.on_subscribe(self, None, self._mid, [granted], None)
        return 0, self._mid

    def publish(self, topic, payload, qos=0, retain=False, properties=None):
        self.sent.append((topic, json.loads(payload), properties))


async def _serve(monkeypatch, client: ThingClient, **kw) -> tuple[Gateway, _Paho]:
    paho = _Paho()
    monkeypatch.setattr("paho.mqtt.client.Client", lambda *a, **k: paho)
    gateway = Gateway(client, MqttGatewayBinding("bus:1883", **kw))
    await gateway.start()
    paho.sent.clear()  # the retained TDs
    return gateway, paho


def _request(response_topic=None, data=None, token=None) -> Properties:
    props = Properties(PacketTypes.PUBLISH)
    if response_topic is not None:
        props.ResponseTopic = response_topic
    if data is not None:
        props.CorrelationData = data
    if token is not None:
        props.UserProperty = ("authorization", f"Bearer {token}")
    return props


def _pump_client() -> ThingClient:
    return ThingClient(tds=[PUMP], bindings=[LocalBinding(_Pump())], approve_when="never")


async def test_the_gateway_copies_the_correlation_data_into_its_reply(monkeypatch):
    gateway, paho = await _serve(monkeypatch, _pump_client())
    request = _request("tc/pump/actions/stop/reply", b"c-1")
    await gateway.binding._handle("tc/pump/actions/stop", "{}", request)
    topic, body, props = paho.sent[-1]
    assert (topic, body) == ("tc/pump/actions/stop/reply", {"stopped": True})
    assert props.CorrelationData == b"c-1"
    assert not hasattr(props, "ResponseTopic")
    await gateway.aclose()


@pytest.mark.parametrize("asked", ["tc/valve/actions/open", "tc/pump/actions/stop/reply/client-7"])
async def test_the_gateway_replies_on_the_reply_topic_whatever_the_request_names(
    monkeypatch, asked
):
    gateway, paho = await _serve(monkeypatch, _pump_client())
    await gateway.binding._handle("tc/pump/actions/stop", "{}", _request(asked, b"c-1"))
    assert [topic for topic, _, _ in paho.sent] == ["tc/pump/actions/stop/reply"]
    await gateway.aclose()


async def test_a_request_without_properties_gets_a_plain_reply(monkeypatch):
    gateway, paho = await _serve(monkeypatch, _pump_client())
    await gateway.binding._handle("tc/pump/actions/stop", "{}", None)
    assert paho.sent == [("tc/pump/actions/stop/reply", {"stopped": True}, None)]
    await gateway.aclose()


async def test_a_reply_to_a_bare_topic_correlation_carries_no_properties(monkeypatch):
    gateway, paho = await _serve(monkeypatch, _pump_client())
    request = ServeRequest("pump", "stop", "invokeaction", correlation="tc/pump/actions/stop")
    await gateway.binding.reply(request, {"stopped": True})
    assert paho.sent == [("tc/pump/actions/stop/reply", {"stopped": True}, None)]
    await gateway.aclose()


class _Guard:
    async def validate(self, token: str) -> dict:
        return {"sub": "alice", "roles": ["op"]}


async def test_a_granted_subscribe_streams_under_the_request_topic(monkeypatch):
    td = {
        **PUMP,
        "events": {
            "telemetry": {"forms": [{"href": "local://telemetry", "op": ["subscribeevent"]}]}
        },
    }
    plain = ThingClient(tds=[td], bindings=[LocalBinding(_Pump())])
    grants = {"op": {(THING_ID, "telemetry", "subscribeevent")}}
    pdp = PolicyDecisionPoint(
        vocabulary=build_vocabulary(plain.things), grant_source=LocalPolicyGrantSource(grants)
    )
    client = ThingClient(tds=[td], bindings=[LocalBinding(_Pump())], pdp=pdp)
    gateway, paho = await _serve(
        monkeypatch,
        client,
        guard=_Guard(),
        broker_binds_identity=True,
        bus_security={"bus_basic": {"scheme": "basic"}},
    )
    mirrors: list[tuple] = []
    monkeypatch.setattr(gateway.binding, "_start_caller_mirror", lambda *a, **k: mirrors.append(a))
    topic = "tc/pump/events/telemetry/subscribe/alice"
    await gateway.binding._handle(topic, "", _request(data=b"c-2", token="t"))
    assert mirrors[0][2] == f"{topic}/stream"
    reply_topic, body, props = paho.sent[-1]
    assert (reply_topic, body) == (
        f"{topic}/reply",
        {"subscribed": True, "stream": f"{topic}/stream"},
    )
    assert props.CorrelationData == b"c-2"
    await gateway.aclose()
    await plain.aclose()
