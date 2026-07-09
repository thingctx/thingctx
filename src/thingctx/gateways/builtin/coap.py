# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""CoAP north binding: serve a WoT fleet over a CoAP server (RFC 7252).

The SECOND reference middleware driver. Its job is to prove the north seam is not
MQTT-shaped: a driver whose transport is request/reply over UDP, with an Observe
extension for events (RFC 7641) and no retain, still serves a fleet through the
same engine, carrying its OWN protocol vocabulary.

Where MQTT is a pub/sub bus, CoAP is a constrained-device request/reply protocol:

* A request carries a METHOD (GET/PUT/POST), and the RESPONSE to that request IS
  the reply. There is no ``<topic>/reply`` side channel; the reply rides back on
  the same exchange. So ``reply`` here does not publish anywhere; it hands the
  encoded result back to the handler that awaited the dispatch.
* There is NO retain. A late subscriber does not get the last value for free; it
  must GET it. So this driver deliberately does NOT emit ``mqv:retain`` (or any
  retain vocab). That absence is a real protocol difference, not an omission.
* Events use OBSERVE (RFC 7641): a client registers on a resource and the server
  pushes notifications on change. So event mirroring is real, but it flows through
  CoAP observation, not a published event topic.

Protocol-specific richness rides in the projected form's own ``covv:`` vocabulary
(a CoAP binding-template namespace), so the engine never sees a method or an
observe flag:

    covv:method         "GET" | "PUT" | "POST"   the CoAP method for the op
    covv:observe        true                      the resource is observable (events)
    covv:contentFormat  int                       the CoAP Content-Format id (0 = text)

A consumer reads these off the form and drives the resource with a stock CoAP
client; a GET returns the reply as the response payload, exactly what CoAP expects.

The aiocoap dependency is imported LAZILY inside ``serve``/``aclose`` only, so the
module imports with no CoAP stack present. The projection, capability, and
request-handling logic are all exercisable offline (no UDP socket): the handler is
factored into :meth:`handle_request`, which a test drives with a fake request.
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

# The op each inbound resource kind resolves to, and the CoAP method it maps to.
# CoAP is method-oriented: GET reads, PUT writes, POST invokes. OBSERVE is a GET
# with the Observe option, so events project a GET-observable resource.
_KIND_FOR_OP = {INVOKE: "actions", READ: "props", WRITE: "props", SUBSCRIBE: "events"}
_METHOD_FOR_OP = {READ: "GET", WRITE: "PUT", INVOKE: "POST"}

# The CoAP Content-Format id for a UTF-8 text/JSON payload. 50 is application/json
# in the CoAP Content-Formats registry; the driver encodes replies as JSON text.
_CONTENT_FORMAT_JSON = 50


def _slug(thing: Any) -> str:
    from thingctx.thing import _tool_name

    return _tool_name(thing.id, "x").rsplit(".", 1)[0]


class CoapGatewayBinding:
    """Serve a fleet over a CoAP server. Implements GatewayBinding + RequestReply +
    EventMirroring + QoSAware.

    RequestReply: a CoAP response IS the reply to its request, so reply-bearing ops
    (read/write/invoke) are routed here. EventMirroring: CoAP Observe (RFC 7641)
    pushes notifications to registered observers, so native events mirror onto
    observable resources. QoSAware: the driver reads its own ``covv:`` terms off a
    form. Deliberately NOT PubSubOnly (it replies), NOT Announces (a consumer
    discovers resources via ``/.well-known/core``, which aiocoap serves; this
    reference driver does not add a separate birth/death announce).

    Args:
        host: the ``host[:port]`` a consumer's CoAP client connects to; the
            projected forms point there. Defaults to the CoAP default port 5683.
        prefix: the resource-path root (default ``tc``).
    """

    scheme = "coap"

    def __init__(
        self,
        host: str = "localhost",
        *,
        prefix: str = DEFAULT_PREFIX,
    ) -> None:
        self._host = host
        self._prefix = prefix
        self._context: Any = None
        self._gateway: Gateway | None = None
        self._event_tasks: list[asyncio.Task] = []
        # resource path -> (slug, affordance, event) for each observable event, so a
        # mirrored native event finds the resource(s) whose observers to notify.
        self._observable: dict[str, tuple[str, str, str]] = {}
        # slug/event -> the aiocoap resource object holding live observers. Populated
        # in serve(); mirror_event pokes the matching resource to fan out a change.
        self._event_resources: dict[tuple[str, str], Any] = {}

    # -- QoSAware: the covv: terms this driver reads off a form ------------- #

    def quality_terms(self) -> tuple[str, ...]:
        """The covv: terms this driver reads off a form (QoSAware). Note the
        ABSENCE of any retain term: CoAP has no retain, so a retain vocab would be
        a lie about the transport."""
        return ("covv:method", "covv:observe", "covv:contentFormat")

    # -- GatewayBinding: projection ----------------------------------------- #

    def project_forms(self, thing: Any, affordance: str, op: str) -> list[dict]:
        """One coap-faced form for this (affordance, op), carrying covv: vocab.

        The href is a ``coap://`` URI keyed by the resource kind (actions/props/
        events) and the affordance. The CoAP specifics (method, observe, content
        format) ride under ``covv:`` so the engine passes them through opaquely, and
        NO retain vocab is emitted (CoAP has none)."""
        slug = _slug(thing)
        kind = _KIND_FOR_OP.get(op)
        if kind is None:
            return []  # an op CoAP does not carry -> omit the form (honest TD)
        href = f"coap://{self._host}/{self._prefix}/{slug}/{kind}/{affordance}"
        form: dict[str, Any] = {"href": href, "op": [op]}
        # Protocol-specific vocabulary, namespaced. A consumer reads its own; the
        # engine never interprets these.
        form["covv:contentFormat"] = _CONTENT_FORMAT_JSON
        if op == SUBSCRIBE:
            # Events are delivered by CoAP Observe: an observable GET resource.
            form["covv:method"] = "GET"
            form["covv:observe"] = True
        else:
            form["covv:method"] = _METHOD_FOR_OP[op]
        return [form]

    # -- inbound: wire -> neutral request -> engine ----------------------- #
    #
    # Factored so a test drives it with a fake request and NO socket: build a
    # ServeRequest from (path, method, payload), dispatch through the engine (which
    # carries the authz gate), and encode the result as the response payload.

    async def handle_request(
        self,
        path: str,
        method: str,
        payload: bytes | str = b"",
    ) -> bytes:
        """Resolve one inbound CoAP request and return the encoded response payload.

        This is the whole request/reply path with no aiocoap and no socket: the
        aiocoap resource handler (in ``serve``) is a thin adapter that unpacks the
        wire request into ``(path, method, payload)`` and calls this, then wraps the
        returned bytes in an aiocoap ``Message``. Because dispatch runs through
        ``engine.dispatch``, the authz gate applies here too, so a denied caller
        gets a denial payload back on the same exchange, never a device touch."""
        if self._gateway is None:
            raise RuntimeError("serve() must run before handle_request()")
        route = self._route_for(path, method)
        if route is None:
            return _encode({"error": f"no resource for {method} {path}", "not_found": True})
        slug, affordance, op = route
        data = _decode_payload(payload)
        req = ServeRequest(slug, affordance, op, data, correlation=path)
        result = await self._gateway.dispatch(req)
        # In CoAP the response to the request IS the reply; reply() encodes it.
        return await self.reply(req, result)

    def _route_for(self, path: str, method: str) -> tuple[str, str, str] | None:
        """Map a resource path + CoAP method to (slug, affordance, op).

        Path shape: ``<prefix>/<slug>/<kind>/<affordance>``. The kind + method fix
        the op: a props GET reads, a props PUT writes, an actions POST invokes."""
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) != 4 or parts[0] != self._prefix:
            return None
        _, slug, kind, affordance = parts
        method = method.upper()
        if kind == "props" and method == "GET":
            return (slug, affordance, READ)
        if kind == "props" and method == "PUT":
            return (slug, affordance, WRITE)
        if kind == "actions" and method == "POST":
            return (slug, affordance, INVOKE)
        return None

    # -- RequestReply capability ------------------------------------------ #

    async def reply(self, request: ServeRequest, result: Any) -> bytes:
        """Encode the result as the CoAP response payload.

        CoAP has no reply topic: the response to the request carries the reply, so
        this returns the encoded bytes for the handler to send back on the same
        exchange (unlike MQTT, which publishes to ``<topic>/reply``). Returning the
        bytes keeps the round-trip testable with no socket."""
        return _encode(result)

    # -- EventMirroring capability (CoAP Observe, RFC 7641) --------------- #

    async def mirror_event(self, thing_slug: str, event: str, payload: Any) -> None:
        """Notify CoAP observers of a native event.

        With aiocoap serving (after ``serve``), this pokes the observable resource
        for ``(thing_slug, event)`` so aiocoap fans the new value out to every
        registered observer as a notification. Offline (no serve, no resource), it
        is a no-op: there are no observers to notify, so mirroring an event simply
        has nowhere to go, which is honest, not a swallow."""
        resource = self._event_resources.get((thing_slug, event))
        if resource is None:
            return
        # aiocoap resources signal a change to observers by updating state and
        # calling updated_state(); the resource's render_get returns the new value.
        resource.latest = _encode(payload)
        updated = getattr(resource, "updated_state", None)
        if callable(updated):
            updated()

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

    # -- GatewayBinding: serve/teardown ------------------------------------- #

    async def serve(self, engine: Gateway) -> None:
        """Stand up a CoAP server context and map each affordance to a resource.

        aiocoap is imported HERE, lazily, so importing this module needs no CoAP
        stack. Each property/action becomes a resource whose render_* handler calls
        ``handle_request`` (the offline-testable path); each event becomes an
        observable resource that ``mirror_event`` pokes. The actual UDP bind lives
        in aiocoap's ``Context.create_server_context`` and is only reached here."""
        import aiocoap
        import aiocoap.resource as resource

        self._gateway = engine
        root = resource.Site()

        for thing in engine.client.things:
            slug = _slug(thing)
            for name in thing.properties:
                path = (self._prefix, slug, "props", name)
                root.add_resource(path, _AffordanceResource(self, "/".join(path)))
            for name in thing.actions:
                path = (self._prefix, slug, "actions", name)
                root.add_resource(path, _AffordanceResource(self, "/".join(path)))
            for name in thing.events:
                path = (self._prefix, slug, "events", name)
                ev = _EventResource(_encode(None))
                self._event_resources[(slug, name)] = ev
                self._observable["/".join(path)] = (slug, name, name)
                root.add_resource(path, ev)
                self._start_mirror(slug, name)

        host, _, p = self._host.partition(":")
        port = int(p) if p else 5683
        self._context = await aiocoap.Context.create_server_context(
            root, bind=(host if host != "localhost" else None, port)
        )

    async def aclose(self) -> None:
        """Tear down: cancel event mirrors and shut down the aiocoap context."""
        for t in self._event_tasks:
            t.cancel()
        self._event_tasks.clear()
        if self._context is not None:
            await self._context.shutdown()
            self._context = None
        self._gateway = None


# --------------------------------------------------------------------------- #
# aiocoap resource adapters. These are only INSTANTIATED inside serve(), so the
# base class comes from the lazily imported aiocoap; the classes are defined at
# module scope but built via a factory the serve path calls, keeping import clean.
# We avoid subclassing aiocoap types at import time by adapting through duck-typed
# handlers aiocoap invokes: render_get / render_put / render_post.
# --------------------------------------------------------------------------- #


class _AffordanceResource:
    """A CoAP resource for one property or action. aiocoap calls render_get/put/
    post on an inbound request; each adapts the wire request to the offline
    ``handle_request`` path and wraps the returned bytes in a CoAP Message."""

    def __init__(self, binding: CoapGatewayBinding, path: str) -> None:
        self._binding = binding
        self._path = path

    async def render_get(self, request: Any) -> Any:
        return await self._respond("GET", request)

    async def render_put(self, request: Any) -> Any:
        return await self._respond("PUT", request)

    async def render_post(self, request: Any) -> Any:
        return await self._respond("POST", request)

    async def _respond(self, method: str, request: Any) -> Any:
        import aiocoap

        payload = getattr(request, "payload", b"") or b""
        body = await self._binding.handle_request(self._path, method, payload)
        return aiocoap.Message(code=aiocoap.CONTENT, payload=body)


class _EventResource:
    """An observable CoAP resource (RFC 7641). aiocoap tracks observers; a GET
    returns the latest value and ``updated_state`` re-renders for observers. This
    class stays duck-typed so the module imports without aiocoap; the observation
    machinery is mixed in only when aiocoap constructs the site in serve()."""

    def __init__(self, latest: bytes) -> None:
        self.latest = latest

    async def render_get(self, request: Any) -> Any:
        import aiocoap

        return aiocoap.Message(code=aiocoap.CONTENT, payload=self.latest)

    def updated_state(self) -> None:
        """Overridden by aiocoap's ObservableResource mixin when observing; a plain
        stub here so an offline mirror_event has a callable to invoke without a
        live observation context."""


def _encode(value: Any) -> bytes:
    """Encode a dispatch result as the CoAP response payload (JSON text bytes)."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode()
    return json.dumps(_jsonable(value)).encode()


def _decode_payload(payload: bytes | str) -> Any:
    """Decode an inbound CoAP payload into the neutral request data. Empty payload
    (a bare GET) is a read with no body; a JSON body decodes to its value, and a
    non-JSON body is wrapped as ``{"value": ...}`` so a PUT still carries a value."""
    text = payload.decode() if isinstance(payload, bytes) else str(payload)
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"value": text}


def _jsonable(value: Any) -> Any:
    from thingctx.runtime import to_text

    if isinstance(value, dict | list | str | int | float | bool) or value is None:
        return value
    try:
        return json.loads(to_text(value))
    except Exception:  # noqa: BLE001
        return {"result": str(value)}
