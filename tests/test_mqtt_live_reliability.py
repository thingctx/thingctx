# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
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
from conftest import AUDIENCE, ISSUER_V2

from thingctx import ThingClient, TransportError
from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary
from thingctx.bindings import LocalBinding, MqttBinding
from thingctx.gateways import Gateway
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding
from thingctx.identity import JwtGatewayGuard

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


def _form(topic: str, *, output=True):
    action = SimpleNamespace(name="cmd", output_schema={"type": "object"} if output else None)
    return action, SimpleNamespace(href=f"mqtt://{HOST}:{PORT}/{topic}", raw={})


async def test_live_subscribe_receives_real_publish():
    topic = f"thingctx/test/{uuid.uuid4().hex}/events"
    inv = MqttBinding(qos=1)
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
        inv = MqttBinding(qos=1, timeout=5.0)
        action, form = _form(cmd)

        result = await inv.invoke(action, form, {"rpm": 1234})

        assert result == {"rpm": 1234}
    finally:
        responder.loop_stop()
        responder.disconnect()


async def test_live_overlapping_calls_each_get_their_own_reply():
    """Two calls in flight on one action, answered out of order by the MQTT
    gateway: each gets its own reply, not the first one on the reply topic."""
    from thingctx import ThingClient
    from thingctx.bindings import LocalBinding
    from thingctx.gateways import Gateway
    from thingctx.gateways.builtin.mqtt import MqttGatewayBinding

    async def echo(n: int, wait: float) -> dict:
        await asyncio.sleep(wait)
        return {"n": n}

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:test:echo",
        "title": "Echo",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {
            "echo": {
                "input": {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}, "wait": {"type": "number"}},
                },
                "output": {"type": "object"},
                "forms": [{"href": "local://echo", "op": ["invokeaction"]}],
            }
        },
    }
    prefix = f"thingctx/test/{uuid.uuid4().hex}"
    native = ThingClient(tds=[td], bindings=[LocalBinding({"echo": echo})], approve_when="never")
    gateway = Gateway(native, MqttGatewayBinding(BROKER, prefix=prefix))
    await gateway.start()
    try:
        inv = MqttBinding(qos=1, timeout=5.0)
        action, form = _form(f"{prefix}/echo/actions/echo")
        slow = asyncio.ensure_future(inv.invoke(action, form, {"n": 1, "wait": 1.0}))
        await asyncio.sleep(0.3)  # the slow call is waiting before the fast one starts
        fast = await inv.invoke(action, form, {"n": 2, "wait": 0})
        assert (await slow, fast) == ({"n": 1}, {"n": 2})
    finally:
        await gateway.aclose()
        # The gateway leaves its TD retained; clear it so the broker is left as found.
        cleaner = _mk_paho()
        cleaner.connect(HOST, PORT)
        cleaner.loop_start()
        cleaner.publish(f"{prefix}/echo/td", b"", qos=1, retain=True).wait_for_publish(5)
        cleaner.loop_stop()
        cleaner.disconnect()


async def test_live_request_nobody_serves_fails_at_once():
    # MQTT 5 lets a broker answer a publish no subscription matched with PUBACK
    # reason 0x10 (section 3.4.2.1); Mosquitto does.
    inv = MqttBinding(qos=1, timeout=10.0)
    action, form = _form(f"thingctx/test/{uuid.uuid4().hex}/unserved")
    started = time.monotonic()
    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})
    if "no reply" in ei.value.detail:
        pytest.skip("this broker does not report PUBACK reason 0x10")
    assert "no subscriber" in ei.value.detail
    assert time.monotonic() - started < 3


def _pump(slug: str) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": f"urn:test:{slug}",
        "title": slug,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {
            "stop": {
                "output": {"type": "object"},
                "forms": [{"href": "local://stop", "op": ["invokeaction"]}],
            }
        },
    }


async def test_live_guarded_gateway_runs_each_request_as_its_caller(keypair):
    prefix = f"thingctx-test-{uuid.uuid4().hex[:8]}"
    native = _pump("pump")
    plain = ThingClient(tds=[native], bindings=[LocalBinding()])
    pdp = PolicyDecisionPoint(
        vocabulary=build_vocabulary(plain.things),
        grant_source=LocalPolicyGrantSource(
            {"operator": {("urn:test:pump", "stop", "invokeaction")}}
        ),
    )
    local = LocalBinding()
    local.register_thing("pump", {"stop": lambda: {"by": "the pump"}})
    guard = JwtGatewayGuard(issuers=[ISSUER_V2], audience=AUDIENCE, jwks=keypair.jwks())
    gateway = Gateway(
        ThingClient(tds=[native], bindings=[local], pdp=pdp),
        MqttGatewayBinding(
            BROKER,
            prefix=prefix,
            guard=guard,
            broker_binds_identity=True,
            bus_security={"bus_basic": {"scheme": "basic"}},
        ),
    )
    await gateway.start()
    try:
        td = gateway.projected_tds["pump"]
        assert td["security"] == ["bus_basic", "bus_bearer"]

        async def call(roles: list[str]) -> dict:
            token = keypair.mint(roles=roles)
            credentials = {"bus_basic": ("caller-object-id", "unused"), "bus_bearer": token}
            binding = MqttBinding(qos=1, timeout=5.0, credentials=credentials)
            return await ThingClient(tds=[td], bindings=[binding]).invoke("pump__stop", {})

        assert await call(["operator"]) == {"by": "the pump"}
        assert (await call(["viewer"])).get("denied") is True
    finally:
        await gateway.aclose()
        cleanup = _mk_paho()
        cleanup.connect(HOST, PORT)
        cleanup.loop_start()
        cleanup.publish(f"{prefix}/pump/td", b"", qos=1, retain=True).wait_for_publish(5)
        cleanup.loop_stop()
        cleanup.disconnect()


async def test_live_connection_refused_is_normalized():
    inv = MqttBinding(connect_retries=0, connect_timeout=1.0, backoff=0)
    # A port with nothing listening: a real refused/timed-out connect.
    action = SimpleNamespace(name="cmd")
    form = SimpleNamespace(href=f"mqtt://{HOST}:12399/thingctx/test/dead", raw={})

    with pytest.raises(TransportError) as ei:
        await inv.invoke(action, form, {})

    assert ei.value.method == "CONNECT"
