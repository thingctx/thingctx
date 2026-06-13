"""ThingClient: list and invoke a Thing's actions, read/write properties,
subscribe to events. No LLM.

    client = ThingClient(tds=[td], invokers=[HttpInvoker()])
    client.list_actions()
    await client.invoke("pump.set_speed", {"rpm": 1200})
"""

from __future__ import annotations

import json
from typing import Any

from thingctx.invokers import Invoker, select_invoker
from thingctx.thing import (
    WoTAction,
    WoTThing,
    actions_to_tools,
    parse_thing,
)


class ThingClient:
    """List + invoke the actions of one or more WoT Things. Transport-
    agnostic; no LLM."""

    @classmethod
    def from_registry(cls, registry, *, invokers=None, **kwargs) -> ThingClient:
        """Build a client over every TD a registry yields. `registry` is
        anything with fetch() -> list[dict] (see thingctx.registry)."""
        return cls(tds=registry.fetch(), invokers=invokers, **kwargs)

    def __init__(
        self,
        *,
        tds: list[dict[str, Any]],
        invokers: list[Invoker] | None = None,
        only_idempotent: bool = False,
        validate: bool = False,
    ) -> None:
        # validate=True checks each TD against the W3C TD 1.1 schema and
        # raises TDValidationError on nonconformance (needs [validate]).
        self._things: list[WoTThing] = [parse_thing(td, validate=validate) for td in tds]
        self._invokers = list(invokers or [])
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
        # Bind the TDs' declared security to any invoker that honors it, so
        # requests carry the right auth without the adopter wiring it. A
        # fleet-aware invoker (with_things) authenticates each call as its
        # owning Thing; otherwise fall back to the first Thing's schemes.
        for inv in self._invokers:
            if hasattr(inv, "with_things"):
                inv.with_things(self._things)
            elif hasattr(inv, "with_security") and self._things:
                inv.with_security(self._things[0])

        # Preferred transport order = the order invokers were given.
        self._prefer = tuple(
            s for inv in self._invokers for s in (getattr(inv, "schemes", None) or (inv.scheme,))
        )

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

    async def invoke(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoke one action by routing to the transport its form names.
        ``arguments`` defaults to ``{}`` (for no-input actions)."""
        arguments = arguments or {}
        action = self._route.get(tool_name)
        if action is None:
            return {"error": f"unknown action: {tool_name}"}
        form = action.primary_form(prefer=self._prefer)
        if form is None:
            return {"error": f"action {tool_name} has no form (no transport)"}
        invoker = select_invoker(self._invokers, form)
        if invoker is None:
            return {
                "error": (
                    f"no invoker for transport {form.scheme!r} (action {tool_name}); register one"
                ),
                "transport": form.scheme,
            }
        # Resolve uriVariables: {id} fills from args and leaves the body.
        import dataclasses

        href, rest = form.fill(arguments or {})
        filled = dataclasses.replace(form, href=href) if href != form.href else form
        return await invoker.invoke(action, filled, rest)

    def list_properties(self) -> list[str]:
        return list(self._props)

    def list_events(self) -> list[str]:
        return list(self._events)

    async def read_property(self, name: str) -> Any:
        """Read a property's current value."""
        prop = self._props.get(name)
        if prop is None:
            return {"error": f"unknown property: {name}"}
        form = prop.primary_form(prefer=self._prefer)
        invoker = select_invoker(self._invokers, form) if form else None
        if invoker is None or not hasattr(invoker, "read"):
            return {"error": f"no readable transport for property {name}"}
        return await invoker.read(prop, form)

    async def write_property(self, name: str, value: Any) -> Any:
        """Write a property's value. Read-only properties are rejected."""
        prop = self._props.get(name)
        if prop is None:
            return {"error": f"unknown property: {name}"}
        if not prop.writable:
            return {"error": f"property {name} is read-only"}
        form = prop.primary_form(prefer=self._prefer)
        invoker = select_invoker(self._invokers, form) if form else None
        if invoker is None or not hasattr(invoker, "write"):
            return {"error": f"no writable transport for property {name}"}
        return await invoker.write(prop, form, value)

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
        invoker = select_invoker(self._invokers, form) if form else None
        if invoker is None or not hasattr(invoker, "subscribe"):
            return _empty_aiter(f"no subscribable transport for {name}")
        bare = target.name
        return await invoker.subscribe(bare, form)


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
