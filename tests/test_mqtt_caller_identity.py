# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A caller's bearer token travels with each MQTT request when the Thing
Description names its carrier, and a guarded gateway declares that carrier and
runs every request as the caller who sent it."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import AUDIENCE, ISSUER_V2
from mqtt_fakes import Broker, FakeClient
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from thingctx import ThingClient
from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary
from thingctx.bindings import LocalBinding, MqttBinding
from thingctx.gateways import Gateway
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding
from thingctx.identity import JwtGatewayGuard
from thingctx.models.sosa import recorder
from thingctx.testing import assert_gateway_binding_contract, gateway_binding_capabilities

PUMP_ID = "urn:demo:pump"
CARRIER = {"scheme": "bearer", "in": "header", "name": "authorization"}


def _pump(forms: dict[str, str], security: dict | None = None) -> dict:
    td: dict[str, Any] = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": PUMP_ID,
        "title": "Pump",
        "securityDefinitions": security or {"nosec_sc": {"scheme": "nosec"}},
        "security": list(security or {"nosec_sc": None}),
        "actions": {
            "stop": {"output": {"type": "object"}, "forms": [{"href": forms["stop"]}]},
            "prime": {"forms": [{"href": forms["prime"]}]},
        },
    }
    return td


BUS = {
    "stop": "mqtt://bus:1883/tc/pump/actions/stop",
    "prime": "mqtt://bus:1883/tc/pump/actions/prime",
}


class _Responder:
    """A Thing on the bus that answers every action and keeps what each request carried."""

    def __init__(self, broker: Broker) -> None:
        self.seen: list[Any] = []
        self.client = FakeClient(broker)
        self.client.on_message = self._on_message
        self.client.connect()
        self.client.subscribe("tc/pump/actions/+")

    def _on_message(self, _c: Any, _u: Any, msg: Any) -> None:
        self.seen.append(msg.properties)
        if msg.topic.endswith("/reply"):
            return
        answer = {"stopped": True} if msg.topic.endswith("/stop") else {"primed": True}
        reply = Properties(PacketTypes.PUBLISH)
        reply.CorrelationData = msg.properties.CorrelationData
        self.client.publish(f"{msg.topic}/reply", json.dumps(answer), properties=reply)


def _consumer(broker: Broker, td: dict, **kw: Any) -> tuple[ThingClient, list[FakeClient]]:
    made: list[FakeClient] = []

    def factory() -> FakeClient:
        made.append(FakeClient(broker))
        return made[-1]

    return ThingClient(
        tds=[td], bindings=[MqttBinding(client_factory=factory, timeout=1, **kw)]
    ), made


async def test_a_named_bearer_scheme_sends_the_token_with_each_request() -> None:
    broker = Broker()
    device = _Responder(broker)
    client, made = _consumer(
        broker, _pump(BUS, {"bus_bearer": CARRIER}), credentials={PUMP_ID: "TOK"}
    )
    assert await client.invoke("pump__stop", {}) == {"stopped": True}
    assert device.seen[0].UserProperty == [("authorization", "Bearer TOK")]
    assert made[0].auth is None


async def test_a_bearer_scheme_that_names_no_carrier_stays_the_connection_password() -> None:
    broker = Broker()
    device = _Responder(broker)
    security = {"bus_bearer": {"scheme": "bearer"}}
    client, made = _consumer(broker, _pump(BUS, security), credentials={PUMP_ID: "TOK"})
    assert await client.invoke("pump__stop", {}) == {"stopped": True}
    assert made[0].auth == ("", "TOK")
    assert not getattr(device.seen[0], "UserProperty", None)


async def test_a_login_keyed_by_thing_id_is_never_sent_as_the_token() -> None:
    broker = Broker()
    device = _Responder(broker)
    security = {"bus_basic": {"scheme": "basic"}, "bus_bearer": CARRIER}
    client, made = _consumer(
        broker, _pump(BUS, security), credentials={PUMP_ID: ("alice", "s3cret")}
    )
    assert await client.invoke("pump__stop", {}) == {"stopped": True}
    assert made[0].auth == ("alice", "s3cret")
    assert not getattr(device.seen[0], "UserProperty", None)


async def test_a_command_without_an_output_carries_the_caller_and_awaits_the_answer() -> None:
    broker = Broker()
    device = _Responder(broker)
    client, _ = _consumer(broker, _pump(BUS, {"bus_bearer": CARRIER}), credentials={PUMP_ID: "TOK"})
    assert await client.invoke("pump__prime", {}) == {"primed": True}
    assert device.seen[0].UserProperty == [("authorization", "Bearer TOK")]


def _guard(keypair: Any) -> JwtGatewayGuard:
    return JwtGatewayGuard(issuers=[ISSUER_V2], audience=AUDIENCE, jwks=keypair.jwks())


async def test_a_guarded_gateway_declares_the_carrier_and_an_open_one_declares_nothing(
    monkeypatch, keypair
) -> None:
    broker = Broker()
    monkeypatch.setattr("paho.mqtt.client.Client", lambda *a, **k: FakeClient(broker))
    native = _pump({"stop": "local://stop", "prime": "local://prime"})
    guarded = Gateway(
        ThingClient(tds=[native], bindings=[LocalBinding({})]),
        MqttGatewayBinding(
            "bus:1883",
            guard=_guard(keypair),
            broker_binds_identity=True,
            bus_security={"bus_basic": {"scheme": "basic"}},
        ),
    )
    await guarded.start()
    td = guarded.projected_tds["pump"]
    assert td["security"] == ["bus_basic", "bus_bearer"]
    scheme = td["securityDefinitions"]["bus_bearer"]
    assert {k: scheme[k] for k in ("scheme", "in", "name", "format", "alg")} == {
        "scheme": "bearer",
        "in": "header",
        "name": "authorization",
        "format": "jwt",
        "alg": "RS256",
    }
    open_gateway = Gateway(
        ThingClient(tds=[native], bindings=[LocalBinding({})]), MqttGatewayBinding("bus:1883")
    )
    await open_gateway.start()
    assert open_gateway.projected_tds["pump"]["security"] == ["bus_nosec"]
    await guarded.aclose()
    await open_gateway.aclose()


@pytest.fixture
def guarded_pump(monkeypatch, keypair):
    broker = Broker()
    monkeypatch.setattr("paho.mqtt.client.Client", lambda *a, **k: FakeClient(broker))
    native = _pump({"stop": "local://stop", "prime": "local://prime"})
    plain = ThingClient(tds=[native], bindings=[LocalBinding({})])
    pdp = PolicyDecisionPoint(
        vocabulary=build_vocabulary(plain.things),
        grant_source=LocalPolicyGrantSource({"operator": {(PUMP_ID, "stop", "invokeaction")}}),
    )
    records: list[dict] = []
    client = ThingClient(
        tds=[native],
        bindings=[LocalBinding({"stop": lambda: {"stopped": True}})],
        pdp=pdp,
        record=recorder(lambda r: records.append(r.as_jsonld())),
    )
    binding = MqttGatewayBinding(
        "bus:1883",
        guard=_guard(keypair),
        broker_binds_identity=True,
        bus_security={"bus_basic": {"scheme": "basic"}},
    )
    return broker, Gateway(client, binding), records


async def test_a_guarded_gateway_runs_each_request_as_the_caller_who_sent_it(
    guarded_pump, keypair
) -> None:
    broker, gateway, records = guarded_pump
    await gateway.start()
    td = gateway.projected_tds["pump"]
    login = ("caller-object-id", "pw")
    operator, _ = _consumer(
        broker, td, credentials={"bus_basic": login, "bus_bearer": keypair.mint(roles=["operator"])}
    )
    viewer, _ = _consumer(
        broker, td, credentials={"bus_basic": login, "bus_bearer": keypair.mint(roles=["viewer"])}
    )
    anonymous, _ = _consumer(broker, td, credentials={"bus_basic": login})
    assert await operator.invoke("pump__stop", {}) == {"stopped": True}
    refused = await viewer.invoke("pump__stop", {})
    assert refused.get("denied") is True
    assert await anonymous.invoke("pump__stop", {}) == {
        "error": "authentication required",
        "denied": True,
    }
    actors = {r.get("prov:wasAssociatedWith") for r in records}
    assert "caller-object-id" in actors
    await gateway.aclose()


async def test_the_broker_scheme_is_declared_beside_the_bearer_and_used_to_connect(
    monkeypatch, keypair
) -> None:
    broker = Broker()
    monkeypatch.setattr("paho.mqtt.client.Client", lambda *a, **k: FakeClient(broker))
    native = _pump({"stop": "local://stop", "prime": "local://prime"})
    gateway = Gateway(
        ThingClient(tds=[native], bindings=[LocalBinding({"stop": lambda: {"stopped": True}})]),
        MqttGatewayBinding(
            "bus:1883",
            guard=_guard(keypair),
            broker_binds_identity=True,
            bus_security={"bus_basic": {"scheme": "basic"}},
        ),
    )
    await gateway.start()
    td = gateway.projected_tds["pump"]
    assert td["security"] == ["bus_basic", "bus_bearer"]
    token = keypair.mint(roles=["operator"], extra={"sub": "bob"})
    credentials = {"bus_basic": ("bob", "secret"), "bus_bearer": token}
    client, made = _consumer(broker, td, credentials=credentials)
    assert await client.invoke("pump__stop", {}) == {"stopped": True}
    assert made[0].auth == ("bob", "secret")
    await gateway.aclose()


def test_the_kit_accepts_a_driver_that_declares_its_security(keypair) -> None:
    binding = MqttGatewayBinding(
        "bus:1883",
        guard=_guard(keypair),
        broker_binds_identity=True,
        bus_security={"bus_basic": {"scheme": "basic"}},
    )
    assert_gateway_binding_contract(binding)
    assert gateway_binding_capabilities(binding)["declares_security"] is True


def test_the_kit_refuses_a_declaration_that_is_not_security_schemes() -> None:
    class Vague(MqttGatewayBinding):
        def security_definitions(self) -> dict[str, dict]:
            return {"bus_mystery": {}}

    with pytest.raises(AssertionError, match="security scheme objects"):
        assert_gateway_binding_contract(Vague("bus:1883"))
