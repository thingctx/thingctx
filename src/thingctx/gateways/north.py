# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The north side of a middleware binding: serve a WoT fleet over a transport.

A binding has two sides. The CONSUMER side (``ProtocolBinding`` in
``thingctx.bindings``) drives a device: it speaks one transport outbound. The
SERVER side, defined here, is the mirror: it re-serves a fleet of Things onto a
middleware (an MQTT bus, a CoAP server, MCP, DDS), so any consumer on that
middleware drives the fleet uniformly.

The split follows how OPC-UA Part 14, W3C WoT, and DDS all separate a neutral
capability model from a swappable transport:

* The ENGINE (:class:`Gateway`) owns the neutral model and verbs: it holds a
  native ``ThingClient``, and the invariant loop is subscribe-inbound -> invoke
  native -> reply -> mirror events. It names the five abstract WoT operations and
  never a wire packet, a topic, a QoS, or a DDS policy.
* A DRIVER (a ``GatewayBinding``) owns the transport: it projects each Thing to a
  re-served TD carrying its OWN protocol vocabulary, maps the neutral verbs to its
  wire, and applies its transport's specific features.

Capability richness, the rule that avoids a lowest-common-denominator seam: a
driver declares what it can do by IMPLEMENTING optional capability protocols
(below), exactly as a consumer-side binding opts into ``Readable``/``Writable``.
The engine feature-detects with ``isinstance`` and calls only what a driver
advertises. Protocol-specific options ride in the projected TD form as namespaced,
ignore-if-unknown vocabulary (``mqv:``/``covv:``/``mcpv:``), and the engine passes
that form through opaquely, the analog of OPC-UA's per-transport settings struct.
No union of every protocol's options ever lands on the engine.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from thingctx.runtime import ThingClient

# The five neutral WoT operations the engine routes. A driver maps these to its
# own wire; the engine never names a transport packet.
READ = "readproperty"
WRITE = "writeproperty"
OBSERVE = "observeproperty"
INVOKE = "invokeaction"
SUBSCRIBE = "subscribeevent"


class ServeRequest:
    """One neutral inbound request the engine hands a driver's reply to. The
    driver builds these from its wire and the engine resolves them against the
    native fleet. ``correlation`` is the engine's neutral request id; a driver
    maps it to its own reply mechanism (MQTT v5 correlation-data, a reply topic,
    DDS-RPC).

    ``identity`` is the validated claims of the CALLER who sent this request, when
    the driver could authenticate it (the :class:`Authenticates` capability). It
    is ``None`` when the transport carries no per-caller identity, and the engine
    then authorizes with its server-level default. This is the same claims shape
    ``thingctx.identity`` produces and ``thingctx.authz`` consumes, so a request
    authenticated on the bus is authorized exactly like one over HTTP."""

    __slots__ = ("thing_slug", "affordance", "op", "payload", "correlation", "identity")

    def __init__(
        self,
        thing_slug: str,
        affordance: str,
        op: str,
        payload: Any = None,
        correlation: Any = None,
        identity: Any = None,
    ) -> None:
        self.thing_slug = thing_slug
        self.affordance = affordance
        self.op = op
        self.payload = payload
        self.correlation = correlation
        self.identity = identity


# --------------------------------------------------------------------------- #
# The driver contract: a base every server-side driver satisfies, plus optional
# capability protocols a driver opts into by presence (the anti-LCD mechanism).
# --------------------------------------------------------------------------- #


@runtime_checkable
class GatewayBinding(Protocol):
    """The minimum a middleware driver implements to serve a fleet.

    ``scheme`` is the URI scheme of the re-served forms (``"mqtt"``, ``"coap"``,
    ``"mcp"``). ``project_forms`` returns this driver's forms for one affordance,
    carrying its own namespaced vocabulary. ``serve`` starts the driver against a
    live fleet, wiring inbound requests to ``engine.dispatch`` and getting replies
    and mirrored events back. ``aclose`` tears down.

    Everything richer than this (event mirroring, QoS, announce, request/reply vs
    pub/sub) is an OPTIONAL capability below; a driver implements only what its
    transport supports, and the engine calls only what the driver advertises.
    """

    scheme: str

    def project_forms(self, thing: Any, affordance: str, op: str) -> list[dict]:
        """The re-served form(s) for one (affordance, op), with this driver's own
        namespaced vocabulary. Return ``[]`` for an op this transport cannot carry
        (so the projected TD is honest about what the bus can do)."""
        ...

    async def serve(self, engine: Gateway) -> None:
        """Connect and begin serving the fleet: subscribe to inbound addresses,
        and on each inbound message build a ``ServeRequest`` and await
        ``engine.dispatch(req)``, then deliver the reply on the wire."""
        ...

    async def aclose(self) -> None:
        """Stop serving and release the transport."""
        ...


@runtime_checkable
class EventMirroring(Protocol):
    """A driver that can push native events onto its wire. The engine feeds each
    mirrored payload here; a pub/sub-only or one-shot transport may omit this."""

    async def mirror_event(self, thing_slug: str, event: str, payload: Any) -> None: ...


@runtime_checkable
class RequestReply(Protocol):
    """A driver whose transport carries a reply for a request (invokeaction with a
    result, a property read). A driver that CANNOT (a fire-and-forget pub/sub bus)
    simply does not implement this, and the engine will not route reply-bearing
    ops to it, rather than silently dropping the reply."""

    async def reply(self, request: ServeRequest, result: Any) -> None: ...


@runtime_checkable
class PubSubOnly(Protocol):
    """A marker a driver implements to declare it has NO reply channel. The engine
    uses this to project only fire-and-forget ops and to surface an explicit error
    for a reply-bearing op, never a silent flatten."""

    is_pubsub_only: bool


@runtime_checkable
class Announces(Protocol):
    """A driver that announces the fleet on connect and reaps it on teardown
    (Sparkplug-style birth/death, a CoAP /.well-known/core, a DDS discovery).
    Announce format is entirely the driver's; the engine has no catalog schema."""

    async def announce(self, engine: Gateway) -> None: ...

    async def reap(self) -> None: ...


@runtime_checkable
class QoSAware(Protocol):
    """A driver that reads per-affordance quality options off the projected form's
    own vocabulary (mqv:qos, ddsv:reliability). Purely informational to the engine;
    it never interprets the values, it only knows the driver handles them."""

    def quality_terms(self) -> tuple[str, ...]: ...


@runtime_checkable
class Authenticates(Protocol):
    """A driver whose transport can carry a PER-CALLER identity, so the gateway
    authorizes each inbound request as the caller who sent it, not as one
    server-level identity.

    This is the north-side mirror of the south-side ``AuthMixin`` (which
    authenticates thingctx OUTBOUND to a device). ``authenticate`` turns a
    driver's raw inbound context into a claims dict the authz seam consumes, by
    whatever mechanism the transport carries it: a bearer token in an MQTT v5
    user-property or an MCP session (validate it with the same
    ``thingctx.identity`` guard the HTTP gateway uses), or a transport-level
    credential (a TLS/DTLS client certificate) mapped to claims.

    A driver whose wire CANNOT carry a caller identity simply does not implement
    this; the gateway then authorizes with its default (server-level) identity,
    exactly as before. So per-caller identity is used precisely where the
    transport can carry it, and never faked where it cannot.

    Return ``None`` to fall back to the server-level identity for this request
    (e.g. an anonymous message on a bus that usually carries identity)."""

    async def authenticate(self, inbound: Any) -> Any | None: ...


# --------------------------------------------------------------------------- #
# The engine.
# --------------------------------------------------------------------------- #


class Gateway:
    """Serve a native ``ThingClient`` over a middleware, through a driver.

    The engine is transport-neutral: it holds the fleet and resolves a neutral
    ``ServeRequest`` to a native device call, and it drives the driver's lifecycle.
    It references no topic, QoS, retain, observe, or partition. Point it at a
    ``GatewayBinding`` and call :meth:`start`.

    If the native client was built with the authorization seam (``pdp``/
    ``identity``), every dispatched request is authorized before the device is
    touched, so the middleware is not an authorization bypass, identical to the
    consumer path.

    Args:
        client: a ThingClient over the Things' real (native) transports.
        north: the middleware (north-facing) driver that serves the fleet.
    """

    def __init__(self, client: ThingClient, north: GatewayBinding) -> None:
        if not isinstance(north, GatewayBinding):
            raise TypeError(
                f"{north!r} is not a GatewayBinding (needs scheme, project_forms, serve, aclose)"
            )
        self._client = client
        self._north = north
        self._projected: dict[str, dict] = {}
        # The PDP the native client enforces with, if any. Held so a per-caller
        # request can be re-guarded (client.guarded(pdp, identity=caller)) to
        # authorize as the authenticated caller rather than the server identity.
        self._client_pdp = getattr(client, "_pdp", None)

    @property
    def client(self) -> ThingClient:
        return self._client

    @property
    def north(self) -> GatewayBinding:
        return self._north

    # -- capability queries the driver answered by presence ---------------- #

    @property
    def can_reply(self) -> bool:
        return isinstance(self._north, RequestReply) and not self._is_pubsub_only

    @property
    def can_mirror(self) -> bool:
        return isinstance(self._north, EventMirroring)

    @property
    def _is_pubsub_only(self) -> bool:
        return isinstance(self._north, PubSubOnly) and getattr(self._north, "is_pubsub_only", False)

    # -- lifecycle --------------------------------------------------------- #

    async def start(self) -> None:
        """Project every Thing to a re-served TD, then start the driver serving.
        If the driver announces (birth), do it after projecting."""
        for thing in self._client.things:
            self._projected[_slug(thing)] = self._project(thing)
        await self._north.serve(self)
        if isinstance(self._north, Announces):
            await self._north.announce(self)

    async def aclose(self) -> None:
        if isinstance(self._north, Announces):
            await self._north.reap()
        await self._north.aclose()
        await self._client.aclose()

    # -- the invariant loop: neutral request -> native call -> reply ------- #

    async def dispatch(self, request: ServeRequest) -> Any:
        """Resolve one neutral inbound request against the native fleet and return
        the result. The driver calls this; the engine owns the native invocation
        and the authz gate (inherited from the ThingClient).

        An authorization denial becomes an error RESULT (delivered to the caller on
        the wire), not a raised exception: a denied caller must not crash the
        gateway's serve loop for everyone else."""
        from thingctx.authz import AuthorizationDenied

        # Authorize as the CALLER, not as the gateway, when the driver
        # authenticated one. guarded() returns a client that shares all state but
        # decides against the caller's claims for this request. If there is no
        # caller identity (the transport carried none), fall through to the
        # gateway's own client and its server-level identity.
        client = self._client
        if request.identity is not None and self._client_pdp is not None:
            client = self._client.guarded(self._client_pdp, identity=request.identity)

        tool = f"{request.thing_slug}.{request.affordance}"
        op = request.op
        try:
            if op == INVOKE:
                result = await client.invoke(tool, request.payload or {})
            elif op in (READ, OBSERVE):
                result = await client.read_property(tool)
            elif op == WRITE:
                value = request.payload
                if isinstance(value, dict) and "value" in value:
                    value = value["value"]
                result = await client.write_property(tool, value)
            else:
                result = {"error": f"engine cannot dispatch op {op!r}"}
        except AuthorizationDenied as denied:
            return {"error": f"authorization denied: {denied.reason}", "denied": True}
        # A reply-bearing op on a pub/sub-only driver is an explicit error, never a
        # silent flatten: the driver told us it has no reply channel.
        if op in (INVOKE, READ, OBSERVE, WRITE) and not self.can_reply:
            scheme = self._north.scheme
            return {
                "error": f"op {op!r} needs a reply channel; driver {scheme!r} is pub/sub-only",
                "no_reply_channel": True,
                "result": result,
            }
        return result

    async def authorize(self, request: ServeRequest) -> dict:
        """Authorize a request WITHOUT invoking (for a subscribe/observe grant
        check before opening a stream). Returns ``{"permit": bool, "reason": ...}``,
        decided against the caller's identity when the request carries one, else the
        server identity. When no PDP is configured, permits (authz is off)."""
        if self._client_pdp is None:
            return {"permit": True}
        identity = (
            request.identity
            if request.identity is not None
            else getattr(self._client, "_identity", None)
        )
        from thingctx.authz.pdp import AccessRequest

        access = AccessRequest(
            thing_id=self._thing_id_for(request.thing_slug),
            affordance=request.affordance,
            op=request.op,
        )
        decision = await self._client_pdp.decide(identity, access)
        return {"permit": bool(decision.permit), "reason": getattr(decision, "reason", None)}

    def _thing_id_for(self, slug: str) -> str:
        """Map a fleet slug back to its Thing id for an AccessRequest."""
        for thing in self._client.things:
            if _slug(thing) == slug:
                return thing.id
        return slug

    async def mirror(self, thing_slug: str, event: str, payload: Any) -> None:
        """Push a native event onto the driver's wire, if it mirrors events."""
        if self.can_mirror:
            await self._north.mirror_event(thing_slug, event, payload)

    # -- projection: ask the driver for each affordance's re-served forms --- #

    def _project(self, thing: Any) -> dict:
        slug = _slug(thing)
        td: dict = {
            "@context": "https://www.w3.org/2022/wot/td/v1.1",
            "id": thing.id,
            "title": thing.title or slug,
            "securityDefinitions": {"bus_nosec": {"scheme": "nosec"}},
            "security": ["bus_nosec"],
        }
        actions: dict = {}
        for name in thing.actions:
            forms = self._north.project_forms(thing, name, INVOKE)
            if forms:
                actions[name] = {"forms": forms}
        if actions:
            td["actions"] = actions
        props: dict = {}
        for name, prop in thing.properties.items():
            ops = ([READ] if prop.readable else []) + ([WRITE] if prop.writable else [])
            forms: list[dict] = []
            for op in ops:
                forms.extend(self._north.project_forms(thing, name, op))
            if forms:
                props[name] = {"forms": forms}
        if props:
            td["properties"] = props
        events: dict = {}
        for name in thing.events:
            forms = self._north.project_forms(thing, name, SUBSCRIBE)
            if forms:
                events[name] = {"forms": forms}
        if events:
            td["events"] = events
        return td

    @property
    def projected_tds(self) -> dict[str, dict]:
        """The re-served TDs by slug, each carrying the driver's own vocabulary."""
        return dict(self._projected)


def _slug(thing: Any) -> str:
    """The fleet slug for a Thing (urn:demo:pump:v1 -> pump), matching the tool
    naming the runtime uses."""
    from thingctx.thing import _tool_name

    return _tool_name(thing.id, "x").rsplit(".", 1)[0]
