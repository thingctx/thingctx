# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for the MQTT gateway (``integrations.mqtt_gateway``).

No broker, no network. Two surfaces are proven:

* ``project_mqtt_td`` — the mqtt-faced TD projection: every affordance form is
  rewritten to ``mqtt://<broker>/tc/<slug>/<kind>/<name>``, the native security
  is replaced with a nosec bus scheme, and the op arrays follow the affordance
  (readproperty/writeproperty per readOnly/writeOnly, invokeaction, subscribeevent).
* ``MqttGateway._handle`` — the inbound bus->native bridge, driven directly. The
  gateway is never ``start``-ed (no paho, no broker); ``_publish`` is captured so
  a test sees exactly what the gateway would put on the bus. A real
  ``ThingClient`` over a ``LocalBinding`` proves the native device is actually
  touched, and a ``pdp``/``identity`` proves the bus is not an authz bypass.

The gateway needs the ``mqtt`` extra only to ``start`` (import paho). Every test
here drives its pure logic, so paho is not imported; no importorskip is needed.
"""

from __future__ import annotations

import asyncio

import pytest

from thingctx import LocalBinding, ThingClient
from thingctx.authz import (
    LocalPolicyGrantSource,
    PolicyDecisionPoint,
    build_vocabulary,
)
from thingctx.integrations.mqtt_gateway import MqttGateway, _slug, project_mqtt_td
from thingctx.thing import parse_thing

PUMP_ID = "urn:demo:pump:v1"
BROKER = "bus:1883"


# --------------------------------------------------------------------------- #
# Test TD + in-process device
# --------------------------------------------------------------------------- #


def _pump_td() -> dict:
    """A pump exercising every projection case:

    * ``set_speed``: an action.
    * ``target_rpm``: a read+write property.
    * ``serial``: ``readOnly`` -> readproperty only.
    * ``mode``: ``writeOnly`` -> writeproperty only.
    * ``alarm``: an event.
    """
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": PUMP_ID,
        "title": "Pump",
        "securityDefinitions": {"basic_sc": {"scheme": "basic"}},
        "security": ["basic_sc"],
        "properties": {
            "target_rpm": {
                "type": "number",
                "forms": [{"href": "local://target_rpm"}],
            },
            "serial": {
                "type": "string",
                "readOnly": True,
                "forms": [{"href": "local://serial", "op": ["readproperty"]}],
            },
            "mode": {
                "type": "string",
                "writeOnly": True,
                "forms": [{"href": "local://mode", "op": ["writeproperty"]}],
            },
        },
        "actions": {
            "set_speed": {
                "input": {"type": "object", "properties": {"rpm": {"type": "number"}}},
                "forms": [{"href": "local://set_speed"}],
            },
        },
        "events": {
            "alarm": {"forms": [{"href": "local://alarm", "op": ["subscribeevent"]}]},
        },
    }


class _Pump:
    """An in-process pump a LocalBinding drives. Records every action call so a
    test can prove the native device was actually touched (or NOT touched, on a
    denied call)."""

    def __init__(self) -> None:
        self.target_rpm = 800
        self.calls: list = []

    # action
    def set_speed(self, rpm: int) -> dict:
        self.calls.append(("set_speed", rpm))
        self.target_rpm = rpm
        return {"ok": True, "rpm": rpm}

    # property read/write (LocalBinding resolves get_<name>/set_<name>)
    def get_target_rpm(self) -> int:
        self.calls.append(("get_target_rpm",))
        return self.target_rpm

    def set_target_rpm(self, value: int) -> dict:
        self.calls.append(("set_target_rpm", value))
        self.target_rpm = value
        return {"ok": True, "target_rpm": value}


def _capture(gateway: MqttGateway) -> list:
    """Monkeypatch the gateway's _publish to record (topic, value); return the
    list. The gateway is never started, so this is the only publish surface."""
    published: list = []
    gateway._publish = lambda topic, value: published.append((topic, value))  # type: ignore[method-assign]
    return published


# --------------------------------------------------------------------------- #
# project_mqtt_td
# --------------------------------------------------------------------------- #


def test_project_rewrites_every_form_href_and_ops():
    """Every action/property/event form href is rewritten to
    mqtt://<broker>/tc/<slug>/<kind>/<name>, and the op arrays follow the
    affordance kind + readOnly/writeOnly."""
    thing = parse_thing(_pump_td())
    td = project_mqtt_td(thing, broker=BROKER)
    slug = _slug(PUMP_ID)
    assert slug == "pump"
    base = f"mqtt://{BROKER}/tc/{slug}"

    # action -> invokeaction, one mqtt form
    a = td["actions"]["set_speed"]["forms"]
    assert a == [{"href": f"{base}/actions/set_speed", "op": ["invokeaction"]}]

    # read+write property -> both ops
    p = td["properties"]["target_rpm"]["forms"]
    assert p == [{"href": f"{base}/props/target_rpm", "op": ["readproperty", "writeproperty"]}]

    # readOnly -> readproperty only; writeOnly -> writeproperty only
    assert td["properties"]["serial"]["forms"] == [
        {"href": f"{base}/props/serial", "op": ["readproperty"]}
    ]
    assert td["properties"]["mode"]["forms"] == [
        {"href": f"{base}/props/mode", "op": ["writeproperty"]}
    ]

    # event -> subscribeevent
    assert td["events"]["alarm"]["forms"] == [
        {"href": f"{base}/events/alarm", "op": ["subscribeevent"]}
    ]

    # every href on the face is an mqtt bus topic; no native http leaked
    hrefs = [
        f["href"]
        for group in ("actions", "properties", "events")
        for aff in td.get(group, {}).values()
        for f in aff["forms"]
    ]
    assert hrefs, "projection must carry affordances"
    assert all(h.startswith(f"mqtt://{BROKER}/tc/{slug}/") for h in hrefs)


def test_project_replaces_native_security_with_bus_nosec():
    """The native securityDefinitions (basic auth) are dropped; the face carries
    a single nosec bus scheme. The native secret never reaches the projected TD."""
    thing = parse_thing(_pump_td())
    td = project_mqtt_td(thing, broker=BROKER)
    assert td["securityDefinitions"] == {"bus_nosec": {"scheme": "nosec"}}
    assert td["security"] == ["bus_nosec"]
    assert "basic_sc" not in td["securityDefinitions"]
    assert td["id"] == PUMP_ID
    assert td["title"] == "Pump"


def test_project_prefix_override():
    """A non-default prefix threads into every href and topic root."""
    thing = parse_thing(_pump_td())
    td = project_mqtt_td(thing, broker=BROKER, prefix="fleet")
    href = td["actions"]["set_speed"]["forms"][0]["href"]
    assert href == f"mqtt://{BROKER}/fleet/pump/actions/set_speed"


# --------------------------------------------------------------------------- #
# inbound bridge: _handle (bus -> native)
# --------------------------------------------------------------------------- #


def _gateway(client: ThingClient) -> MqttGateway:
    return MqttGateway(client, broker=BROKER)


@pytest.mark.asyncio
async def test_handle_action_invokes_native_and_replies():
    """A message on tc/pump/actions/set_speed invokes the native device AND
    publishes the result on .../reply."""
    pump = _Pump()
    client = ThingClient(tds=[_pump_td()], bindings=[LocalBinding(pump)])
    gw = _gateway(client)
    published = _capture(gw)

    await gw._handle("tc/pump/actions/set_speed", '{"rpm":1500}')

    assert pump.calls == [("set_speed", 1500)]  # native device actually invoked
    assert published == [("tc/pump/actions/set_speed/reply", {"ok": True, "rpm": 1500})]
    await client.aclose()


@pytest.mark.asyncio
async def test_handle_property_read_replies_with_value():
    """An empty payload on a property topic is a read: the reply carries the
    device's current value."""
    pump = _Pump()
    pump.target_rpm = 1234
    client = ThingClient(tds=[_pump_td()], bindings=[LocalBinding(pump)])
    gw = _gateway(client)
    published = _capture(gw)

    await gw._handle("tc/pump/props/target_rpm", "")

    assert ("get_target_rpm",) in pump.calls  # a read hit the device
    assert published == [("tc/pump/props/target_rpm/reply", 1234)]
    await client.aclose()


@pytest.mark.asyncio
async def test_handle_property_write_changes_state_and_replies():
    """A {"value": ...} payload is a write: device state changes and a reply is
    published."""
    pump = _Pump()
    client = ThingClient(tds=[_pump_td()], bindings=[LocalBinding(pump)])
    gw = _gateway(client)
    published = _capture(gw)

    await gw._handle("tc/pump/props/target_rpm", '{"value":1500}')

    assert pump.target_rpm == 1500  # native state actually changed
    assert pump.calls == [("set_target_rpm", 1500)]
    assert published == [("tc/pump/props/target_rpm/reply", {"ok": True, "target_rpm": 1500})]
    await client.aclose()


@pytest.mark.asyncio
async def test_handle_reply_topic_is_ignored():
    """A message on a .../reply topic must do nothing: no device call, no
    publish. This is the loop guard against the gateway acting on its own
    replies."""
    pump = _Pump()
    client = ThingClient(tds=[_pump_td()], bindings=[LocalBinding(pump)])
    gw = _gateway(client)
    published = _capture(gw)

    await gw._handle("tc/pump/actions/set_speed/reply", '{"rpm":9999}')

    assert pump.calls == []  # device never touched
    assert published == []  # no re-publish -> no infinite loop
    await client.aclose()


@pytest.mark.asyncio
async def test_handle_wrong_prefix_ignored():
    """A topic outside the gateway's prefix is dropped, so a foreign topic on the
    same broker never reaches a device."""
    pump = _Pump()
    client = ThingClient(tds=[_pump_td()], bindings=[LocalBinding(pump)])
    gw = _gateway(client)
    published = _capture(gw)

    await gw._handle("other/pump/actions/set_speed", '{"rpm":10}')

    assert pump.calls == []
    assert published == []
    await client.aclose()


# --------------------------------------------------------------------------- #
# authz on the bus: the gateway is not an authz bypass
# --------------------------------------------------------------------------- #


def _guarded_client(td: dict, pump: _Pump, roles: list[str], policy: dict) -> ThingClient:
    """A ThingClient built the native guarded way (pdp=, identity=), so the
    gateway's _handle routes every call through the PEP before the device."""
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(vocab, LocalPolicyGrantSource(policy))
    return ThingClient(
        tds=[td],
        bindings=[LocalBinding(pump)],
        pdp=pdp,
        identity={"roles": roles},
        authz_raise=False,  # denials come back as an envelope the gateway publishes
    )


@pytest.mark.asyncio
async def test_bus_authz_denies_ungranted_write_and_touches_nothing():
    """Identity granted READ but not WRITE. A write over the bus is denied: the
    reply carries a denial envelope and the device is never touched. Proves the
    bus does not bypass authz."""
    td = _pump_td()
    pump = _Pump()
    policy = {"reader": {(PUMP_ID, "target_rpm", "readproperty")}}
    client = _guarded_client(td, pump, ["reader"], policy)
    gw = _gateway(client)
    published = _capture(gw)

    await gw._handle("tc/pump/props/target_rpm", '{"value":1500}')

    assert pump.calls == []  # device not written
    assert pump.target_rpm == 800  # state unchanged
    assert len(published) == 1
    topic, envelope = published[0]
    assert topic == "tc/pump/props/target_rpm/reply"
    assert envelope["error"] == "authorization denied"
    assert envelope["op"] == "writeproperty"
    await client.aclose()


@pytest.mark.asyncio
async def test_bus_authz_denies_ungranted_action_and_touches_nothing():
    """Same identity, an ungranted action over the bus: denied before the device,
    denial envelope published."""
    td = _pump_td()
    pump = _Pump()
    policy = {"reader": {(PUMP_ID, "target_rpm", "readproperty")}}
    client = _guarded_client(td, pump, ["reader"], policy)
    gw = _gateway(client)
    published = _capture(gw)

    await gw._handle("tc/pump/actions/set_speed", '{"rpm":1500}')

    assert pump.calls == []  # action never invoked
    assert len(published) == 1
    topic, envelope = published[0]
    assert topic == "tc/pump/actions/set_speed/reply"
    assert envelope["error"] == "authorization denied"
    assert envelope["op"] == "invokeaction"
    await client.aclose()


@pytest.mark.asyncio
async def test_bus_authz_allows_granted_read():
    """The SAME guarded client: a granted read succeeds over the bus and the
    reply carries the real value. Proves the deny path above is enforcement, not
    a broken gateway."""
    td = _pump_td()
    pump = _Pump()
    pump.target_rpm = 640
    policy = {"reader": {(PUMP_ID, "target_rpm", "readproperty")}}
    client = _guarded_client(td, pump, ["reader"], policy)
    gw = _gateway(client)
    published = _capture(gw)

    await gw._handle("tc/pump/props/target_rpm", "")

    assert ("get_target_rpm",) in pump.calls  # granted read reached the device
    assert published == [("tc/pump/props/target_rpm/reply", 640)]
    await client.aclose()


# --------------------------------------------------------------------------- #
# event mirroring: native events -> bus
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_event_mirror_republishes_native_payloads():
    """_start_mirror holds a native client.subscribe and republishes each pushed
    payload to tc/<slug>/events/<name>. Drive it via LocalBinding.emit."""
    td = _pump_td()
    binding = LocalBinding(_Pump())
    client = ThingClient(tds=[td], bindings=[binding])
    gw = _gateway(client)
    published = _capture(gw)

    # Start the mirror task for the alarm event, then let the subscribe register.
    gw._start_mirror("pump", "alarm", PUMP_ID)
    await asyncio.sleep(0)  # let _mirror open the subscribe queue

    # The gateway subscribes as "pump.alarm"; LocalBinding registers the queue
    # under the affordance name the client passes it (the event name "alarm").
    binding.emit("alarm", {"code": 1})
    binding.emit("alarm", {"code": 2})
    # Yield until both payloads have been drained and republished.
    for _ in range(50):
        await asyncio.sleep(0)
        if len(published) >= 2:
            break

    assert published == [
        ("tc/pump/events/alarm", {"code": 1}),
        ("tc/pump/events/alarm", {"code": 2}),
    ]

    for t in gw._event_tasks:
        t.cancel()
    await client.aclose()
