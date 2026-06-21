"""Live MQTT reliability against a real broker (e.g. a local Mosquitto).

These complement the fake-broker logic tests with a real wire round-trip:
actual CONNECT/SUBACK/PUBACK, real QoS-1 delivery, and real connection-refused
handling. They are marked ``network`` (deselect with ``-m 'not network'``) and
skip automatically if no broker is reachable.

    pytest -m network                 # run them
    THINGCTX_MQTT_BROKER=host:1883 pytest -m network
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from types import SimpleNamespace

import pytest

from thingctx import TransportError
from thingctx.invokers import MqttInvoker

pytestmark = pytest.mark.network

BROKER = os.environ.get("THINGCTX_MQTT_BROKER", "localhost:1883")
HOST, _, _port = BROKER.partition(":")
PORT = int(_port or "1883")


def _reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


@pytest.fixture(scope="module", autouse=True)
def _require_broker():
    if not _reachable(HOST, PORT):
        pytest.skip(f"no MQTT broker reachable at {HOST}:{PORT}")


def _mk_paho(cid: str = ""):
    import paho.mqtt.client as mqtt

    try:
        return mqtt.Client(client_id=cid)
    except TypeError:  # paho v2
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid)


def _form(topic: str):
    action = SimpleNamespace(name="cmd")
    return action, SimpleNamespace(href=f"mqtt://{HOST}:{PORT}/{topic}", raw={})


async def test_live_subscribe_receives_real_publish():
    topic = f"thingctx/test/{uuid.uuid4().hex}/events"
    inv = MqttInvoker(qos=1)
    _action, form = _form(topic)

    stream = await inv.subscribe("evt", form)
    try:
        await asyncio.sleep(0.3)  # let the SUBACK settle before publishing
        pub = _mk_paho()
        pub.connect(HOST, PORT)
        pub.loop_start()
        try:
            pub.publish(topic, json.dumps({"temp": 98}), qos=1)
            msg = await asyncio.wait_for(stream.__anext__(), timeout=5.0)
        finally:
            pub.loop_stop()
            pub.disconnect()
        assert msg == {"temp": 98}
    finally:
        await stream.aclose()


async def test_live_invoke_round_trip_with_echo_responder():
    cmd = f"thingctx/test/{uuid.uuid4().hex}/cmd"
    reply = f"{cmd}/reply"

    # A stand-in device: subscribe to the command topic, echo each payload back
    # on the reply topic. This is what a real Thing's MQTT side would do.
    responder = _mk_paho()
    responder.on_connect = lambda c, u, f, rc, *a: c.subscribe(cmd, qos=1)
    responder.on_message = lambda c, u, m: c.publish(reply, m.payload, qos=1)
    responder.connect(HOST, PORT)
    responder.loop_start()
    try:
        time.sleep(0.3)  # responder subscription active before we publish
        inv = MqttInvoker(qos=1, timeout=5.0)
        action, form = _form(cmd)

        result = await inv.invoke(action, form, {"rpm": 1234})

        assert result == {"rpm": 1234}
    finally:
        responder.loop_stop()
        responder.disconnect()


async def test_live_connection_refused_is_normalized():
    inv = MqttInvoker(connect_retries=0, connect_timeout=1.0, backoff=0)
    # A port with nothing listening: a real refused/timed-out connect.
    action = SimpleNamespace(name="cmd")
    form = SimpleNamespace(href=f"mqtt://{HOST}:12399/thingctx/test/dead", raw={})

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert ei.value.method == "CONNECT"
