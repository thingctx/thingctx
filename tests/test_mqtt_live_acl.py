# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A locked broker and a guarded gateway together: a token works only from the
login it names, and each caller reads only its own replies and streams.

Needs a Mosquitto with ``allow_anonymous false``, a ``password_file`` and the rules
``acl_file(gateways=["gw-1"], callers=["alice", "bob"], prefix="+")`` writes, named
by THINGCTX_MQTT_ACL_BROKER (``host:port``) and THINGCTX_MQTT_ACL_LOGINS (a JSON
file of login to password, with gw-1, alice and bob). Skips otherwise."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import AUDIENCE, ISSUER_V2

from thingctx import ThingClient
from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary
from thingctx.bindings import LocalBinding, MqttBinding
from thingctx.gateways import Gateway
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding
from thingctx.identity import JwtGatewayGuard

pytestmark = pytest.mark.network

BROKER = os.environ.get("THINGCTX_MQTT_ACL_BROKER", "")
LOGINS = os.environ.get("THINGCTX_MQTT_ACL_LOGINS", "")
NOT_AUTHORIZED = 0x87
PUMP_ID = "urn:test:pump"
NATIVE = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": PUMP_ID,
    "title": "pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {"stop": {"output": {"type": "object"}, "forms": [{"href": "local://stop"}]}},
    "events": {"telemetry": {"forms": [{"href": "local://telemetry"}]}},
}


@pytest.fixture(scope="module")
def logins() -> dict[str, str]:
    if not BROKER or not LOGINS:
        pytest.skip("set THINGCTX_MQTT_ACL_BROKER and THINGCTX_MQTT_ACL_LOGINS")
    return json.loads(Path(LOGINS).read_text())


def _paho(user: str, password: str) -> Any:
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    client.username_pw_set(user, password)
    host, _, port = BROKER.partition(":")
    connected = threading.Event()
    client.on_connect = lambda *_a: connected.set()
    client.connect(host, int(port or 1883))
    client.loop_start()
    assert connected.wait(5), f"{user} could not connect to {BROKER}"
    return client


def _puback(user: str, password: str, topic: str, token: str) -> int:
    """The PUBACK reason the broker gives ``user`` for a request on ``topic``."""
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    client = _paho(user, password)
    reasons: list[int] = []
    acked = threading.Event()

    def on_publish(_c: Any, _u: Any, _mid: Any, reason: Any, _p: Any) -> None:
        reasons.append(int(reason.value))
        acked.set()

    client.on_publish = on_publish
    request = Properties(PacketTypes.PUBLISH)
    request.UserProperty = ("authorization", f"Bearer {token}")
    client.publish(topic, "{}", qos=1, properties=request)
    assert acked.wait(5)
    client.loop_stop()
    client.disconnect()
    return reasons[0]


async def test_a_locked_broker_and_the_guard_keep_each_caller_to_its_own_name(keypair, logins):
    prefix = f"thingctx-test-{uuid.uuid4().hex[:8]}"
    stops: list[int] = []
    device = LocalBinding({"stop": lambda: stops.append(1) or {"stopped": len(stops)}})
    plain = ThingClient(tds=[NATIVE], bindings=[LocalBinding()])
    grants = {
        "operator": {(PUMP_ID, "stop", "invokeaction"), (PUMP_ID, "telemetry", "subscribeevent")}
    }
    pdp = PolicyDecisionPoint(
        vocabulary=build_vocabulary(plain.things), grant_source=LocalPolicyGrantSource(grants)
    )

    class LoggedIn(MqttGatewayBinding):
        def configure(self, paho: Any) -> None:
            paho.username_pw_set("gw-1", logins["gw-1"])

    guard = JwtGatewayGuard(issuers=[ISSUER_V2], audience=AUDIENCE, jwks=keypair.jwks())
    gateway = Gateway(
        ThingClient(tds=[NATIVE], bindings=[device], pdp=pdp),
        LoggedIn(
            BROKER,
            prefix=prefix,
            guard=guard,
            broker_binds_identity=True,
            bus_security={"bus_basic": {"scheme": "basic"}},
        ),
    )
    seen_by_bob: list[tuple[str, Any]] = []
    bob_watch = _paho("bob", logins["bob"])
    bob_watch.on_message = lambda _c, _u, m: seen_by_bob.append(
        (m.topic, getattr(m.properties, "UserProperty", None))
    )
    # bob's watch must be subscribed before any traffic, or seeing nothing proves nothing.
    watching = threading.Event()
    bob_watch.on_subscribe = lambda *_a: watching.set()
    bob_watch.subscribe(f"{prefix}/#", qos=1)
    assert watching.wait(5)
    await gateway.start()
    try:
        td = gateway.projected_tds["pump"]
        alice_token = keypair.mint(roles=["operator"], extra={"sub": "alice"})

        def client(user: str, token: str, tds: list[dict] | None = None) -> ThingClient:
            credentials = {"bus_basic": (user, logins[user]), "bus_bearer": token}
            return ThingClient(
                tds=tds or [td], bindings=[MqttBinding(qos=1, timeout=5.0, credentials=credentials)]
            )

        assert await client("alice", alice_token).invoke("pump__stop", {}) == {"stopped": 1}
        refused = await client("bob", alice_token).invoke("pump__stop", {})
        assert refused == {"error": "authentication required", "denied": True}
        at_alices_level = f"{prefix}/pump/actions/stop/alice"
        assert _puback("bob", logins["bob"], at_alices_level, alice_token) == NOT_AUTHORIZED
        await asyncio.sleep(0.3)
        assert stops == [1]

        alice = client("alice", alice_token)
        stream = await alice.subscribe("pump__telemetry")
        ask = {
            **{
                k: td[k]
                for k in ("@context", "id", "securityDefinitions", "security", "uriVariables")
            },
            "actions": {
                "subscribe": {
                    "output": {"type": "object"},
                    "forms": [
                        {
                            "href": td["events"]["telemetry"]["forms"][0]["href"].removesuffix(
                                "/stream"
                            )
                        }
                    ],
                }
            },
        }
        for n in (1, 2):
            answer = await client("alice", alice_token, [ask]).invoke("pump__subscribe", {})
            assert answer["stream"] == f"{prefix}/pump/events/telemetry/subscribe/alice/stream"
            await asyncio.sleep(0.3)
            device.emit("telemetry", {"n": n})
            assert await asyncio.wait_for(stream.__anext__(), 5) == {"n": n}
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(stream.__anext__(), 0.5)
        await stream.aclose()
        await asyncio.sleep(0.3)
        assert not [topic for topic, _ in seen_by_bob if "/alice" in topic]
        assert not [topic for topic, props in seen_by_bob if props]
    finally:
        await gateway.aclose()
        bob_watch.loop_stop()
        bob_watch.disconnect()
        cleaner = _paho("gw-1", logins["gw-1"])
        cleaner.publish(f"{prefix}/pump/td", b"", qos=1, retain=True).wait_for_publish(5)
        cleaner.loop_stop()
        cleaner.disconnect()
