# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""MQTT gateway binding: serve a WoT fleet onto an MQTT bus.

Implements the ``GatewayBinding`` contract (project forms, serve, teardown) plus
the ``RequestReply``, ``EventMirroring``, and ``QoSAware`` capabilities, because
an MQTT broker can carry a reply, mirror events, and honor per-message QoS. A
pub/sub-only or one-shot transport would implement fewer of these, and the engine
would call only what it advertises.

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
from typing import Any, cast

from thingctx.gateways.engine import (
    INVOKE,
    READ,
    SUBSCRIBE,
    WRITE,
    Gateway,
    ServeRequest,
)
from thingctx.runtime import to_text
from thingctx.thing import TOOL_SEP, thing_slug

DEFAULT_PREFIX = "tc"

# The op each inbound topic kind resolves to, and the topic segment for each.
_KIND_FOR_OP = {INVOKE: "actions", READ: "props", WRITE: "props", SUBSCRIBE: "events"}


def _slug(thing: Any) -> str:
    return thing_slug(thing.id)


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
        guard: an optional ``thingctx.identity`` guard (a ``JwtGatewayGuard`` or
            anything with ``async validate(token) -> claims``). When given, the
            driver implements per-caller authentication: it reads a bearer token
            from each message's MQTT v5 ``authorization`` user-property, validates
            it to claims, and the engine authorizes the request as THAT caller.
            Without a guard the driver carries no per-caller identity and the
            gateway authorizes with its server-level identity, as before.
    """

    scheme = "mqtt"

    def __init__(
        self,
        broker: str,
        *,
        prefix: str = DEFAULT_PREFIX,
        qos: int = 1,
        default_retain: bool = False,
        guard: Any = None,
        broker_binds_identity: bool = False,
    ) -> None:
        # Confused-deputy guardrail, enforced at config time: a per-caller
        # ``guard`` reads the caller's token from a message user-property but does
        # NOT bind it to the broker connection. On a broker that does not
        # authenticate each publisher connection, a sender could present another
        # party's still-valid token and be authorized as them. So a guard is
        # REFUSED unless the deployment attests the broker binds connection
        # identity (mTLS client cert / per-client ACLs), e.g. Azure Event Grid /
        # Event Hub, HiveMQ with client ACLs.
        if guard is not None and not broker_binds_identity:
            raise ValueError(
                "a per-caller guard requires a broker that binds connection identity "
                "(mTLS client cert or per-client topic ACLs), because the message token "
                "is not bound to the connection. Pass broker_binds_identity=True to attest "
                "the broker enforces this, or omit the guard to serve at the server identity."
            )
        self._broker = broker
        self._prefix = prefix
        self._qos = qos
        self._default_retain = default_retain
        self._guard = guard
        self._broker_binds_identity = broker_binds_identity
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
        # optional dep, kept local so the core imports without the extra
        import paho.mqtt.client as mqtt  # noqa: PLC0415

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
                if self._guard is None:
                    # Unguarded: the whole gateway is server-level, so mirror the
                    # event to an open topic, consistent with the rest of the face.
                    self._start_mirror(slug, name)
                else:
                    # Guarded: an event carries a subscribeevent grant, so it must
                    # NOT be mirrored to an open topic (that would leak a gated
                    # stream to any subscriber). Instead a consumer must REQUEST
                    # the stream on an authenticated subscribe topic; the gateway
                    # authorizes it and mirrors only to that caller's stream topic.
                    req_topic = f"{self._prefix}/{slug}/events/{name}/subscribe"
                    self._routes[req_topic] = (slug, name, SUBSCRIBE)
                    paho.subscribe(req_topic, qos=self._qos)

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
        # Carry the MQTT v5 properties through so _handle can read the caller's
        # authorization user-property (paho attaches them as msg.properties).
        props = getattr(msg, "properties", None)
        asyncio.run_coroutine_threadsafe(self._handle(msg.topic, payload, props), self._loop)

    async def _handle(self, topic: str, payload: str, props: Any = None) -> None:
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
        # Authenticate the caller when this driver requires it. Fail CLOSED, the
        # same posture as the HTTP guard: if a guard is configured, a message with
        # a missing or invalid token is DENIED, not silently downgraded to the
        # server identity. Only when no guard is configured does the gateway serve
        # with its own (server-level) identity.
        req = ServeRequest(slug, affordance, op, data, correlation=topic)
        if self._guard is not None:
            identity = await self.authenticate(props)
            if identity is None:
                await self.reply(req, {"error": "authentication required", "denied": True})
                return
            req.identity = identity

        # A guarded event SUBSCRIBE is authorized per caller, then mirrored only to
        # that caller's stream, never to an open topic. Authorize the subscribeevent
        # op through the engine (same gate as any other op); on permit, start a
        # caller-scoped mirror to the stream topic the request names (or a default
        # per-request topic). This closes the open-mirror leak: a gated event stream
        # reaches only a caller the PDP granted subscribeevent.
        if op == SUBSCRIBE:
            await self._handle_subscribe(req, data)
            return

        # serve() sets _gateway before any inbound message can be handled.
        result = await cast("Gateway", self._gateway).dispatch(req)
        await self.reply(req, result)

    async def _handle_subscribe(self, req: ServeRequest, data: Any) -> None:
        """Authorize an event subscribe request, then mirror to the caller's own
        stream topic on permit. The request may name a ``stream`` topic to receive
        on; default is ``<subscribe-topic>/stream``."""
        # Authorize the subscribeevent op for this caller via the engine gate.
        # serve() sets _gateway before any inbound message reaches here.
        decision = await cast("Gateway", self._gateway).authorize(req)
        if not decision.get("permit"):
            await self.reply(req, {"error": "subscribe denied", "denied": True})
            return
        stream_topic = (
            data.get("stream") if isinstance(data, dict) and data.get("stream") else None
        ) or f"{req.correlation}/stream"
        # Mirror THIS caller's stream to their topic only; the native subscribe is
        # authorized for the caller's identity by the engine.
        self._start_caller_mirror(req.thing_slug, req.affordance, stream_topic, req.identity)
        await self.reply(req, {"subscribed": True, "stream": stream_topic})

    # -- Authenticates capability ----------------------------------------- #

    async def authenticate(self, inbound: Any) -> Any | None:
        """Validate the caller's bearer token from an MQTT v5 ``authorization``
        user-property into claims, using the configured guard. Returns ``None``
        (fall back to the server identity) when there is no guard, no token, or
        the token fails validation. ``inbound`` is the paho ``Properties`` object.

        It reuses the same ``thingctx.identity`` guard the HTTP gateway uses, so a
        caller validated on the bus yields the same claims shape as one over HTTP.

        TRUST BOUNDARY (read before relying on this): the token is validated
        cryptographically (signature, issuer, audience, expiry), so a forged token
        is rejected. But this method does NOT bind the message-level token to the
        broker CONNECTION that delivered it. On a broker that authenticates each
        publisher connection and enforces topic permissions (Event Grid, Event Hub,
        HiveMQ with per-client ACLs), the connection is the outer trust boundary
        and the token identifies the caller within it. On a broker where many
        untrusted senders share one connection, or an open broker, a sender could
        present another party's still-valid token: message-level identity alone is
        NOT sufficient there. Bind identity to the connection (mTLS client cert,
        per-client broker ACLs), or do not enable a guard on such a broker."""
        if self._guard is None:
            return None
        token = _user_property(inbound, "authorization")
        if not token:
            return None
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        try:
            return await self._guard.validate(token)
        except Exception:
            return None

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
        # _start_mirror runs from serve(), which set _gateway just before.
        gateway = cast("Gateway", self._gateway)

        async def _mirror() -> None:
            try:
                stream = await gateway.client.subscribe(f"{slug}{TOOL_SEP}{name}")
                async for payload in stream:
                    await self.mirror_event(slug, name, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                return

        self._event_tasks.append(asyncio.ensure_future(_mirror()))

    def _start_caller_mirror(self, slug: str, name: str, stream_topic: str, identity: Any) -> None:
        """Mirror an event to ONE caller's stream topic, after their subscribeevent
        grant was authorized. The native subscribe runs on the caller's guarded
        client, so per-delivery re-authorization (token expiry / revocation)
        applies: the caller's stream stops when their grant lapses, not just at
        subscribe time."""
        # Reached from _handle_subscribe, so serve() has already set _gateway.
        client = cast("Gateway", self._gateway).client
        pdp = getattr(client, "_pdp", None)
        if pdp is not None and identity is not None:
            client = client.guarded(pdp, identity=identity)

        async def _mirror() -> None:
            try:
                stream = await client.subscribe(f"{slug}{TOOL_SEP}{name}")
                async for payload in stream:
                    self._publish(stream_topic, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                return

        self._event_tasks.append(asyncio.ensure_future(_mirror()))

    # -- helper ------------------------------------------------------------ #

    def _publish(self, topic: str, value: Any) -> None:
        if self._paho is None:
            return
        body = value if isinstance(value, str) else json.dumps(_jsonable(value))
        self._paho.publish(topic, body, qos=self._qos)


def _user_property(props: Any, key: str) -> str | None:
    """Read one MQTT v5 user-property by key. paho exposes them as a list of
    (name, value) pairs on ``props.UserProperty``. Returns None if absent."""
    pairs = getattr(props, "UserProperty", None) if props is not None else None
    if not pairs:
        return None
    for name, value in pairs:
        if name == key:
            # MQTT v5 user-property values are UTF-8 strings (paho decodes them).
            return str(value)
    return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict | list | str | int | float | bool) or value is None:
        return value
    try:
        return json.loads(to_text(value))
    except Exception:
        return {"result": str(value)}
