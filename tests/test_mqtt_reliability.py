"""MqttInvoker reliability, tested against a fake broker client (no real
broker): connect retry/backoff, QoS, re-subscribe on reconnect, reply-timeout
normalization, and graceful shutdown."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import thingctx.reliability as reliability
from thingctx import TransportError, parse_thing
from thingctx.auth import EnhancedAuth
from thingctx.invokers import MqttInvoker


class _Msg:
    def __init__(self, payload: bytes):
        self.payload = payload


class _Info:
    rc = 0

    def wait_for_publish(self, timeout=None):  # paho MQTTMessageInfo shape
        return None


class FakeClient:
    """Mimics the slice of paho's API the invoker uses. ``loop_start`` fires
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

    def publish(self, topic, payload, qos=0):
        self.publishes.append((topic, payload, qos))
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


def _form(href="mqtt://broker.local:1883/pump/cmd"):
    action = SimpleNamespace(name="cmd")
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
    connect() through the reliability connect path -- not merely constructed."""
    captured: dict = {}

    class _V5Fake(FakeClient):
        def connect(self, host, port, **kwargs):
            captured["properties"] = kwargs.get("properties")
            super().connect(host, port)

    thing = parse_thing(_enhanced_td())
    ea = EnhancedAuth(method="K8S-SAT", data=b"sat-token")
    inv = MqttInvoker(
        credentials={"urn:dev:x": ea},
        client_factory=lambda: _V5Fake(auto_reply={"ok": True}),
    ).with_security(thing)
    action = SimpleNamespace(name="cmd", thing_id="urn:dev:x")
    form = SimpleNamespace(href="mqtt://broker/pump/cmd", raw={})

    result = await inv.invoke(action, form, {})

    assert result == {"ok": True}
    props = captured["properties"]
    assert props is not None  # the v5 properties really reached connect()
    assert props.AuthenticationMethod == "K8S-SAT"
    assert props.AuthenticationData == b"sat-token"


async def test_invoke_round_trips_reply_at_qos1():
    fake = FakeClient(auto_reply={"ok": True, "rpm": 900})
    inv = MqttInvoker(client_factory=lambda: fake)
    action, form = _form()

    result = await inv.invoke(action, form, {"rpm": 900})

    assert result == {"ok": True, "rpm": 900}
    # Published to the command topic at QoS 1, subscribed to the reply topic.
    assert fake.publishes == [("pump/cmd", json.dumps({"rpm": 900}), 1)]
    assert ("pump/cmd/reply", 1) in fake.subscriptions


async def test_connect_is_retried_with_backoff(no_sleep):
    fake = FakeClient(fail_connects=2, auto_reply={"ok": True})
    inv = MqttInvoker(client_factory=lambda: fake, connect_retries=3, backoff=0.1)
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
    inv = MqttInvoker(client_factory=lambda: fake, connect_retries=2, backoff=0)
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
    connection fully down (loop_stop + disconnect), then a fresh attempt -- with
    its own connect event -- succeeds."""

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

        def publish(self, topic, payload, qos=0):
            if self.on_message:
                self.on_message(self, None, _Msg(json.dumps({"ok": True}).encode()))
            return _Info()

    fake = SlowConnack()
    inv = MqttInvoker(
        client_factory=lambda: fake, connect_retries=2, backoff=0, connect_timeout=0.05
    )
    action, form = _form()

    result = await inv.invoke(action, form, {})

    assert result == {"ok": True}
    assert fake.loop_starts == 2  # first attempt timed out, retry succeeded
    assert fake.stops >= 1 and fake.disconnects >= 1  # torn down between attempts


async def test_reply_timeout_is_normalized():
    fake = FakeClient(auto_reply=None)  # never replies
    inv = MqttInvoker(client_factory=lambda: fake, timeout=0.05)
    action, form = _form()

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert ei.value.method == "PUBLISH"
    assert "no reply" in ei.value.detail


async def test_resubscribe_on_reconnect():
    """paho does not resubscribe after a reconnect; the invoker's on_connect
    must, so a second CONNACK re-establishes the subscription."""
    fake = FakeClient(auto_reply={"ok": True})
    inv = MqttInvoker(client_factory=lambda: fake)
    action, form = _form()
    await inv.invoke(action, form, {})

    subs_after_first = [t for (t, _q) in fake.subscriptions]
    assert "pump/cmd/reply" in subs_after_first

    # Simulate a dropped connection re-establishing (paho fires on_connect again).
    fake.on_connect(fake, None, {}, 0)
    assert fake.subscriptions.count(("pump/cmd/reply", 1)) == 2


async def test_invoke_shuts_down_client():
    fake = FakeClient(auto_reply={"ok": True})
    inv = MqttInvoker(client_factory=lambda: fake)
    action, form = _form()

    await inv.invoke(action, form, {})

    assert fake.loop_stopped and fake.disconnected


async def test_subscribe_streams_and_decodes():
    fake = FakeClient()
    inv = MqttInvoker(client_factory=lambda: fake, qos=1)
    _action, form = _form("mqtt://broker.local/pump/events")

    stream = await inv.subscribe("pump.overheat", form)
    assert ("pump/events", 1) in fake.subscriptions  # subscribed at QoS 1

    fake.emit(json.dumps({"temp": 98}).encode())
    fake.emit(b"not-json")

    first = await stream.__anext__()
    second = await stream.__anext__()
    assert first == {"temp": 98}
    assert second == "not-json"  # non-JSON falls back to text

    await stream.aclose()
    assert fake.loop_stopped and fake.disconnected
