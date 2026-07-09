# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""MQTT north binding: serve a WoT fleet onto an MQTT bus.

This is the reference middleware driver. It implements the ``GatewayBinding``
contract (project forms, serve, teardown) plus the ``RequestReply``,
``EventMirroring``, and ``QoSAware`` capabilities, because an MQTT broker can
carry a reply, mirror events, and honor per-message QoS. A pub/sub-only or
one-shot transport would implement fewer of these, and the engine would call only
what it advertises.

Protocol-specific richness rides in the projected form's own ``mqv:`` vocabulary
(the MQTT binding-template namespace), so the engine never sees a topic, a QoS, or
a retain flag:

    mqv:qos        0 | 1 | 2      per-affordance delivery guarantee
    mqv:retain     bool           retain the last value for late subscribers
    mqv:userProperties  {..}      MQTT v5 user properties (the long-tail bag)

A consumer reads these off the form and drives the bus with a stock south-side
MqttBinding; the reply shape (``<topic>/reply``) is exactly what that binding
expects.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from thingctx.gateways.north import (
    INVOKE,
    READ,
    SUBSCRIBE,
    WRITE,
    Gateway,
    ServeRequest,
)

DEFAULT_PREFIX = "tc"

# The op each inbound topic kind resolves to, and the topic segment for each.
_KIND_FOR_OP = {INVOKE: "actions", READ: "props", WRITE: "props", SUBSCRIBE: "events"}


def _slug(thing: Any) -> str:
    from thingctx.thing import _tool_name

    return _tool_name(thing.id, "x").rsplit(".", 1)[0]


class MqttGatewayBinding:
    """Serve a fleet onto an MQTT broker. Implements GatewayBinding + RequestReply +
    EventMirroring + QoSAware.

    Args:
        broker: the ``host[:port]`` a consumer's MqttBinding connects to; the
            projected forms point there.
        prefix: the topic root (default ``tc``).
        qos: default MQTT QoS for gateway publishes/subscribes.
        default_retain: whether property/event topics retain by default; a form's
            ``mqv:retain`` overrides per affordance.
    """

    scheme = "mqtt"

    def __init__(
        self,
        broker: str,
        *,
        prefix: str = DEFAULT_PREFIX,
        qos: int = 1,
        default_retain: bool = False,
    ) -> None:
        self._broker = broker
        self._prefix = prefix
        self._qos = qos
        self._default_retain = default_retain
        self._paho: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gateway: Gateway | None = None
        self._event_tasks: list[asyncio.Task] = []
        # topic -> (slug, affordance, op) so an inbound message routes in O(1).
        self._routes: dict[str, tuple[str, str, str]] = {}

    # -- GatewayBinding: projection ----------------------------------------- #

    def quality_terms(self) -> tuple[str, ...]:
        """The mqv: terms this driver reads off a form (QoSAware)."""
        return ("mqv:qos", "mqv:retain", "mqv:userProperties")

    def project_forms(self, thing: Any, affordance: str, op: str) -> list[dict]:
        """One mqtt-faced form for this (affordance, op), carrying mqv: vocab."""
        slug = _slug(thing)
        kind = _KIND_FOR_OP.get(op)
        if kind is None:
            return []  # an op MQTT does not carry -> omit the form (honest TD)
        href = f"mqtt://{self._broker}/{self._prefix}/{slug}/{kind}/{affordance}"
        form: dict[str, Any] = {"href": href, "op": [op]}
        # Protocol-specific vocabulary, namespaced. A consumer/binding reads its
        # own; the engine passes this through opaquely.
        form["mqv:qos"] = self._qos
        if kind in ("props", "events"):
            form["mqv:retain"] = self._default_retain
        return [form]

    # -- GatewayBinding: serve/teardown ------------------------------------- #

    async def serve(self, engine: Gateway) -> None:
        """Connect, subscribe to every inbound action/property topic, retain each
        projected TD, and start mirroring events."""
        import paho.mqtt.client as mqtt

        self._gateway = engine
        self._loop = asyncio.get_running_loop()
        host, _, p = self._broker.partition(":")
        port = int(p) if p else 1883

        paho = mqtt.Client(protocol=mqtt.MQTTv5)
        paho.on_message = self._on_message
        self._paho = paho
        self.configure(paho)  # override point for TLS / credentials (Event Grid)
        paho.connect(host, port)
        paho.loop_start()

        for thing in engine.client.things:
            slug = _slug(thing)
            td = engine.projected_tds.get(slug, {})
            paho.publish(f"{self._prefix}/{slug}/td", json.dumps(td), qos=self._qos, retain=True)
            for name in thing.actions:
                topic = f"{self._prefix}/{slug}/actions/{name}"
                self._routes[topic] = (slug, name, INVOKE)
                paho.subscribe(topic, qos=self._qos)
            for name in thing.properties:
                topic = f"{self._prefix}/{slug}/props/{name}"
                # a props topic carries read (empty/no-value) or write ({"value":..})
                self._routes[topic] = (slug, name, WRITE)
                paho.subscribe(topic, qos=self._qos)
            for name in thing.events:
                self._start_mirror(slug, name)

    def configure(self, paho: Any) -> None:
        """Override point: set TLS / credentials on the paho client before connect
        (e.g. for Event Grid, a subclass calls paho.tls_set(...)). No-op default so
        the reference driver hard-codes no broker's auth."""

    async def aclose(self) -> None:
        for t in self._event_tasks:
            t.cancel()
        self._event_tasks.clear()
        if self._paho is not None:
            self._paho.loop_stop()
            self._paho.disconnect()

    # -- inbound: wire -> neutral request -> engine ----------------------- #

    def _on_message(self, _c: Any, _u: Any, msg: Any) -> None:
        if self._loop is None:
            return
        payload = msg.payload.decode() if isinstance(msg.payload, bytes) else str(msg.payload)
        asyncio.run_coroutine_threadsafe(self._handle(msg.topic, payload), self._loop)

    async def _handle(self, topic: str, payload: str) -> None:
        if topic.endswith("/reply"):
            return  # never act on our own reply publishes
        route = self._routes.get(topic)
        if route is None:
            return
        slug, affordance, op = route
        try:
            data = json.loads(payload) if payload.strip() else {}
        except json.JSONDecodeError:
            data = {"value": payload}
        # A props topic is a WRITE when the payload carries a value, else a READ.
        if op == WRITE and not (isinstance(data, dict) and "value" in data):
            op = READ
        req = ServeRequest(slug, affordance, op, data, correlation=topic)
        result = await self._gateway.dispatch(req)
        await self.reply(req, result)

    # -- RequestReply capability ------------------------------------------ #

    async def reply(self, request: ServeRequest, result: Any) -> None:
        """Publish the result to ``<topic>/reply`` (the shape a stock south-side
        MqttBinding consumer awaits). ``request.correlation`` is the inbound topic."""
        reply_topic = f"{request.correlation}/reply"
        self._publish(reply_topic, result)

    # -- EventMirroring capability ---------------------------------------- #

    async def mirror_event(self, thing_slug: str, event: str, payload: Any) -> None:
        self._publish(f"{self._prefix}/{thing_slug}/events/{event}", payload)

    def _start_mirror(self, slug: str, name: str) -> None:
        async def _mirror() -> None:
            try:
                stream = await self._gateway.client.subscribe(f"{slug}.{name}")
                async for payload in stream:
                    await self.mirror_event(slug, name, payload)
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


def _jsonable(value: Any) -> Any:
    from thingctx.runtime import to_text

    if isinstance(value, dict | list | str | int | float | bool) or value is None:
        return value
    try:
        return json.loads(to_text(value))
    except Exception:  # noqa: BLE001
        return {"result": str(value)}
