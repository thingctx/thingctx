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
from typing import Any

from thingctx.bindings import BindingRegistry, ProtocolBinding, default_bindings
from thingctx.bindings.builtin.media import is_media_form
from thingctx.thing import (
    SCALAR_INPUT_KEY,
    WoTAction,
    WoTThing,
    actions_to_tools,
    is_wrapped_input,
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
        self._tool_specs, self._route = actions_to_tools(
            self._things,
            only_idempotent=only_idempotent,
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

        # Preferred transport order = the order bindings were given.
        self._prefer = tuple(
            s for inv in self._bindings for s in (getattr(inv, "schemes", None) or (inv.scheme,))
        )

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
        # A scalar or array action input is projected to the model wrapped
        # under a single key (thing._project_input); unwrap it here so the
        # transport receives the bare value the TD declared, not the envelope.
        if is_wrapped_input(action.input_schema) and isinstance(arguments, dict):
            arguments = arguments.get(SCALAR_INPUT_KEY)
        # Resolve uriVariables: {id} fills from args and leaves the body.
        import dataclasses

        if isinstance(arguments, dict):
            href, rest = form.fill(arguments)
            filled = dataclasses.replace(form, href=href) if href != form.href else form
            return await binding.invoke(action, filled, rest)
        # An unwrapped scalar/array body has no uriVariables to fill.
        return await binding.invoke(action, form, arguments)

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
        target = self._events.get(name) or self._props.get(name)
        if target is None:
            return _empty_aiter(f"unknown event/property: {name}")
        form = target.primary_form(prefer=self._prefer)
        binding = self._registry.resolve(form) if form else None
        if binding is None or not hasattr(binding, "subscribe"):
            return _empty_aiter(f"no subscribable transport for {name}")
        bare = target.name
        return await binding.subscribe(bare, form)

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
        form = next((f for f in action.forms if is_media_form(f)), None)
        binding = self._registry.resolve(form) if form else None
        if binding is None or not hasattr(binding, "frames"):
            return _empty_aiter(f"no media transport for {name}; register MediaBinding")
        import dataclasses

        href, rest = form.fill(arguments or {})
        filled = dataclasses.replace(form, href=href) if href != form.href else form
        return binding.frames(action, filled, rest, track=track)

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


def to_text(value: Any) -> str:
    """Render an invoke result as text (used by the LLM host)."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return str(value)
