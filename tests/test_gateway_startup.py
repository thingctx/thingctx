# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A gateway reports itself started only once the broker has
accepted its connection and acknowledged every subscription, so a request sent
right after start finds its topic served. A broker that refuses either, or never
answers, fails the start instead of leaving a gateway that serves nothing. Each new
connection after start is served again, since the session ends with the old one."""

from __future__ import annotations

import asyncio
import socket
import time

import pytest
from mqtt_fakes import NOT_AUTHORIZED, OK, Broker, FakeClient

from thingctx import ThingClient
from thingctx.bindings import LocalBinding
from thingctx.gateways import Gateway
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding

NATIVE = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:pump",
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {"stop": {"output": {"type": "object"}, "forms": [{"href": "local://stop"}]}},
    "properties": {"rpm": {"type": "integer", "forms": [{"href": "local://rpm"}]}},
}


def _gateway(monkeypatch, broker: Broker, **kw) -> Gateway:
    monkeypatch.setattr("paho.mqtt.client.Client", lambda *a, **k: FakeClient(broker))
    device = LocalBinding({"stop": lambda: {"stopped": True}, "rpm": lambda: 1200})
    return Gateway(
        ThingClient(tds=[NATIVE], bindings=[device]), MqttGatewayBinding("bus:1883", **kw)
    )


async def test_start_returns_only_once_every_subscription_is_acknowledged(monkeypatch) -> None:
    broker = Broker()
    broker.held = []
    gateway = _gateway(monkeypatch, broker)

    starting = asyncio.ensure_future(gateway.start())
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(broker.held) == 2

    broker.held.pop(0)()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not starting.done()

    broker.release()
    await asyncio.wait_for(starting, 1)
    await gateway.aclose()


async def test_a_subscription_the_broker_refuses_fails_the_start(monkeypatch) -> None:
    broker = Broker()
    broker.denied = {"tc/pump/actions/stop"}
    gateway = _gateway(monkeypatch, broker)

    with pytest.raises(
        ConnectionError, match="refused the gateway's subscription to tc/pump/actions/stop"
    ):
        await gateway.start()


async def test_a_connection_the_broker_refuses_fails_the_start(monkeypatch) -> None:
    broker = Broker()
    broker.refuse_connect = True
    gateway = _gateway(monkeypatch, broker)

    with pytest.raises(ConnectionError, match="refused the gateway's connection"):
        await gateway.start()


async def test_a_broker_that_never_answers_fails_the_start_after_the_timeout(monkeypatch) -> None:
    broker = Broker()
    broker.silent = True
    gateway = _gateway(monkeypatch, broker, connect_timeout=0.05)

    with pytest.raises(ConnectionError, match="did not answer within 0.05 s"):
        await gateway.start()


def test_the_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="connect_timeout must be positive"):
        MqttGatewayBinding("bus:1883", connect_timeout=0)


async def test_a_silent_broker_fails_the_start_without_stalling_the_event_loop() -> None:
    # A listener that completes the TCP handshake and never answers the CONNECT.
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    port = silent.getsockname()[1]
    device = LocalBinding({"stop": lambda: {"stopped": True}, "rpm": lambda: 1200})
    binding = MqttGatewayBinding(f"127.0.0.1:{port}", connect_timeout=0.3)
    gateway = Gateway(ThingClient(tds=[NATIVE], bindings=[device]), binding)
    gaps: list[float] = []

    async def tick() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.ensure_future(tick())
    started = time.monotonic()
    try:
        with pytest.raises(ConnectionError, match="did not answer within 0.3 s"):
            await gateway.start()
    finally:
        ticker.cancel()
        silent.close()

    assert time.monotonic() - started < 3
    assert max(gaps) < 0.5


async def test_a_failed_start_leaves_nothing_running_so_a_retry_serves_once(monkeypatch) -> None:
    broker = Broker()
    broker.refuse_connect = True
    monkeypatch.setattr("paho.mqtt.client.Client", lambda *a, **k: FakeClient(broker))
    td = NATIVE | {"events": {"alarm": {"forms": [{"href": "local://alarm"}]}}}
    device = LocalBinding({"stop": lambda: {"stopped": True}, "rpm": lambda: 1200})
    gateway = Gateway(ThingClient(tds=[td], bindings=[device]), MqttGatewayBinding("bus:1883"))
    with pytest.raises(ConnectionError, match="refused the gateway's connection"):
        await gateway.start()

    broker.refuse_connect = False
    await gateway.start()
    seen: list[bytes] = []
    watcher = FakeClient(broker)
    watcher.on_message = lambda _c, _u, msg: seen.append(msg.payload)
    watcher.connect()
    watcher.subscribe("tc/pump/events/alarm")
    for _ in range(5):
        await asyncio.sleep(0)
    device.emit("alarm", {"level": "high"})
    for _ in range(5):
        await asyncio.sleep(0)

    assert len(seen) == 1
    await gateway.aclose()


async def test_a_cancelled_start_leaves_the_bus(monkeypatch) -> None:
    broker = Broker()
    broker.held = []
    gateway = _gateway(monkeypatch, broker)
    starting = asyncio.ensure_future(gateway.start())
    for _ in range(5):
        await asyncio.sleep(0)

    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert not broker.clients[0].connected
    assert not broker.route("tc/pump/actions/stop", "{}", False, None)


async def test_an_unreachable_broker_fails_the_start_within_the_timeout() -> None:
    # TEST-NET-1 (RFC 5737) routes nowhere, so the TCP connect never completes.
    device = LocalBinding({"stop": lambda: {"stopped": True}, "rpm": lambda: 1200})
    binding = MqttGatewayBinding("192.0.2.1:1883", connect_timeout=0.3)
    gateway = Gateway(ThingClient(tds=[NATIVE], bindings=[device]), binding)
    started = time.monotonic()

    with pytest.raises(ConnectionError, match="could not reach 192.0.2.1:1883"):
        await gateway.start()

    assert time.monotonic() - started < 2


async def test_a_description_is_retained_only_after_its_topics_are_subscribed(monkeypatch) -> None:
    broker = Broker()
    seen: list[tuple[str, str]] = []

    class Recording(FakeClient):
        def subscribe(self, topic: str, qos: int = 0) -> tuple[int, int]:
            seen.append(("subscribe", topic))
            return super().subscribe(topic, qos)

        def publish(self, topic, payload, qos=0, retain=False, properties=None):
            if topic.endswith("/td"):
                seen.append(("retain", topic))
            return super().publish(topic, payload, qos, retain, properties)

    gateway = _gateway(monkeypatch, broker)
    monkeypatch.setattr("paho.mqtt.client.Client", lambda *a, **k: Recording(broker))
    await gateway.start()

    assert seen == [
        ("subscribe", "tc/pump/actions/stop"),
        ("subscribe", "tc/pump/props/rpm"),
        ("retain", "tc/pump/td"),
    ]
    await gateway.aclose()


async def test_a_new_connection_after_start_is_served_again(monkeypatch) -> None:
    broker = Broker()
    gateway = _gateway(monkeypatch, broker)
    await gateway.start()
    paho = broker.clients[0]
    # A restarted broker keeps no subscription and no retained message; paho then
    # reports the new connection.
    paho.subs.clear()
    broker.retained.clear()

    paho.on_connect(paho, None, {}, NOT_AUTHORIZED, None)
    assert not paho.subs
    paho.on_connect(paho, None, {}, OK, None)

    assert broker.route("tc/pump/actions/stop", "{}", False, None)
    assert broker.record("tc/pump/td")["id"] == "urn:demo:pump"
    await gateway.aclose()
