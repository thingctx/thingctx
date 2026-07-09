# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""ThingClient: list and invoke a Thing's actions, read/write properties,
subscribe to events. No LLM.

    client = ThingClient(tds=[td], bindings=[HttpBinding()])
    client.list_actions()
    await client.invoke("pump.set_speed", {"rpm": 1200})
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from thingctx.bindings import BindingRegistry, ProtocolBinding, default_bindings
from thingctx.bindings.builtin.media import is_media_form
from thingctx.thing import (
    WoTAction,
    WoTThing,
    actions_to_tools,
    parse_thing,
)
from thingctx.trust import (
    ApprovePolicy,
    Approver,
    VerifyReport,
    gate_action,
    gate_write,
    verify_thing,
)

if TYPE_CHECKING:
    from thingctx.authz.pdp import AccessRequest, PolicyDecisionPoint


class ThingClient:
    """List + invoke the actions of one or more WoT Things. Transport-
    agnostic; no LLM."""

    @classmethod
    def from_registry(cls, registry, *, bindings=None, **kwargs) -> ThingClient:
        """Build a client over every TD a registry yields. `registry` is
        anything with fetch() -> list[dict] (see thingctx.registry)."""
        return cls(tds=registry.fetch(), bindings=bindings, **kwargs)

    def __init__(
        self,
        *,
        tds: list[dict[str, Any]],
        bindings: BindingRegistry | list[ProtocolBinding] | None = None,
        only_idempotent: bool = False,
        validate: bool = False,
        approve: Approver | None = None,
        approve_when: ApprovePolicy = "declared",
        pdp: PolicyDecisionPoint | None = None,
        identity: Any = None,
        authz_raise: bool = True,
    ) -> None:
        # validate=True checks each TD against the W3C TD 1.1 schema and
        # raises TDValidationError on nonconformance (needs [validate]).
        #
        # approve / approve_when gate risky calls behind a human/policy: see
        # thingctx.trust. With approve_when="declared" (default) only actions
        # the TD marks risky are gated, and a gated call with no approver is
        # denied (a safe default). approve_when="never" disables the gate.
        self._approve = approve
        self._approve_when: ApprovePolicy = approve_when
        # pdp / identity move authorization INTO the client. When pdp is None,
        # authorization is off and nothing below runs (backward compatible: a
        # caller that never sets a pdp is unaffected). When a pdp is set, every
        # device-reaching dispatch method authorizes the resolved (thing_id,
        # affordance, op) against it BEFORE the approve gate and BEFORE any
        # binding is selected, so a denial can never reach a transport. There is
        # no wrapper to bypass: self.invoke IS the authorized path, and
        # as_tools() hands out that same method. authz_raise picks the denial
        # shape: raise AuthorizationDenied (default, so a denial is never
        # mistaken for a device response) or return a thingctx-style error
        # envelope (matching the approve gate's blocked return).
        self._pdp = pdp
        self._identity = identity
        self._authz_raise = authz_raise
        self._things: list[WoTThing] = [parse_thing(td, validate=validate) for td in tds]
        # Bindings resolve a form to a transport. ``bindings`` is a
        # BindingRegistry or a plain list; an explicitly supplied binding
        # shadows a built-in for its scheme. When none is given, default to
        # http + local so the documented quickstart routes without wiring; pass
        # an empty list for a client that registers none.
        if isinstance(bindings, BindingRegistry):
            self._registry = bindings
        elif bindings is not None:
            self._registry = BindingRegistry(list(bindings))
        else:
            self._registry = BindingRegistry(default_bindings())
        self._bindings = self._registry.bindings
        self._only_idempotent = only_idempotent
        # Preferred transport order = the order bindings were given.
        self._prefer = tuple(
            s for inv in self._bindings for s in (getattr(inv, "schemes", None) or (inv.scheme,))
        )
        self._reindex()

    def _reindex(self) -> None:
        """Recompute every derived map from ``self._things``: the tool specs,
        the invoke route, the property/event maps, the media split, and the
        per-binding security binding. Run at construction and again whenever the
        thing set changes (see :meth:`add_things`)."""
        self._tool_specs, self._route = actions_to_tools(
            self._things,
            only_idempotent=self._only_idempotent,
        )
        # Telemetry name to (Thing, Property/Event) maps, keyed by the
        # same short ``<slug>.<name>`` scheme as actions.
        from thingctx.thing import _tool_name

        self._props: dict[str, Any] = {}
        self._events: dict[str, Any] = {}
        for thing in self._things:
            for p in thing.properties.values():
                self._props[_tool_name(thing.id, p.name)] = p
            for e in thing.events.values():
                self._events[_tool_name(thing.id, e.name)] = e
        # Bind the TDs' declared security to any binding that honors it, so
        # requests carry the right auth without the adopter wiring it. A
        # fleet-aware binding (with_things) authenticates each call as its
        # owning Thing; otherwise fall back to the first Thing's schemes.
        for inv in self._bindings:
            if hasattr(inv, "with_things"):
                inv.with_things(self._things)
            elif hasattr(inv, "with_security") and self._things:
                inv.with_security(self._things[0])

        # Media affordances are continuous streams, not request/response: they
        # are consumed via frames(), never invoke(). Split them out of the
        # invoke route and the LLM tool specs so a tool-calling loop never tries
        # to invoke() one; expose them through list_media()/frames() instead.
        self._media: dict[str, WoTAction] = {}
        for name, action in list(self._route.items()):
            if any(is_media_form(f) for f in action.forms):
                self._media[name] = action
                del self._route[name]
        if self._media:
            self._tool_specs = [
                s for s in self._tool_specs if s.get("function", {}).get("name") not in self._media
            ]

    def add_things(self, tds: list[dict[str, Any]], *, validate: bool = False) -> list[str]:
        """Register TDs into a live client and return the added Thing ids.

        The runtime counterpart of the constructor's ``tds=`` for Things that
        appear after construction: a self-describing binding (see
        docs/DISCOVERY.md), a directory push. Each TD is parsed, appended, and
        the client is fully reindexed (tool specs, route, property/event maps,
        media split, and the declared-security binding on every binding).

        Collision policy, so a re-describe is idempotent and safe:
        - Thing id: a new TD whose id already exists REPLACES the prior Thing
          (a device re-describing itself supersedes its old shape). The old
          Thing's tools disappear from the route; the new Thing's take their
          place. Order is preserved for ids that do not collide.
        - Tool name: names are ``<slug>.<action>``; because a colliding id
          replaces rather than stacks, two live Things never share a tool name
          unless their ids slugify the same. If they do, the later-added Thing
          wins the name (last write), matching the id-replace rule.
        """
        added = [parse_thing(td, validate=validate) for td in tds]
        by_id = {t.id: t for t in self._things}
        for t in added:
            by_id[t.id] = t  # replace on id collision; append otherwise
        # Preserve first-seen order: existing ids keep their slot, new ids append.
        seen: set[str] = set()
        merged: list[WoTThing] = []
        for t in [*self._things, *added]:
            if t.id not in seen:
                seen.add(t.id)
                merged.append(by_id[t.id])
        self._things = merged
        self._reindex()
        # Keep authorization in lockstep with the fleet. The PDP holds a CLOSED
        # vocabulary (the grantable (thing, affordance, op) tuples) built from the
        # thing set. Without this refresh, a runtime-added device's ops are
        # ungrantable (silently denied even with a correct grant) and a replaced
        # device's old ops linger as a stale over-grant. Refresh only on this
        # runtime-mutation path, not in _reindex, so a caller's construction-time
        # PDP vocabulary stays authoritative.
        if self._pdp is not None and hasattr(self._pdp, "vocabulary"):
            from thingctx.authz.vocabulary import build_vocabulary

            self._pdp.vocabulary = build_vocabulary(self._things)
        return [t.id for t in added]

    async def aclose(self) -> None:
        """Release any pooled transport resources (e.g. an binding's reused
        HTTP client). Safe to call more than once."""
        for inv in self._bindings:
            closer = getattr(inv, "aclose", None)
            if closer is not None:
                await closer()

    async def __aenter__(self) -> ThingClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def list_actions(self) -> list[dict[str, Any]]:
        """OpenAI-format tool specs for every exposed action."""
        return self._tool_specs

    def as_tools(self):
        """Return (tool_specs, invoke) to drive the Thing from your own
        agent loop. invoke is the same coroutine as self.invoke."""
        return self._tool_specs, self.invoke

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        return self._tool_specs

    def action_for(self, tool_name: str) -> WoTAction | None:
        return self._route.get(tool_name)

    @property
    def things(self) -> list[WoTThing]:
        return self._things

    def set_approval(
        self, approve: Approver | None, *, approve_when: ApprovePolicy | None = None
    ) -> None:
        """Set or replace the approval gate after construction. The MCP bridge
        uses this to bind an approver to the live server session (which does
        not exist when the client is built)."""
        self._approve = approve
        if approve_when is not None:
            self._approve_when = approve_when

    def guarded(
        self,
        pdp: PolicyDecisionPoint,
        *,
        identity: Any = None,
        authz_raise: bool = True,
    ) -> ThingClient:
        """Return a client that authorizes every device-reaching call against
        ``pdp`` for ``identity``. Sugar over the ``pdp=`` constructor param.

        This is NOT a proxy. It returns a ThingClient that shares this client's
        internal state (the same parsed Things, binding registry, route,
        property/event/media maps, approve gate) with only the authorization
        settings set. So there is no second dispatch surface to drift from and
        nothing to bypass: the returned client's own dispatch methods enforce
        the check, exactly as if you had passed ``pdp=`` at construction.
        """
        clone = object.__new__(type(self))
        clone.__dict__ = dict(self.__dict__)
        clone._pdp = pdp
        clone._identity = identity
        clone._authz_raise = authz_raise
        return clone

    async def _authorize(self, affordance: Any, op: str) -> Any:
        """Authorize ``op`` on a resolved affordance object. Returns None to
        proceed; raises :class:`AuthorizationDenied` (or, when
        ``authz_raise`` is False, returns an error envelope) on deny.

        Callers pass the SAME affordance object the method is about to dispatch
        (from ``_route`` / ``_props`` / ``_events`` / ``_media``), so the tuple
        authorized is exactly the tuple that would run. The decision does not
        read the form scheme, so a multi-transport affordance hits one check
        whichever transport it would route to; ``form_scheme`` is carried for
        audit only. Runs before any binding is selected: a denied call never
        reaches a transport.
        """
        from thingctx.authz.pdp import AccessRequest, AuthorizationDenied

        form = affordance.primary_form(prefer=self._prefer)
        request = AccessRequest(
            thing_id=affordance.thing_id,
            affordance=affordance.name,
            op=op,
            form_scheme=(form.scheme if form is not None else None),
        )
        decision = await self._pdp.decide(self._identity, request)
        if decision.permit:
            return None
        if self._authz_raise:
            raise AuthorizationDenied(request, decision.reason)
        return {
            "error": "authorization denied",
            "thing": request.thing_id,
            "affordance": request.affordance,
            "op": request.op,
            "reason": decision.reason,
        }

    def _authz_request(self, affordance: Any, op: str) -> AccessRequest:
        """Build the AccessRequest for a stream re-check (subscribe/media)."""
        from thingctx.authz.pdp import AccessRequest

        return AccessRequest(thing_id=affordance.thing_id, affordance=affordance.name, op=op)

    async def invoke(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoke one action by routing to the transport its form names.
        ``arguments`` defaults to ``{}`` (for no-input actions)."""
        arguments = arguments or {}
        if tool_name in self._media:
            return {
                "error": f"{tool_name} is a media stream; consume it with client.frames(...)",
                "media": True,
            }
        action = self._route.get(tool_name)
        if action is None:
            return {"error": f"unknown action: {tool_name}"}
        # Authorize first, approve second: authorization is the access boundary
        # (may I touch this at all?), the approve gate is a trust prompt on top
        # of an allowed call. A denied call must not prompt for approval.
        if self._pdp is not None:
            denied = await self._authorize(action, "invokeaction")
            if denied is not None:
                return denied
        blocked = await gate_action(
            action, tool_name, arguments, approve=self._approve, policy=self._approve_when
        )
        if blocked is not None:
            return blocked
        form = action.primary_form(prefer=self._prefer)
        if form is None:
            return {"error": f"action {tool_name} has no form (no transport)"}
        binding = self._registry.resolve(form)
        if binding is None:
            return {
                "error": (
                    f"no binding for transport {form.scheme!r} (action {tool_name}); register one"
                ),
                "transport": form.scheme,
            }
        # Resolve uriVariables: {id} fills from args and leaves the body.
        import dataclasses

        href, rest = form.fill(arguments or {})
        filled = dataclasses.replace(form, href=href) if href != form.href else form
        return await binding.invoke(action, filled, rest)

    def list_properties(self) -> list[str]:
        return list(self._props)

    def list_events(self) -> list[str]:
        return list(self._events)

    def list_media(self) -> list[str]:
        """Names of media affordances (continuous audio/video streams). Consume
        them with frames(); they are not in list_actions()."""
        return list(self._media)

    def media_form(self, name: str):
        """The media form backing a media affordance, or None. Lets callers read
        the form's media hint (e.g. a snapshot default) without reaching in."""
        action = self._media.get(name)
        if action is None:
            return None
        return next((f for f in action.forms if is_media_form(f)), None)

    async def read_property(self, name: str) -> Any:
        """Read a property's current value."""
        prop = self._props.get(name)
        if prop is None:
            return {"error": f"unknown property: {name}"}
        if self._pdp is not None:
            denied = await self._authorize(prop, "readproperty")
            if denied is not None:
                return denied
        form = prop.primary_form(prefer=self._prefer)
        binding = self._registry.resolve(form) if form else None
        if binding is None or not hasattr(binding, "read"):
            return {"error": f"no readable transport for property {name}"}
        return await binding.read(prop, form)

    async def write_property(self, name: str, value: Any) -> Any:
        """Write a property's value. Read-only properties are rejected."""
        prop = self._props.get(name)
        if prop is None:
            return {"error": f"unknown property: {name}"}
        # Authorize before the read-only check: a read-only property has no
        # writeproperty tuple in the TD-derived vocabulary, so a write to it is
        # an authorization denial (not in vocabulary), and must surface as one
        # rather than as a capability envelope.
        if self._pdp is not None:
            denied = await self._authorize(prop, "writeproperty")
            if denied is not None:
                return denied
        if not prop.writable:
            return {"error": f"property {name} is read-only"}
        blocked = await gate_write(
            prop.thing_id, name, value, approve=self._approve, policy=self._approve_when
        )
        if blocked is not None:
            return blocked
        form = prop.primary_form(prefer=self._prefer)
        binding = self._registry.resolve(form) if form else None
        if binding is None or not hasattr(binding, "write"):
            return {"error": f"no writable transport for property {name}"}
        return await binding.write(prop, form, value)

    async def subscribe(self, name: str):
        """Subscribe to an event or observable property. Returns an async
        iterator that yields each pushed value.

            async for reading in await client.subscribe("pump.telemetry"):
                ...
        """
        # WoT subscribe covers two ops: an event is subscribeevent, an
        # observable property is observeproperty. Resolve which from where the
        # name lives (events first, matching the original lookup order) so the
        # right op is authorized.
        event = self._events.get(name)
        if event is not None:
            target, op = event, "subscribeevent"
        else:
            prop = self._props.get(name)
            if prop is None:
                return _empty_aiter(f"unknown event/property: {name}")
            target, op = prop, "observeproperty"
        # Two enforcement points for a stream, because it is not request/reply:
        # 1. gate at establish time (an ungranted caller never subscribes).
        if self._pdp is not None:
            denied = await self._authorize(target, op)
            if denied is not None:
                # Yield a single denial envelope as a stream so `async for`
                # sees it (raise path already raised inside _authorize).
                return _single_denial_aiter(denied)
        form = target.primary_form(prefer=self._prefer)
        binding = self._registry.resolve(form) if form else None
        if binding is None or not hasattr(binding, "subscribe"):
            return _empty_aiter(f"no subscribable transport for {name}")
        bare = target.name
        stream = await binding.subscribe(bare, form)
        # 2. per-delivery filter: the token can expire while the stream lives,
        # so re-authorize before each value and stop the stream on lapse.
        if self._pdp is not None:
            from thingctx.authz.pdp import _authorized_stream

            return _authorized_stream(
                stream, self._pdp, self._identity, self._authz_request(target, op)
            )
        return stream

    async def frames(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        track: str = "video",
    ):
        """Open a media affordance and yield decoded frames. Returns an async
        iterator; ``track`` selects video or audio.

            async for frame in await client.frames("cam-1.watch"):
                ...
        """
        action = self._media.get(name)
        if action is None:
            return _empty_aiter(f"unknown media affordance: {name}")
        # A media affordance is a WoT action, so it is authorized as
        # invokeaction (the op its form declares), gated before the device
        # stream opens and re-checked per frame.
        if self._pdp is not None:
            denied = await self._authorize(action, "invokeaction")
            if denied is not None:
                return _single_denial_aiter(denied)
        form = next((f for f in action.forms if is_media_form(f)), None)
        binding = self._registry.resolve(form) if form else None
        if binding is None or not hasattr(binding, "frames"):
            return _empty_aiter(f"no media transport for {name}; register MediaBinding")
        import dataclasses

        href, rest = form.fill(arguments or {})
        filled = dataclasses.replace(form, href=href) if href != form.href else form
        stream = binding.frames(action, filled, rest, track=track)
        if self._pdp is not None:
            from thingctx.authz.pdp import _authorized_stream

            return _authorized_stream(
                stream, self._pdp, self._identity, self._authz_request(action, "invokeaction")
            )
        return stream

    async def publish(
        self,
        name: str,
        frames,
        arguments: dict[str, Any] | None = None,
        *,
        track: str = "video",
    ) -> None:
        """Push an async iterator of frames to a media affordance's ingest
        target (a URL or a file). The outbound mirror of ``frames()``; returns
        when the source is exhausted.

            await client.publish("studio.broadcast", frame_source())
        """
        action = self._media.get(name)
        if action is None:
            raise KeyError(f"unknown media affordance: {name}")
        # Publish reaches the device (a write of a live signal). Media is a WoT
        # action, so authorize invokeaction before selecting a transport.
        if self._pdp is not None:
            denied = await self._authorize(action, "invokeaction")
            if denied is not None:
                return denied
        form = next((f for f in action.forms if is_media_form(f)), None)
        binding = self._registry.resolve(form) if form else None
        if binding is None or not hasattr(binding, "publish"):
            raise RuntimeError(f"no media transport for {name}; register MediaBinding")
        import dataclasses

        href, rest = form.fill(arguments or {})
        filled = dataclasses.replace(form, href=href) if href != form.href else form
        await binding.publish(action, filled, frames, rest, track=track)

    async def verify(self, thing_id: str | None = None) -> list[VerifyReport]:
        """Ground each Thing's TD against the live endpoint: read every
        readable property and check it answers and matches its declared type.
        Read-only and safe (actions are never invoked). Returns one report per
        Thing; ``thing_id`` limits it to a single Thing.

            for report in await client.verify():
                assert report.ok, report.as_dict()
        """
        things = [t for t in self._things if thing_id is None or t.id == thing_id]
        return [await verify_thing(self, t) for t in things]


async def _empty_aiter(err: str):
    if False:  # pragma: no cover, make this an async generator
        yield None
    import warnings

    warnings.warn(err, stacklevel=2)


async def _single_denial_aiter(envelope: Any):
    """Yield one authorization-denial envelope as a stream, so a stream-shaped
    denial (subscribe/frames with authz_raise=False) is visible to `async for`
    rather than silently empty."""
    yield envelope


def to_text(value: Any) -> str:
    """Render an invoke result as text (used by the LLM host)."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return str(value)
