# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""MqttBinding reliability, tested against a fake broker client (no real
broker): connect retry/backoff, QoS, re-subscribe on reconnect, reply-timeout
normalization, and graceful shutdown."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from thingctx import TransportError, parse_thing, reliability
from thingctx.auth import EnhancedAuth
from thingctx.bindings import MqttBinding


class _Msg:
    def __init__(self, payload: bytes):
        self.payload = payload


class _Info:
    rc = 0

    def wait_for_publish(self, timeout=None):  # paho MQTTMessageInfo shape
        return None


class FakeClient:
    """Mimics the slice of paho's API the binding uses. ``loop_start`` fires
    ``on_connect`` (a simulated CONNACK); ``publish`` optionally delivers a
    reply via ``on_message`` to complete a request/reply call."""

    def __init__(self, *, fail_connects: int = 0, auto_reply=None):
        self.fail_connects = fail_connects
        self.auto_reply = auto_reply
        self.subscriptions: list[tuple[str, int]] = []
        self.publishes: list[tuple[str, str, int]] = []
        self.connect_calls = 0
        self.loop_stopped = False
        self.disconnected = False
        self.on_connect = None
        self.on_message = None

    def reconnect_delay_set(self, **kw):
        pass

    def connect(self, host, port):
        self.connect_calls += 1
        self.host, self.port = host, port
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise ConnectionRefusedError("broker down")

    def loop_start(self):
        if self.on_connect:  # simulate a successful CONNACK
            self.on_connect(self, None, {}, 0)

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.publishes.append((topic, payload, qos, retain))
        if self.auto_reply is not None and self.on_message:
            self.on_message(self, None, _Msg(json.dumps(self.auto_reply).encode()))
        return _Info()

    def emit(self, payload):  # test helper: deliver an event to a subscriber
        self.on_message(self, None, _Msg(payload))


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(reliability.asyncio, "sleep", fake_sleep)
    return slept


def _form(href="mqtt://broker.local:1883/pump/cmd", *, output=True):
    # output=True → request/reply (action declares an output schema); False →
    # fire-and-forget publish.
    action = SimpleNamespace(
        name="cmd",
        output_schema={"type": "object"} if output else None,
    )
    form = SimpleNamespace(href=href, raw={})
    return action, form


def _enhanced_td():
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:x",
        "title": "x",
        "securityDefinitions": {"sc": {"scheme": "nosec"}},
        "security": ["sc"],
        "actions": {"cmd": {"forms": [{"href": "mqtt://broker/pump/cmd"}]}},
    }


async def test_v5_enhanced_auth_properties_reach_connect():
    """End to end on the v5 path: EnhancedAuth material is built into CONNECT
    properties (AuthenticationMethod/Data) and actually handed to the client's
    connect() through the reliability connect path, not merely constructed."""
    captured: dict = {}

    class _V5Fake(FakeClient):
        def connect(self, host, port, **kwargs):
            captured["properties"] = kwargs.get("properties")
            super().connect(host, port)

    thing = parse_thing(_enhanced_td())
    ea = EnhancedAuth(method="K8S-SAT", data=b"sat-token")
    inv = MqttBinding(
        credentials={"urn:dev:x": ea},
        client_factory=lambda: _V5Fake(auto_reply={"ok": True}),
    ).with_security(thing)
    action = SimpleNamespace(name="cmd", thing_id="urn:dev:x", output_schema={"type": "object"})
    form = SimpleNamespace(href="mqtt://broker/pump/cmd", raw={})

    result = await inv.invoke(action, form, {})

    assert result == {"ok": True}
    props = captured["properties"]
    assert props is not None  # the v5 properties really reached connect()
    assert props.AuthenticationMethod == "K8S-SAT"
    assert props.AuthenticationData == b"sat-token"


async def test_invoke_round_trips_reply_at_qos1():
    fake = FakeClient(auto_reply={"ok": True, "rpm": 900})
    inv = MqttBinding(client_factory=lambda: fake)
    action, form = _form()

    result = await inv.invoke(action, form, {"rpm": 900})

    assert result == {"ok": True, "rpm": 900}
    # Published to the command topic at QoS 1, subscribed to the reply topic.
    assert fake.publishes == [("pump/cmd", json.dumps({"rpm": 900}), 1, False)]
    assert ("pump/cmd/reply", 1) in fake.subscriptions


async def test_invoke_without_output_is_fire_and_forget():
    """An action with no output schema publishes and returns without awaiting
    ``<topic>/reply`` (registry mqtt.publish)."""
    fake = FakeClient()
    inv = MqttBinding(client_factory=lambda: fake)
    action, form = _form(output=False)

    result = await inv.invoke(action, form, {"value": 1, "retain": True})

    assert result == {"ok": True, "topic": "pump/cmd"}
    assert fake.publishes == [("pump/cmd", json.dumps({"value": 1}), 1, True)]
    assert fake.subscriptions == []  # never subscribed to a reply topic


async def test_subscribe_fills_broker_uri_variables():
    """ThingClient.subscribe must fill {+broker}/{+topic} before the binding
    connects (registry mqtt TD)."""
    from thingctx import ThingClient

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thingctx:mqtt",
        "title": "mqtt",
        "securityDefinitions": {"n": {"scheme": "nosec"}},
        "security": ["n"],
        "uriVariables": {"broker": {"type": "string"}},
        "events": {
            "subscribe": {
                "uriVariables": {"topic": {"type": "string"}},
                "forms": [{"href": "mqtt://{+broker}/{+topic}", "op": "subscribeevent"}],
            }
        },
    }
    fake = FakeClient()
    client = ThingClient(
        tds=[td],
        bindings=[MqttBinding(client_factory=lambda: fake)],
        approve_when="never",
    )
    stream = await client.subscribe(
        "mqtt__subscribe", {"broker": "broker.example:1883", "topic": "a/b"}
    )
    assert fake.host == "broker.example"
    assert fake.port == 1883
    assert ("a/b", 1) in fake.subscriptions
    await stream.aclose()


async def test_subscribe_preserves_hash_and_plus_wildcards_in_topic():
    """Regression: an MQTT topic filter may end in '#' or contain '+'. urlparse
    reads '#' as a URL fragment and would drop the wildcard, so the binding must
    take the topic from the raw href. A dropped '#' means the subscription matches
    nothing and the stream is silently empty."""
    from thingctx import ThingClient

    def _td(topic_var_default: str):
        return {
            "@context": "https://www.w3.org/2022/wot/td/v1.1",
            "id": "urn:thingctx:mqtt",
            "title": "mqtt",
            "securityDefinitions": {"n": {"scheme": "nosec"}},
            "security": ["n"],
            "uriVariables": {"broker": {"type": "string"}},
            "events": {
                "subscribe": {
                    "uriVariables": {"topic": {"type": "string"}},
                    "forms": [{"href": "mqtt://{+broker}/{+topic}", "op": "subscribeevent"}],
                }
            },
        }

    for wildcard in ("home/#", "sensors/+/temp", "a/b/#"):
        fake = FakeClient()
        client = ThingClient(
            tds=[_td(wildcard)],
            bindings=[MqttBinding(client_factory=lambda f=fake: f)],
            approve_when="never",
        )
        stream = await client.subscribe("mqtt__subscribe", {"broker": "b:1883", "topic": wildcard})
        subbed = [t for t, _ in fake.subscriptions]
        assert wildcard in subbed, f"{wildcard!r} not subscribed; got {subbed}"
        await stream.aclose()


async def test_subscribe_pushes_each_message_in_order_as_it_arrives():
    """The subscribe stream is push, not poll: every message the broker delivers
    reaches the async iterator, in arrival order, one per broker delivery. The
    consumer awaits between messages and is resumed on each on_message, so a
    later message published after an idle gap still arrives on its own delivery,
    not batched with an earlier one."""
    from thingctx import ThingClient

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thingctx:mqtt",
        "title": "mqtt",
        "securityDefinitions": {"n": {"scheme": "nosec"}},
        "security": ["n"],
        "uriVariables": {"broker": {"type": "string"}},
        "events": {
            "subscribe": {
                "uriVariables": {"topic": {"type": "string"}},
                "forms": [{"href": "mqtt://{+broker}/{+topic}", "op": "subscribeevent"}],
            }
        },
    }
    fake = FakeClient()
    client = ThingClient(
        tds=[td],
        bindings=[MqttBinding(client_factory=lambda: fake)],
        approve_when="never",
    )
    stream = await client.subscribe(
        "mqtt__subscribe", {"broker": "b:1883", "topic": "sensors/temp"}
    )

    received: list = []

    async def consume():
        async for msg in stream:
            received.append(msg)
            if len(received) == 3:
                break

    task = asyncio.create_task(consume())
    # Let the consumer reach its first idle await before anything is emitted, so
    # a delivery genuinely wakes it (rather than finding a message already queued).
    await asyncio.sleep(0)
    for i in range(3):
        fake.emit(json.dumps({"n": i}).encode())
        # Yield so the consumer runs and drains THIS delivery before the next,
        # proving one-message-per-delivery ordering rather than a batch drain.
        await asyncio.sleep(0)

    await asyncio.wait_for(task, timeout=1)
    await stream.aclose()
    # Every delivery surfaced, in order, decoded from JSON.
    assert received == [{"n": 0}, {"n": 1}, {"n": 2}]


async def test_connect_is_retried_with_backoff(no_sleep):
    fake = FakeClient(fail_connects=2, auto_reply={"ok": True})
    inv = MqttBinding(client_factory=lambda: fake, connect_retries=3, backoff=0.1)
    action, form = _form()

    result = await inv.invoke(action, form, {})

    assert result == {"ok": True}
    assert fake.connect_calls == 3  # failed twice, succeeded on the third
    # Exponential backoff between tries (base 0.1, 0.2) within the jitter band.
    assert len(no_sleep) == 2
    assert 0.1 <= no_sleep[0] <= 0.2
    assert 0.2 <= no_sleep[1] <= 0.3
    assert no_sleep[1] > no_sleep[0]


async def test_connect_exhaustion_raises_transport_error(no_sleep):
    fake = FakeClient(fail_connects=99)
    inv = MqttBinding(client_factory=lambda: fake, connect_retries=2, backoff=0)
    action, form = _form()

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert ei.value.method == "CONNECT"
    assert ei.value.attempts == 3
    assert isinstance(ei.value.__cause__, ConnectionRefusedError)
    # A failed connect must not leak the client: it is torn down.
    assert fake.loop_stopped and fake.disconnected


async def test_connect_torn_down_and_retried_after_connack_timeout(no_sleep):
    """A CONNACK that never arrives on the first attempt times out, tears the
    connection fully down (loop_stop + disconnect), then a fresh attempt, with
    its own connect event, succeeds."""

    class SlowConnack:
        def __init__(self):
            self.loop_starts = self.stops = self.disconnects = 0
            self.on_connect = self.on_message = None

        def reconnect_delay_set(self, **kw):
            pass

        def connect(self, host, port):
            pass

        def loop_start(self):
            self.loop_starts += 1
            if self.loop_starts >= 2 and self.on_connect:  # CONNACK only on retry
                self.on_connect(self, None, {}, 0)

        def loop_stop(self):
            self.stops += 1

        def disconnect(self):
            self.disconnects += 1

        def subscribe(self, topic, qos=0):
            pass

        def publish(self, topic, payload, qos=0, retain=False):
            if self.on_message:
                self.on_message(self, None, _Msg(json.dumps({"ok": True}).encode()))
            return _Info()

    fake = SlowConnack()
    inv = MqttBinding(
        client_factory=lambda: fake, connect_retries=2, backoff=0, connect_timeout=0.05
    )
    action, form = _form()

    result = await inv.invoke(action, form, {})

    assert result == {"ok": True}
    assert fake.loop_starts == 2  # first attempt timed out, retry succeeded
    assert fake.stops >= 1 and fake.disconnects >= 1  # torn down between attempts


async def test_reply_timeout_is_normalized():
    fake = FakeClient(auto_reply=None)  # never replies
    inv = MqttBinding(client_factory=lambda: fake, timeout=0.05)
    action, form = _form()

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert ei.value.method == "PUBLISH"
    assert "no reply" in ei.value.detail


async def test_resubscribe_on_reconnect():
    """paho does not resubscribe after a reconnect; the binding's on_connect
    must, so a second CONNACK re-establishes the subscription."""
    fake = FakeClient(auto_reply={"ok": True})
    inv = MqttBinding(client_factory=lambda: fake)
    action, form = _form()
    await inv.invoke(action, form, {})

    subs_after_first = [t for (t, _q) in fake.subscriptions]
    assert "pump/cmd/reply" in subs_after_first

    # Simulate a dropped connection re-establishing (paho fires on_connect again).
    fake.on_connect(fake, None, {}, 0)
    assert fake.subscriptions.count(("pump/cmd/reply", 1)) == 2


async def test_invoke_shuts_down_client():
    fake = FakeClient(auto_reply={"ok": True})
    inv = MqttBinding(client_factory=lambda: fake)
    action, form = _form()

    await inv.invoke(action, form, {})

    assert fake.loop_stopped and fake.disconnected


async def test_subscribe_streams_and_decodes():
    fake = FakeClient()
    inv = MqttBinding(client_factory=lambda: fake, qos=1)
    _action, form = _form("mqtt://broker.local/pump/events")

    stream = await inv.subscribe("pump__overheat", form)
    assert ("pump/events", 1) in fake.subscriptions  # subscribed at QoS 1

    fake.emit(json.dumps({"temp": 98}).encode())
    fake.emit(b"not-json")

    first = await stream.__anext__()
    second = await stream.__anext__()
    assert first == {"temp": 98}
    assert second == "not-json"  # non-JSON falls back to text

    await stream.aclose()
    assert fake.loop_stopped and fake.disconnected
