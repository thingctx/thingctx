# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""MQTT gateway binding: serve a WoT fleet onto an MQTT bus.

Implements the ``GatewayBinding`` contract plus ``RequestReply``,
``EventMirroring`` and ``QoSAware``, because an MQTT broker can carry a reply,
mirror events and honor each message's QoS. A guarded gateway also uses
``Authenticates``, ``DeclaresSecurity`` and ``DeclaresUriVariables`` to check each
caller and declare its schemes and the sender's variable. The engine calls only
what a driver advertises.

Protocol-specific richness rides in the projected form's own ``mqv:`` vocabulary
(the MQTT binding-template namespace), so the engine never sees a topic, a QoS, or
a retain flag:

    mqv:qos        "0" | "1" | "2"  delivery guarantee
    mqv:retain     bool             retain the last value for late subscribers

Both terms are defined by the W3C MQTT binding template, which owns the ``mqv``
prefix. Nothing is added to it here.

A consumer reads these off the form and drives the bus with a stock south-side
MqttBinding; the reply shape (``<topic>/reply``, with the request's MQTT 5
Correlation Data copied back) is exactly what that binding expects.

A guarded gateway ends every request topic in the sender's MQTT User Name
(``<prefix>/<slug>/actions/<name>/<user>``, ``.../props/<name>/read/<user>``,
``.../props/<name>/write/<user>``, ``.../props/<name>/observe/<user>`` and
``.../events/<name>/subscribe/<user>``), so a topic names the whole operation and
the broker can tell a read from a write. It answers on ``<request>/reply`` and
streams to ``<request>/stream``. Its projected TDs declare that level once, as a
Thing level URI variable typed ``MQTT_USER_NAME`` (a thingctx term, not an mqv
one), so each client fills in its own User Name. A request runs only when its
token's ``sub`` is that User Name.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, cast

from thingctx.bindings.builtin.mqtt import MqttTls
from thingctx.gateways.engine import (
    INVOKE,
    OBSERVE,
    READ,
    SUBSCRIBE,
    WRITE,
    Gateway,
    ServeRequest,
)
from thingctx.runtime import to_text
from thingctx.thing import MQTT_USER_NAME, TOOL_SEP, thing_slug

DEFAULT_PREFIX = "tc"

# The URI variable that ends a guarded request topic: the sender's MQTT User Name.
_SENDER = "mqttUserName"

_log = logging.getLogger("thingctx.gateway")

# The op each inbound topic kind resolves to, and the topic segment for each.
_KIND_FOR_OP = {
    INVOKE: "actions",
    READ: "props",
    WRITE: "props",
    OBSERVE: "props",
    SUBSCRIBE: "events",
}
# A guarded gateway gives a property's read, write and observe a topic each.
_PROPERTY_OP = {READ: "read", WRITE: "write", OBSERVE: "observe"}


def _slug(thing: Any) -> str:
    return thing_slug(thing.id)


@dataclass(frozen=True)
class _Correlation:
    """How to answer one inbound request: on its topic's ``/reply``, carrying
    the MQTT 5 Correlation Data the requester sent, if any."""

    topic: str
    data: bytes | None = None


def _correlation(request: ServeRequest) -> _Correlation:
    """The request's correlation; a bare inbound topic is accepted too."""
    c = request.correlation
    return c if isinstance(c, _Correlation) else _Correlation(str(c))


@dataclass
class _Startup:
    """What a gateway waits for before it reports serving:
    the broker's CONNACK, and a SUBACK for each SUBSCRIBE it sent (MQTT 5.0,
    sections 3.2 and 3.9). Updated on the event loop, from paho's callbacks."""

    done: asyncio.Event = field(default_factory=asyncio.Event)
    # Packet identifier -> the topic filter its SUBSCRIBE asked for.
    pending: dict[int, str] = field(default_factory=dict)
    # Topic filters paho could not send, typically because a refused CONNECT
    # closed the connection first; the CONNACK then says why.
    unsent: list[str] = field(default_factory=list)
    connected: bool = False
    refusal: str | None = None

    def sent(self, result: int, mid: int | None, topic: str) -> None:
        if result != 0 or mid is None:
            self.unsent.append(topic)
        else:
            self.pending[mid] = topic

    def connack(self, reason: Any) -> None:
        if reason.is_failure:
            self.refuse(f"the broker refused the gateway's connection: {reason}")
        elif self.unsent:
            self.refuse(f"the gateway could not send its subscription to {self.unsent[0]}")
        else:
            self.connected = True
            self._settle()

    def suback(self, mid: int, reasons: list[Any]) -> None:
        topic = self.pending.pop(mid, None)
        refused = [reason for reason in reasons if reason.is_failure]
        if topic is not None and refused:
            self.refuse(f"the broker refused the gateway's subscription to {topic}: {refused[0]}")
        self._settle()

    def refuse(self, why: str) -> None:
        self.refusal = self.refusal or why
        self.done.set()

    def _settle(self) -> None:
        if self.connected and not self.pending:
            self.done.set()


class MqttGatewayBinding:
    """Serve a fleet onto an MQTT broker.

    Args:
        broker: the ``host[:port]`` a consumer's MqttBinding connects to, or
            ``mqtt://host[:port]``, or ``mqtts://host[:port]`` for MQTT over TLS
            on port 8883 unless one is named; the projected forms point there.
        prefix: the topic root (default ``tc``).
        qos: default MQTT QoS for gateway publishes/subscribes.
        default_retain: whether each event the gateway mirrors is published retained
            (MQTT 5.0, section 3.3.1.3), which its form declares as ``mqv:retain``.
            A property's form declares ``mqv:retain`` false, because its topic
            carries requests and never a value.
        guard: an optional ``thingctx.identity`` guard (a ``JwtGatewayGuard`` or
            anything with ``async validate(token) -> claims``). When given, the
            driver validates the bearer token in each message's MQTT 5 User
            Property ``authorization`` and runs the request, authorized as THAT
            caller, only when the token's ``sub`` is the User Name the request
            topic ends in. Without a guard the gateway authorizes every request
            with its own identity.
        broker_binds_identity: attests that the broker lets each login publish
            requests only under its own User Name, as the rules
            :func:`thingctx.gateways.mosquitto.acl_file` writes do. A guard is
            refused without it.
        bus_security: the TD security schemes a caller needs to connect to the
            broker itself (``{"bus_basic": {"scheme": "basic"}}``, say). Each
            projected TD declares them next to the gateway's own, so a consumer
            authenticates the connection as well as each request. A guarded
            gateway needs a basic scheme here: a thingctx client takes the User
            Name its topics end in from that scheme's credential.
        connect_timeout: seconds ``start`` waits for the broker to accept the
            connection and acknowledge every subscription.
        tls: an :class:`~thingctx.bindings.MqttTls` that puts the connection on
            TLS, as ``mqtts://`` does with the system's CA store. A gateway on TLS
            writes ``mqtts://`` in the forms it projects.
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
        bus_security: dict[str, dict] | None = None,
        connect_timeout: float = 10.0,
        tls: MqttTls | None = None,
    ) -> None:
        scheme, sep, address = broker.partition("://")
        if not sep:
            scheme, address = "mqtt", broker
        if scheme not in ("mqtt", "mqtts") or not address:
            raise ValueError(
                f"broker must be host[:port], mqtt://host[:port] or mqtts://host[:port], "
                f"not {broker!r}"
            )
        # Confused deputy guardrail: a guard checks that the token's sub is the User
        # Name the request topic ends in, but only the broker can make that User Name
        # the sender's. If a login can publish under another's User Name, a sender can
        # present another party's valid token and be authorized as them. So a guard
        # is REFUSED unless the deployment attests the broker keeps each login to its
        # own topic level.
        if guard is not None and not broker_binds_identity:
            raise ValueError(
                "a per caller guard requires a broker that binds connection identity to "
                "the request topic: each login may publish requests only under its own MQTT "
                "User Name, as the rules thingctx.gateways.mosquitto.acl_file writes do. "
                "Pass broker_binds_identity=True to attest the broker enforces this, or "
                "omit the guard to serve at the server identity."
            )
        schemes = (bus_security or {}).values()
        if guard is not None and not any(s.get("scheme") == "basic" for s in schemes):
            raise ValueError(
                "a guarded gateway needs a basic scheme in bus_security, such as "
                '{"bus_basic": {"scheme": "basic"}}: a client takes the MQTT User Name '
                "its request topics end in from that scheme's credential"
            )
        if connect_timeout <= 0:
            raise ValueError(f"connect_timeout must be positive, not {connect_timeout}")
        self._broker = address
        self._tls = tls or (MqttTls() if scheme == "mqtts" else None)
        self.scheme = "mqtts" if self._tls is not None else "mqtt"
        self._prefix = prefix
        self._qos = qos
        self._default_retain = default_retain
        self._guard = guard
        self._broker_binds_identity = broker_binds_identity
        self._paho: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gateway: Gateway | None = None
        # slug -> the tasks mirroring that Thing's events.
        self._event_tasks: dict[str, list[asyncio.Task]] = {}
        # (slug, event, stream topic) -> the task mirroring a caller's stream.
        self._streams: dict[tuple[str, str, str], asyncio.Task] = {}
        # topic -> (slug, affordance, op) so an inbound message routes in O(1).
        self._routes: dict[str, tuple[str, str, str]] = {}
        self._bus_security = dict(bus_security or {})
        self._connect_timeout = connect_timeout
        self._startup: _Startup | None = None

    # -- GatewayBinding: projection ----------------------------------------- #

    def quality_terms(self) -> tuple[str, ...]:
        """The mqv: terms this driver reads off a form (QoSAware)."""
        return ("mqv:qos", "mqv:retain")

    def project_forms(self, thing: Any, affordance: str, op: str) -> list[dict]:
        """One form on the bus for this (affordance, op), carrying mqv: vocab. A
        guarded form ends in the sender's User Name, and a guarded event's form
        names the caller's own stream."""
        slug = _slug(thing)
        kind = _KIND_FOR_OP.get(op)
        # An open props topic carries reads and writes, so an unguarded gateway has
        # no topic of its own for a property's changes.
        if kind is None or (op == OBSERVE and self._guard is None):
            return []  # an op MQTT does not carry -> omit the form (honest TD)
        href = f"{self.scheme}://{self._broker}/{self._prefix}/{slug}/{kind}/{affordance}"
        if self._guard is not None:
            # Reserved expansion (RFC 6570, section 3.2.3) keeps a User Name such as
            # a@b verbatim, as the broker and the token's sub spell it.
            sender = f"{{+{_SENDER}}}"
            if kind == "events":
                href += f"/subscribe/{sender}/stream"
            elif kind == "props":
                href += f"/{_PROPERTY_OP[op]}/{sender}"
                if op == OBSERVE:
                    href += "/stream"
            else:
                href += f"/{sender}"
        form: dict[str, Any] = {"href": href, "op": [op]}
        # Protocol-specific vocabulary, namespaced. A consumer/binding reads its
        # own; the engine passes this through opaquely.
        # The binding's schema types mqv:qos as a string: "0", "1" or "2".
        form["mqv:qos"] = str(self._qos)
        # A guarded form names a request or one caller's stream; neither is retained.
        # An open props topic carries requests and never a value, so only an event,
        # mirrored on its own topic, is retained.
        if kind in ("props", "events") and self._guard is None:
            form["mqv:retain"] = self._default_retain and kind == "events"
        return [form]

    def uri_variables(self) -> dict[str, dict]:
        """With a guard, the variable every request topic ends in: the sender's MQTT
        User Name (MQTT 5.0, section 3.1.3.5), typed ``MQTT_USER_NAME`` so a client
        fills in its own and the tool projection leaves it out. Without a guard,
        none."""
        if self._guard is None:
            return {}
        return {
            _SENDER: {
                "@type": MQTT_USER_NAME,
                "type": "string",
                "minLength": 1,
                "description": "The MQTT User Name the client connects with. A request "
                "runs only when its token's subject is this name.",
            }
        }

    # -- GatewayBinding: serve/teardown ------------------------------------- #

    async def serve(self, engine: Gateway) -> None:
        """Connect, subscribe to every inbound action/property topic, retain each
        projected TD, and start mirroring events. Returns only once the broker has
        accepted the connection and acknowledged every subscription, so a request
        sent after start finds its topic served; raises ``ConnectionError`` when
        the broker refuses either, or does not answer within ``connect_timeout``
        seconds."""
        # optional dep, kept local so the core imports without the extra
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        self._gateway = engine
        loop = self._loop = asyncio.get_running_loop()
        host, _, p = self._broker.partition(":")
        port = int(p) if p else (8883 if self._tls is not None else 1883)
        startup = self._startup = _Startup()

        # paho calls these on its own network thread.
        def on_connect(_c: Any, _u: Any, _f: Any, reason: Any, _p: Any) -> None:
            loop.call_soon_threadsafe(startup.connack, reason)

        def on_subscribe(_c: Any, _u: Any, mid: int, reasons: Any, _p: Any) -> None:
            loop.call_soon_threadsafe(startup.suback, mid, reasons)

        paho = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        paho.on_message = self._on_message
        paho.on_connect = on_connect
        paho.on_subscribe = on_subscribe
        paho.connect_timeout = self._connect_timeout
        self._paho = paho
        if self._tls is not None:
            self._tls.apply(paho)
        self.configure(paho)  # override point for credentials (Event Grid)
        try:
            await asyncio.to_thread(paho.connect, host, port)
        except OSError as exc:
            raise ConnectionError(f"the gateway could not reach {self._broker}: {exc}") from exc
        paho.loop_start()

        for thing in engine.client.things:
            self._serve_thing(thing)
        try:
            await asyncio.wait_for(startup.done.wait(), self._connect_timeout)
        except asyncio.TimeoutError:
            wait = self._connect_timeout
            startup.refuse(f"the broker at {self._broker} did not answer within {wait} s")
        except asyncio.CancelledError:
            await self._stop_serving()
            raise
        finally:
            self._startup = None
            paho.on_connect = self._on_reconnect
            paho.on_subscribe = None
        if startup.refusal is not None:
            await self._stop_serving()
            raise ConnectionError(startup.refusal)

    def _serve_thing(self, thing: Any) -> None:
        """Route and subscribe one Thing's inbound topics, start mirroring its
        events, then retain its projected TD. A broker handles one client's packets
        in order, so a client that finds the TD on the bus finds its topics served."""
        slug = _slug(thing)
        # A guarded request topic ends in its sender's User Name, which + matches.
        sender = "" if self._guard is None else "/+"
        for name in thing.actions:
            topic = f"{self._prefix}/{slug}/actions/{name}{sender}"
            self._routes[topic] = (slug, name, INVOKE)
            self._subscribe(topic)
        for name, prop in thing.properties.items():
            if self._guard is None:
                # an open props topic carries read (empty/no-value) or write ({"value":..})
                topic = f"{self._prefix}/{slug}/props/{name}"
                self._routes[topic] = (slug, name, WRITE)
                self._subscribe(topic)
                continue
            ops = [
                *([READ] if prop.readable else []),
                *([WRITE] if prop.writable else []),
                *([OBSERVE] if prop.observable else []),
            ]
            for op in ops:
                topic = f"{self._prefix}/{slug}/props/{name}/{_PROPERTY_OP[op]}{sender}"
                self._routes[topic] = (slug, name, op)
                self._subscribe(topic)
        for name in thing.events:
            if self._guard is None:
                # Unguarded: the whole gateway is server-level, so mirror the
                # event to an open topic, consistent with the rest of the face.
                self._start_mirror(slug, name)
            else:
                # Guarded: an event carries a subscribeevent grant, so mirroring it
                # to an open topic would leak it to any subscriber. A consumer
                # REQUESTS the stream on an authenticated subscribe topic, and the
                # gateway mirrors only to that caller's stream topic once authorized.
                req_topic = f"{self._prefix}/{slug}/events/{name}/subscribe{sender}"
                self._routes[req_topic] = (slug, name, SUBSCRIBE)
                self._subscribe(req_topic)
        self._retain_td(self._paho, slug)

    def _retain_td(self, paho: Any, slug: str) -> None:
        td = cast("Gateway", self._gateway).projected_tds.get(slug, {})
        paho.publish(f"{self._prefix}/{slug}/td", json.dumps(td), qos=self._qos, retain=True)

    def _subscribe(self, topic: str) -> None:
        result, mid = self._paho.subscribe(topic, qos=self._qos)
        if self._startup is not None:
            self._startup.sent(result, mid, topic)

    def _on_reconnect(self, client: Any, _u: Any, _f: Any, reason: Any, _p: Any) -> None:
        """Serve again on each new connection after start. The session ends with
        each connection (no Session Expiry Interval, MQTT 5.0, section 3.1.2.11.2),
        so the broker keeps no subscription across it, and a broker that restarted
        may keep no retained TD either."""
        if reason.is_failure:
            return
        for topic in list(self._routes):
            client.subscribe(topic, qos=self._qos)
        for slug in list(cast("Gateway", self._gateway).projected_tds):
            self._retain_td(client, slug)

    def security_definitions(self) -> dict[str, dict]:
        """The broker's own schemes (``bus_security``), and with a guard, the bearer
        token each request must carry in the MQTT 5 User Property ``authorization``.
        TD 1.1 can say that only as a header parameter, so the scheme also says it
        in words. With neither, nothing is asked of a caller."""
        if self._guard is None:
            return dict(self._bus_security)
        scheme: dict[str, Any] = {
            "scheme": "bearer",
            "in": "header",
            "name": "authorization",
            "description": "Send the caller's token as the MQTT 5 User Property "
            "'authorization' on every request.",
        }
        algorithms = getattr(self._guard, "allowed_algorithms", None)
        if algorithms:
            scheme |= {"format": "jwt", "alg": algorithms[0]}
        return {**self._bus_security, "bus_bearer": scheme}

    def configure(self, paho: Any) -> None:
        """Override point: set credentials on the paho client before it connects
        (a subclass calls paho.username_pw_set(...), say). TLS from ``mqtts://`` or
        ``tls`` is already set when this runs. The default sets nothing, so the
        reference driver builds in no broker's auth. Leave ``on_connect`` and
        ``on_subscribe`` alone: the gateway sets them before this runs and relies on
        them."""

    async def aclose(self) -> None:
        await self._stop_serving()

    async def _stop_serving(self) -> None:
        """Cancel every event mirror, forget every route and leave the bus, so
        nothing a stopped or failed start set up keeps running and a later start
        serves each Thing once."""
        for tasks in self._event_tasks.values():
            for t in tasks:
                t.cancel()
        self._event_tasks.clear()
        self._streams.clear()
        self._routes.clear()
        if self._paho is not None:
            # DISCONNECT wakes paho's network thread, which may be waiting on a
            # broker that never answered; the join then runs off the event loop.
            self._paho.disconnect()
            await asyncio.to_thread(self._paho.loop_stop)

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
        # A guarded route is the filter <base>/+, and the level + matches is the
        # sender's User Name.
        sender, routed = "", topic
        if self._guard is not None:
            base, _, sender = topic.rpartition("/")
            routed = f"{base}/+"
        route = self._routes.get(routed)
        if route is None:
            return
        slug, affordance, op = route
        try:
            data = json.loads(payload) if payload.strip() else {}
        except json.JSONDecodeError:
            data = {"value": payload}
        # An open props topic is a WRITE when the payload carries a value, else a
        # READ. A guarded one names its operation, and a write must carry a value.
        has_value = isinstance(data, dict) and "value" in data
        if op == WRITE and not has_value and self._guard is None:
            op = READ
        correlation = _Correlation(topic, getattr(props, "CorrelationData", None))
        # Authenticate the caller when this driver requires it. Fail CLOSED, the
        # same posture as the HTTP guard: if a guard is configured, a message with
        # a missing or invalid token is DENIED, not silently downgraded to the
        # server identity. Only when no guard is configured does the gateway serve
        # with its own (server-level) identity.
        req = ServeRequest(slug, affordance, op, data, correlation=correlation)
        if self._guard is not None:
            identity = await self.authenticate(props)
            if not _issued_to(identity, sender):
                if isinstance(identity, dict):
                    _log.warning(
                        "gateway: refused a token for %r sent under %r",
                        identity.get("sub"),
                        sender,
                    )
                await self.reply(req, {"error": "authentication required", "denied": True})
                return
            req.identity = identity
            if op == WRITE and not has_value:
                await self.reply(req, {"error": 'a write carries its value as {"value": ...}'})
                return

        # A subscribe or observe is authorized per caller through the engine, like
        # any other op, and mirrored only to that caller's <request topic>/stream,
        # never to an open topic, so a gated stream reaches only a caller the PDP
        # granted it.
        if op in (SUBSCRIBE, OBSERVE):
            await self._handle_subscribe(req)
            return

        # serve() sets _gateway before any inbound message can be handled.
        result = await cast("Gateway", self._gateway).dispatch(req)
        await self.reply(req, result)

    async def _handle_subscribe(self, req: ServeRequest) -> None:
        """Authorize an event subscribe or a property observe request, then mirror
        the event or the property's changes to ``<request topic>/stream`` on
        permit. The stream topic follows from the request topic alone, so a request
        cannot point the gateway's writes at a topic of its choosing."""
        # Authorize the subscribeevent op for this caller via the engine gate.
        # serve() sets _gateway before any inbound message reaches here.
        decision = await cast("Gateway", self._gateway).authorize(req)
        if not decision.get("permit"):
            await self.reply(req, {"error": "subscribe denied", "denied": True})
            return
        stream_topic = f"{_correlation(req).topic}/stream"
        # Mirror THIS caller's stream to their topic only; the native subscribe is
        # authorized for the caller's identity by the engine.
        self._start_caller_mirror(req.thing_slug, req.affordance, stream_topic, req.identity)
        await self.reply(req, {"subscribed": True, "stream": stream_topic})

    # -- Authenticates capability ----------------------------------------- #

    async def authenticate(self, inbound: Any) -> Any | None:
        """Validate the caller's bearer token from an MQTT v5 ``authorization``
        user-property into claims, using the configured guard. Returns ``None``
        when there is no guard, no token, or the token fails validation; a guarded
        gateway refuses any request for which it returns ``None``. ``inbound`` is the
        paho ``Properties`` object.

        It reuses the same ``thingctx.identity`` guard the HTTP gateway uses, so a
        caller validated on the bus yields the same claims shape as one over HTTP.

        TRUST BOUNDARY (read before relying on this): the token is validated
        cryptographically (signature, issuer, audience, expiry), so a forged token
        is rejected. This method does NOT bind the token to the connection that
        delivered it: ``_handle`` binds it to the User Name the request topic ends
        in, and only the broker binds that User Name to the login that published.
        On a broker that lets each login publish only under its own User Name, a
        token works only from the login it names. On an open broker, or one whose
        rules let a login publish under another's User Name, a sender could present
        another party's token while it is still valid: do not enable a guard on
        such a broker."""
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
        MqttBinding consumer awaits), with the request's MQTT 5 Correlation Data
        copied back. ``request.correlation`` is the inbound topic, or what this
        driver built from the inbound message."""
        correlation = _correlation(request)
        self._publish(f"{correlation.topic}/reply", result, correlation_data=correlation.data)

    # -- EventMirroring capability ---------------------------------------- #

    async def mirror_event(self, thing_slug: str, event: str, payload: Any) -> None:
        topic = f"{self._prefix}/{thing_slug}/events/{event}"
        self._publish(topic, payload, retain=self._default_retain)

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

        self._event_tasks.setdefault(slug, []).append(asyncio.ensure_future(_mirror()))

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

        # A repeated request for the same event and stream topic replaces the
        # running mirror; a second one would deliver every event twice.
        key = (slug, name, stream_topic)
        previous = self._streams.pop(key, None)
        tasks = self._event_tasks.setdefault(slug, [])
        if previous is not None:
            previous.cancel()
            if previous in tasks:
                tasks.remove(previous)
        task = asyncio.ensure_future(_mirror())
        self._streams[key] = task
        tasks.append(task)

    # -- helper ------------------------------------------------------------ #

    def _publish(
        self,
        topic: str,
        value: Any,
        correlation_data: bytes | None = None,
        retain: bool = False,
    ) -> None:
        if self._paho is None:
            return
        body = value if isinstance(value, str) else json.dumps(_jsonable(value))
        if correlation_data is None:
            self._paho.publish(topic, body, qos=self._qos, retain=retain)
            return
        # optional dep, kept local so the core imports without the extra
        from paho.mqtt.packettypes import PacketTypes  # noqa: PLC0415
        from paho.mqtt.properties import Properties  # noqa: PLC0415

        props = Properties(PacketTypes.PUBLISH)
        props.CorrelationData = correlation_data
        self._paho.publish(topic, body, qos=self._qos, properties=props)


def _issued_to(claims: Any, user: str) -> bool:
    """True when the token's subject (RFC 7519, section 4.1.2) is the MQTT User Name
    the request topic ends in. An empty level, which ``+`` also matches (MQTT 5.0,
    section 4.7.1.3), names no one."""
    return bool(user) and isinstance(claims, dict) and claims.get("sub") == user


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
