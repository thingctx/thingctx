# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Closing a thingctx client disconnects every MQTT subscription it opened,
including one whose stream was never read, without blocking the event loop."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from mqtt_fakes import Broker, FakeClient

from thingctx import ThingClient
from thingctx.bindings import MqttBinding

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:pump",
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "events": {"alarm": {"forms": [{"href": "mqtt://bus:1883/tc/pump/events/alarm"}]}},
}


class SlowStop(FakeClient):
    """A client whose network thread takes a while to end once disconnected."""

    def __init__(self, broker: Broker) -> None:
        super().__init__(broker)
        self.steps: list[str] = []

    def disconnect(self) -> None:
        self.steps.append("disconnect")
        super().disconnect()

    def loop_stop(self) -> None:
        self.steps.append("loop_stop")
        self.stopped_on = threading.current_thread()
        time.sleep(0.3)


def _client(broker: Broker, kind: type[FakeClient] = FakeClient) -> tuple[ThingClient, list[Any]]:
    made: list[Any] = []

    def factory() -> FakeClient:
        made.append(kind(broker))
        return made[-1]

    return ThingClient(tds=[TD], bindings=[MqttBinding(client_factory=factory, timeout=1)]), made


async def test_closing_the_client_disconnects_a_subscription_never_read() -> None:
    client, made = _client(Broker())
    await client.subscribe("pump__alarm")
    assert made[0].connected

    await client.aclose()

    assert not made[0].connected


async def test_a_stream_read_and_closed_disconnects_on_its_own() -> None:
    broker = Broker()
    client, made = _client(broker)
    stream = await client.subscribe("pump__alarm")
    broker.route("tc/pump/events/alarm", '{"level": "high"}', False, None)

    assert await asyncio.wait_for(stream.__anext__(), 1) == {"level": "high"}
    await stream.aclose()

    assert not made[0].connected
    await client.aclose()


async def test_closing_disconnects_first_and_does_not_block_the_loop() -> None:
    client, made = _client(Broker(), SlowStop)
    stream = await client.subscribe("pump__alarm")
    await client.subscribe("pump__alarm")
    ticks = 0

    async def beat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beating = asyncio.ensure_future(beat())
    await asyncio.sleep(0)
    await stream.aclose()
    await client.aclose()
    beating.cancel()

    # Two joins of 0.3 s each; a blocked loop would not beat at all meanwhile.
    assert ticks > 20
    for fake in made:
        assert fake.steps[:2] == ["disconnect", "loop_stop"]
        assert fake.stopped_on is not threading.main_thread()
