# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Gateway projection: a constant tool surface over any number of Things.

The flat projection (:func:`thingctx.thing.actions_to_tools`) gives a model one
tool per action. That is correct for a handful of Things and wrong for a fleet:
tool-selection accuracy falls as the list grows, and the tool definitions
themselves cost context every turn. Both scale with the number of TDs.

The gateway projection fixes the surface at six verbs, no matter how many Things
the client manages. The fleet is reached through *arguments*, not through an
ever-larger tool list:

    search_things(query)                -> lean summaries (id, title, actions)
    describe(thing_id[, affordance])    -> one Thing's affordances / a schema
    invoke_action(thing_id, action, arguments)
    read_property(thing_id, property)
    write_property(thing_id, property, value)
    subscribe_event(thing_id, event)

Six tools at ten Things, six at ten thousand. Each verb routes onto the existing
:class:`~thingctx.runtime.ThingClient` surface (``call_tool`` / ``read_property``
/ ``write_property`` / ``subscribe``), so transports, auth, and the approval gate
are untouched. This is a projection mode, not a new execution path.

``thing_id`` in every verb is the Thing's *slug* (``thing_slug``), the same short,
already-collision-checked key the flat route uses. The Thing and the action are
separate arguments, so the gateway never flattens many Things into one namespace.

The six verbs are intent-level, not the WoT operation vocabulary 1:1. Every verb
dispatches to a WoT-op-authorized call: the authorization layer keys grants on WoT
ops exactly (``(thing, affordance, invokeaction)``), and the gateway maps onto
those ops without being them. One ``subscribe_event`` folds ``observeproperty`` and
``subscribeevent``; the teardown ops (``unsubscribeevent`` / ``cancelaction``) and
bulk ops are runtime bookkeeping the verb set does not surface.

Over MCP the ``subscribe_event`` stream is dropped (MCP cannot carry a live
stream); events are read through the background subscription trio instead. The
direct-Python gateway keeps ``subscribe_event`` returning an iterable stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from thingctx.thing import _tool_name, thing_slug

if TYPE_CHECKING:
    from thingctx.runtime import ThingClient
    from thingctx.thing import WoTThing

# The six verbs. This list never grows with the fleet; that is the whole point.
GATEWAY_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_things",
            "description": (
                "Find Things by keyword over their title, description, type, and "
                "action names. Returns lean summaries; call describe for detail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "keywords to match"},
                    "limit": {"type": "integer", "description": "max results (default 8)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe",
            "description": (
                "Describe one Thing: its actions, properties, and events. With "
                "affordance set, return that affordance's full input schema."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "thing_id": {"type": "string", "description": "a Thing id from search_things"},
                    "affordance": {
                        "type": "string",
                        "description": "optional action/property/event name for its schema",
                    },
                },
                "required": ["thing_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "invoke_action",
            "description": (
                "Invoke an action on a Thing (send a command, publish a message, call an "
                "API, run an operation on a real device or service). Prefer this over "
                "writing code or a shell command to reach the same service directly: this "
                "path holds no credentials in the agent and is policy-gated. Get the "
                "action's schema first with describe."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "thing_id": {"type": "string"},
                    "action": {"type": "string"},
                    "arguments": {"type": "object", "description": "the action's input"},
                },
                "required": ["thing_id", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_property",
            "description": "Read a property of a Thing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thing_id": {"type": "string"},
                    "property": {"type": "string"},
                },
                "required": ["thing_id", "property"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_property",
            "description": "Write a property of a Thing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thing_id": {"type": "string"},
                    "property": {"type": "string"},
                    "value": {"description": "the new value"},
                },
                "required": ["thing_id", "property", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subscribe_event",
            "description": "Subscribe to an event of a Thing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thing_id": {"type": "string"},
                    "event": {"type": "string"},
                },
                "required": ["thing_id", "event"],
            },
        },
    },
]

# Every tool the gateway ever exposes, by name.
GATEWAY_TOOL_NAMES = frozenset(t["function"]["name"] for t in GATEWAY_TOOLS)


def _summary(thing: WoTThing) -> dict[str, Any]:
    """A lean summary: enough to decide relevance, not the whole Thing. The
    thing_id is the slug, the id every verb takes back as an argument."""
    return {
        "thing_id": thing_slug(thing.id),
        "title": thing.title or thing.id,
        "description": (thing.description or "")[:200],
        "actions": sorted(thing.actions),
        "properties": sorted(thing.properties),
        "events": sorted(thing.events),
    }


def _haystack(thing: WoTThing) -> str:
    """The searchable text of a Thing: title, description, @type local names,
    and every affordance name. Kept lowercase for case-insensitive matching."""
    parts: list[str] = [thing.title or "", thing.description or "", thing.id]
    parts += [t.split(":")[-1] for t in thing.at_type]
    parts += list(thing.actions) + list(thing.properties) + list(thing.events)
    for a in thing.actions.values():
        parts.append(a.description or "")
    return " ".join(parts).lower()


def keyword_search(things: list[WoTThing], query: str, limit: int = 8) -> list[WoTThing]:
    """Rank Things by how many query terms appear in their searchable text.

    Simple title/description/@type/affordance-name keyword match. Behind one
    function so a Thing Description Directory's own search (JSONPath, SPARQL,
    semantic) can replace it without touching the verbs.
    """
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return things[:limit]
    scored: list[tuple[int, int, WoTThing]] = []
    for i, thing in enumerate(things):
        hay = _haystack(thing)
        score = sum(1 for t in terms if t in hay)
        if score:
            # Higher score first; original order (i) breaks ties, so results are
            # deterministic and network-free (no clock, no random).
            scored.append((-score, i, thing))
    scored.sort()
    return [t for _, _, t in scored[:limit]]


class GatewayProjection:
    """A constant six-verb surface over a :class:`ThingClient`.

    The client already parses the TDs, resolves transports, binds auth, and holds
    the approval gate. The gateway adds only the projection: the fixed tool specs
    a model sees, and the routing from a generic verb call back onto the client's
    real methods. It never grows the tool list with the fleet.
    """

    def __init__(self, client: ThingClient) -> None:
        self._client = client
        # slug -> Thing, so a thing_id argument resolves in O(1) and an unknown
        # id can be answered with near-matches instead of a bare failure.
        self._by_slug: dict[str, WoTThing] = {thing_slug(t.id): t for t in client.things}

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        """The six verbs. Constant for any fleet size."""
        return GATEWAY_TOOLS

    def _resolve(self, thing_id: str) -> WoTThing:
        thing = self._by_slug.get(thing_id)
        if thing is None:
            near = [s for s in self._by_slug if thing_id.lower() in s.lower()][:5]
            hint = f"; did you mean {near}?" if near else ""
            raise KeyError(f"no Thing {thing_id!r}{hint}")
        return thing

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        """Dispatch one of the six verbs. Errors return an ``{"error": ...}``
        envelope (not a raise) so a tool-calling loop can read the reason and
        retry, the same contract the flat surface uses."""
        args = args or {}
        try:
            if name == "search_things":
                return self._search(args)
            if name == "describe":
                return self._describe(args)
            if name == "invoke_action":
                return await self._invoke(args)
            if name == "read_property":
                return await self._read(args)
            if name == "write_property":
                return await self._write(args)
            if name == "subscribe_event":
                return await self._subscribe(args)
        except KeyError as exc:
            # KeyError stringifies with surrounding quotes; unwrap to the message.
            return {"error": exc.args[0] if exc.args else str(exc)}
        else:
            return {"error": f"unknown gateway tool {name!r}"}

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", ""))
        limit = int(args.get("limit") or 8)
        hits = keyword_search(self._client.things, query, limit)
        return {"results": [_summary(t) for t in hits], "count": len(hits)}

    def _media_names(self, thing: WoTThing) -> set[str]:
        """The affordance names of ``thing`` that are media streams (RTSP/video),
        which are reached with the snapshot tool, not subscribe/read. Derived from
        the client's media map so describe can steer the model to the right tool."""
        try:
            media = set(self._client.list_media())
        except Exception:
            return set()
        out: set[str] = set()
        for aff in list(thing.actions) + list(thing.properties) + list(thing.events):
            if _tool_name(thing.id, aff) in media:
                out.add(aff)
        return out

    def _describe(self, args: dict[str, Any]) -> dict[str, Any]:
        thing = self._resolve(str(args.get("thing_id", "")))
        media = self._media_names(thing)
        affordance = args.get("affordance")
        if affordance:
            # Return the exact input schema at call time. This is what recovers the
            # typed-tool advantage the flat projection got from its definitions:
            # the model reads the schema as data, then invokes.
            name = str(affordance)

            def _media_steer(d: dict) -> dict:
                # A media (RTSP/video) affordance, whatever its WoT kind, is captured
                # with the snapshot verb, not invoked/subscribed. Flag it so the model
                # picks the right tool.
                if name in media:
                    d["media"] = True
                    d["how_to_read"] = (
                        "This is a live video stream. Capture it with the snapshot "
                        f"tool (thing_id={thing_slug(thing.id)}, affordance={name}, pass "
                        "its uriVariables e.g. host/path in arguments), not subscribe_event."
                    )
                return d

            if name in thing.actions:
                a = thing.actions[name]
                return _media_steer(
                    {
                        "thing_id": thing_slug(thing.id),
                        "affordance": name,
                        "kind": "action",
                        "description": a.description,
                        "input_schema": a.input_schema or {"type": "object"},
                        "output_schema": a.output_schema,
                    }
                )
            if name in thing.properties:
                p = thing.properties[name]
                return _media_steer(
                    {
                        "thing_id": thing_slug(thing.id),
                        "affordance": name,
                        "kind": "property",
                        "readable": getattr(p, "readable", True),
                        "writable": getattr(p, "writable", False),
                        "schema": getattr(p, "schema", None),
                    }
                )
            if name in thing.events:
                e = thing.events[name]
                return _media_steer(
                    {
                        "thing_id": thing_slug(thing.id),
                        "affordance": name,
                        "kind": "event",
                        "data_schema": e.data_schema,
                    }
                )
            return {"error": f"no affordance {name!r} on {thing_slug(thing.id)!r}"}
        # No affordance: the Thing overview.
        overview = {
            "thing_id": thing_slug(thing.id),
            "title": thing.title or thing.id,
            "description": thing.description or "",
            "actions": {n: (a.description or "") for n, a in thing.actions.items()},
            "properties": sorted(thing.properties),
            "events": sorted(thing.events),
        }
        if media:
            # Name the media streams and how to read them, so the model does not try
            # to subscribe_event a video feed (it uses the snapshot tool instead).
            overview["media"] = sorted(media)
            overview["media_hint"] = (
                f"Media affordances {sorted(media)} are live video streams: capture "
                "them with the snapshot tool, not subscribe_event."
            )
        return overview

    async def _invoke(self, args: dict[str, Any]) -> Any:
        thing = self._resolve(str(args.get("thing_id", "")))
        action = str(args.get("action", ""))
        if action not in thing.actions:
            return {
                "error": f"no action {action!r} on {thing_slug(thing.id)!r}",
                "actions": sorted(thing.actions),
            }
        tool = _tool_name(thing.id, action)
        return await self._client.call_tool(tool, dict(args.get("arguments") or {}))

    async def _read(self, args: dict[str, Any]) -> Any:
        thing = self._resolve(str(args.get("thing_id", "")))
        prop = str(args.get("property", ""))
        if prop not in thing.properties:
            return {"error": f"no property {prop!r}", "properties": sorted(thing.properties)}
        return await self._client.read_property(_tool_name(thing.id, prop))

    async def _write(self, args: dict[str, Any]) -> Any:
        thing = self._resolve(str(args.get("thing_id", "")))
        prop = str(args.get("property", ""))
        if prop not in thing.properties:
            return {"error": f"no property {prop!r}", "properties": sorted(thing.properties)}
        return await self._client.write_property(_tool_name(thing.id, prop), args.get("value"))

    async def _subscribe(self, args: dict[str, Any]) -> Any:
        thing = self._resolve(str(args.get("thing_id", "")))
        event = str(args.get("event", ""))
        if event not in thing.events:
            return {"error": f"no event {event!r}", "events": sorted(thing.events)}
        return await self._client.subscribe(_tool_name(thing.id, event))
