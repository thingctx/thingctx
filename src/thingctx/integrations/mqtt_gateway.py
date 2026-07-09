# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""MqttGateway: put a fleet of native Things on one MQTT bus.

The MCP bridge (``integrations.mcp``) exposes a fleet to MCP clients. This is its
sibling for a different audience: it exposes a fleet to an MQTT BUS, so any
consumer on the bus drives any Thing uniformly over MQTT, without knowing or
caring what the Thing really speaks (HTTP, CoAP, Modbus, ...).

The shape mirrors the MCP bridge: you hand it a ``ThingClient`` built over the
Things' REAL (native) transports, and the gateway serves them onto a broker. It
adds one process (the gateway) and no core changes.

  consumer (agent / dashboard / another Thing)
      | drives over the bus with a stock MqttBinding
      v
  MQTT broker  <--  MqttGateway  -->  ThingClient(native http/coap/modbus/...)
                    subscribes action topics, invokes native, publishes replies,
                    mirrors native events onto the bus

What it reuses (everything that matters): the native ``ThingClient`` and its
bindings, the request/reply topic shape a stock ``MqttBinding`` consumer already
expects (publish to ``<topic>``, reply on ``<topic>/reply``), the long-lived
``subscribe()`` contract for event mirroring, and the auth layer (native secrets
resolve inside the gateway and NEVER go on the bus). If the gateway's client was
built with a ``pdp``/``identity`` (the authz seam), every bridged call is
authorized before the native device is touched, so the bus does not become an
authorization bypass.

Topic convention (matches the ``<slug>.<name>`` tool naming the runtime uses):

    tc/<slug>/actions/<name>          request: a consumer publishes input here
    tc/<slug>/actions/<name>/reply    reply:   the gateway publishes the result
    tc/<slug>/props/<name>            property read (req/reply) or write
    tc/<slug>/props/<name>/reply      property read reply
    tc/<slug>/events/<name>           the gateway republishes native events here
    tc/<slug>/td                      retained: the projected mqtt-faced TD

``tc/+/td`` retained messages give any consumer the whole fleet's descriptions by
subscribing once, a discovery surface for free.

Scope, on purpose: this is a THIN reference gateway. A stateful, multi-tenant,
policy-driven bus (retained-state caches, rules, bus-auth policy) is engine
territory and does not belong in the lean client. Media never transits the bus:
continuous frames stay referenced (see the media binding); the gateway bridges
control/state/events only.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from thingctx.runtime import ThingClient, to_text
from thingctx.thing import _tool_name

DEFAULT_PREFIX = "tc"


def _slug(thing_id: str) -> str:
    """The same slug the tool names use (urn:demo:pump:v1 -> pump)."""
    # _tool_name returns "<slug>.<name>"; take the slug half with a stable name.
    return _tool_name(thing_id, "x").rsplit(".", 1)[0]


def project_mqtt_td(thing: Any, *, broker: str, prefix: str = DEFAULT_PREFIX) -> dict:
    """Project an mqtt-faced TD for one Thing: copy the raw TD, rewrite every
    affordance's forms to an ``mqtt://<broker>/<prefix>/<slug>/...`` topic, and
    drop the native security (the bus carries its own; the native secret stays in
    the gateway). Consumers read THIS TD and drive over the bus.

    ``broker`` is the host[:port] a consumer's MqttBinding will connect to.
    """
    slug = _slug(thing.id)
    base = f"mqtt://{broker}/{prefix}/{slug}"

    # Build the face from the PARSED thing, not thing.raw: the runtime's
    # WoTThing does not carry the source TD, so the affordances are rebuilt
    # from its typed fields (names, readable/writable flags, schemas).
    td: dict = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": thing.id,
        "title": thing.title or slug,
        # The bus is one security domain; strip the native schemes from the face.
        "securityDefinitions": {"bus_nosec": {"scheme": "nosec"}},
        "security": ["bus_nosec"],
    }
    if getattr(thing, "description", ""):
        td["description"] = thing.description

    def _forms(kind: str, name: str, ops: list[str]) -> list[dict]:
        return [{"href": f"{base}/{kind}/{name}", "op": ops}]

    if thing.actions:
        actions: dict = {}
        for name, action in thing.actions.items():
            adef: dict = {"forms": _forms("actions", name, ["invokeaction"])}
            if getattr(action, "input_schema", None):
                adef["input"] = action.input_schema
            if getattr(action, "output_schema", None):
                adef["output"] = action.output_schema
            actions[name] = adef
        td["actions"] = actions
    if thing.properties:
        properties: dict = {}
        for name, prop in thing.properties.items():
            ops = []
            if prop.readable:
                ops.append("readproperty")
            if prop.writable:
                ops.append("writeproperty")
            pdef: dict = {"forms": _forms("props", name, ops or ["readproperty"])}
            if not prop.readable:
                pdef["writeOnly"] = True
            if not prop.writable:
                pdef["readOnly"] = True
            if getattr(prop, "schema", None):
                pdef.update(prop.schema)
            properties[name] = pdef
        td["properties"] = properties
    if thing.events:
        events: dict = {}
        for name, event in thing.events.items():
            edef: dict = {"forms": _forms("events", name, ["subscribeevent"])}
            if getattr(event, "data_schema", None):
                edef["data"] = event.data_schema
            events[name] = edef
        td["events"] = events
    return td


class MqttGateway:
    """Bridge a native ``ThingClient`` onto an MQTT broker.

    Args:
        client: a ThingClient over the Things' REAL transports (native bindings).
            If it was built with a ``pdp``/``identity``, bridged calls are
            authorized before the device is touched.
        broker: the broker host[:port] BOTH the gateway and consumers connect to.
            The projected TDs point consumers at this host.
        prefix: the topic root (default ``tc``).
        qos: MQTT QoS for gateway publishes/subscribes (default 1).

    Needs the ``mqtt`` extra (paho-mqtt). Call :meth:`start` to connect and begin
    serving; :meth:`aclose` to stop. Nothing is served until ``start``.
    """

    def __init__(
        self,
        client: ThingClient,
        *,
        broker: str,
        prefix: str = DEFAULT_PREFIX,
        qos: int = 1,
    ) -> None:
        self._client = client
        self._broker = broker
        self._prefix = prefix
        self._qos = qos
        self._paho: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event_tasks: list[asyncio.Task] = []
        self._projected: dict[str, dict] = {}  # slug -> mqtt-faced TD

    # -- lifecycle --------------------------------------------------------- #

    async def start(self, *, host: str | None = None, port: int = 1883, **connect_kw: Any) -> None:
        """Connect to the broker, subscribe to every action/property/write topic,
        mirror events, and retain the projected TDs. ``host``/``port`` default to
        parsing ``broker``; ``connect_kw`` passes through to paho ``connect`` (for
        TLS / username / Event Grid, wire it here)."""
        import paho.mqtt.client as mqtt

        self._loop = asyncio.get_running_loop()
        if host is None:
            host, _, p = self._broker.partition(":")
            port = int(p) if p else port

        paho = mqtt.Client(protocol=mqtt.MQTTv5)
        paho.on_message = self._on_message
        # TLS / auth for a real broker (Event Grid) is set by the caller via
        # connect_kw + paho.tls_set/username_pw_set before start(); expose the
        # client so the caller can configure it.
        self._paho = paho
        self._configure_hook(paho)

        paho.connect(host, port, **connect_kw)
        paho.loop_start()

        # Subscribe to every inbound topic and retain each projected TD.
        for thing in self._client.things:
            slug = _slug(thing.id)
            td = project_mqtt_td(thing, broker=self._broker, prefix=self._prefix)
            self._projected[slug] = td
            paho.publish(f"{self._prefix}/{slug}/td", json.dumps(td), qos=self._qos, retain=True)
            for name in thing.actions:
                paho.subscribe(f"{self._prefix}/{slug}/actions/{name}", qos=self._qos)
            for name in thing.properties:
                paho.subscribe(f"{self._prefix}/{slug}/props/{name}", qos=self._qos)
            # Mirror events + observable properties onto the bus.
            for name in thing.events:
                self._start_mirror(slug, name, thing.id)

    def _configure_hook(self, paho: Any) -> None:
        """Override point / no-op. A caller that needs TLS or credentials (e.g.
        Event Grid) sets them on ``self._paho`` before ``start`` returns, or
        subclasses and configures here. Kept a hook so the reference gateway does
        not hard-code any one broker's auth."""

    async def aclose(self) -> None:
        for t in self._event_tasks:
            t.cancel()
        self._event_tasks.clear()
        if self._paho is not None:
            self._paho.loop_stop()
            self._paho.disconnect()
        await self._client.aclose()

    # -- inbound: bus -> native ------------------------------------------- #

    def _on_message(self, _c: Any, _u: Any, msg: Any) -> None:
        """Paho calls this on a broker thread; hand the work to the event loop."""
        if self._loop is None:
            return
        payload = msg.payload.decode() if isinstance(msg.payload, bytes) else str(msg.payload)
        asyncio.run_coroutine_threadsafe(self._handle(msg.topic, payload), self._loop)

    async def _handle(self, topic: str, payload: str) -> None:
        parts = topic.split("/")
        # <prefix>/<slug>/<kind>/<name>[...]
        if len(parts) < 4 or parts[0] != self._prefix:
            return
        _, slug, kind, name = parts[0], parts[1], parts[2], parts[3]
        if parts[-1] == "reply":  # never act on our own reply publishes
            return
        try:
            args = json.loads(payload) if payload.strip() else {}
        except json.JSONDecodeError:
            args = {"value": payload}

        reply_topic = f"{topic}/reply"
        if kind == "actions":
            result = await self._client.invoke(f"{slug}.{name}", args)
            self._publish(reply_topic, result)
        elif kind == "props":
            # A dict with "value" is a write; anything else (or empty) is a read.
            if isinstance(args, dict) and "value" in args:
                result = await self._client.write_property(f"{slug}.{name}", args["value"])
            else:
                result = await self._client.read_property(f"{slug}.{name}")
            self._publish(reply_topic, result)

    # -- outbound: native events -> bus ----------------------------------- #

    def _start_mirror(self, slug: str, name: str, thing_id: str) -> None:
        async def _mirror() -> None:
            try:
                stream = await self._client.subscribe(f"{slug}.{name}")
                async for payload in stream:
                    self._publish(f"{self._prefix}/{slug}/events/{name}", payload)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a dead mirror must not kill the gateway
                return

        self._event_tasks.append(asyncio.ensure_future(_mirror()))

    # -- helper ------------------------------------------------------------ #

    def _publish(self, topic: str, value: Any) -> None:
        if self._paho is None:
            return
        body = value if isinstance(value, str) else json.dumps(_jsonable(value))
        self._paho.publish(topic, body, qos=self._qos)

    @property
    def projected_tds(self) -> dict[str, dict]:
        """The mqtt-faced TDs, by slug. A consumer reads these to drive the bus."""
        return dict(self._projected)


def _jsonable(value: Any) -> Any:
    """Best-effort JSON shape for a native result (dataclasses / bytes / plain)."""
    if isinstance(value, dict | list | str | int | float | bool) or value is None:
        return value
    try:
        return json.loads(to_text(value))
    except Exception:  # noqa: BLE001
        return {"result": str(value)}
