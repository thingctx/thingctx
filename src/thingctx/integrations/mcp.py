# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A generic MCP server over a registry of WoT Things.

A registry is anything that yields TDs: a folder, a URL, or a W3C Thing
Description Directory (see thingctx.registry). Every Thing's actions
become MCP tools (namespaced by Thing, so no collisions), readable
properties become MCP resources, and tc:PromptTemplate actions become MCP
prompts. Any MCP client (Claude CLI, Copilot CLI, ...) drives the whole
fleet. You write no MCP server per device: each TD already describes its
actions and their transports, and thingctx routes each call over the
transport the TD names.

    thingctx-mcp ./registry/                 # a folder of *.td.json
    thingctx-mcp tdd:https://hub.local     # a TD Directory
    thingctx-mcp https://device/.well-known/wot   # a single TD URL

In an MCP client config (e.g. Claude CLI .mcp.json):

    { "mcpServers": { "things": {
        "command": "thingctx-mcp", "args": ["tdd:https://hub.local"] } } }

A Thing whose forms are ``local://`` is implemented by a live in-process
object, which a TD alone cannot supply. An installed package provides that
object through the ``thingctx.local_handlers`` entry point group, where each
entry point is named for a Thing slug and resolves to a zero-argument callable
returning the handler (an object whose methods are the actions, or a mapping
of action name to callable):

    [project.entry-points."thingctx.local_handlers"]
    pump = "my_pkg:make_pump"   # -> the object backing urn:...:pump

The binary imports only the handlers whose slug matches a Thing in the served
registry; no flag or TD edit is needed, and the TD names no implementation.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from thingctx.runtime import ThingClient, to_text

logger = logging.getLogger("thingctx.mcp")

# Upper bound on frames a single snapshot tool call will decode and return, so a
# client cannot request an arbitrarily large in-memory image batch.
_MAX_SNAPSHOT_FRAMES = 32


def _credentials_from_env() -> dict[str, str]:
    """Collect per-Thing secrets from the environment.

    ``THINGCTX_TOKEN_<SLUG>=<secret>`` binds a secret to the Thing whose slug
    is ``<SLUG>`` (lowercased, with ``_`` mapped to ``-`` so
    ``THINGCTX_TOKEN_GOOGLE_MAPS`` -> ``google-maps``). The slug is the same
    one used in tool names. The secret is applied per the Thing's declared
    scheme (bearer/basic/apikey). Secrets live only in the process
    environment, never in a TD or on disk here.
    """
    prefix = "THINGCTX_TOKEN_"
    creds: dict[str, str] = {}
    for key, val in os.environ.items():
        if key.startswith(prefix) and val:
            slug = key[len(prefix) :].lower().replace("_", "-")
            if slug:
                creds[slug] = val
    return creds


def _elicit_approver(server):
    """An approver that asks the connected MCP client (Claude/Copilot CLI) to
    confirm a gated call, via MCP elicitation. Denies if the client cannot
    elicit or there is no live session , a gate with nobody to open it stays
    shut. This is the human-in-the-loop for the CLI integrations."""

    async def approve(req) -> bool:
        try:
            session = server.request_context.session
        except Exception:  # noqa: BLE001  (no active request/session)
            return False
        # Show the argument names, not their values: an argument can carry a
        # secret or PII, and the elicitation message is shown to the user and may
        # be logged by the client.
        arg_names = ", ".join(sorted((req.arguments or {}).keys()))
        message = f"Approve {req.tool_name}({arg_names})?  Reason: {req.reason}." + (
            f"  {req.description}" if req.description else ""
        )
        try:
            # An empty object schema asks for a plain accept / decline / cancel.
            result = await session.elicit(
                message=message, requestedSchema={"type": "object", "properties": {}}
            )
        except Exception:  # noqa: BLE001  (client has no elicitation capability)
            return False
        return getattr(result, "action", None) == "accept"

    return approve


def build_mcp_server(
    client: ThingClient,
    *,
    name: str = "thingctx",
    approve: Any = "elicit",
    approve_when: str | None = None,
    event_history: int = 16,
):
    """Build an mcp Server that bridges `client` to MCP. Needs the `mcp`
    package.

    The trust gate (thingctx.trust) is enforced on the same ``client.invoke``
    path used here, so risky tools are gated for MCP clients too. ``approve``:
    ``"elicit"`` (default) asks the connected client to confirm a gated call,
    but only installs elicitation when the client has no approver yet, so an
    approver the caller already configured is never clobbered; a callable uses
    your own approver; ``None`` leaves the client's gate as-is. ``approve_when``
    overrides the client's policy (declared/destructive/all/never).

    ``event_history`` is the per-event ring size: how many recent payloads are
    buffered between a client's reads so a burst is delivered whole rather than
    collapsed to the latest (see the event resource read).
    """
    import collections

    import mcp.types as types
    from mcp.server.lowlevel import Server

    server: Server = Server(name)
    if callable(approve):
        client.set_approval(approve, approve_when=approve_when)
    elif approve == "elicit" and client._approve is None:
        client.set_approval(_elicit_approver(server), approve_when=approve_when)
    elif approve_when is not None:
        client.set_approval(client._approve, approve_when=approve_when)

    import asyncio

    # Map each media affordance (``<slug>.<name>``) to a ``<slug>.snapshot`` MCP
    # tool name, disambiguating the rare Thing with several media streams.
    media_tools: dict[str, str] = {}  # mcp tool name -> media affordance name
    for media_name in client.list_media():
        slug = media_name.split(".", 1)[0]
        tool_name = f"{slug}.snapshot"
        if tool_name in media_tools:
            tool_name = f"{media_name}.snapshot"
        media_tools[tool_name] = media_name

    # Events and observable properties are push streams. MCP carries push via
    # resources/subscribe -> resources/updated: each becomes a subscribable
    # resource; the notification names the URI and the client re-reads.
    #
    # An observable property has a current value, so its read is a live read. An
    # event has no on-demand value and each occurrence matters, so a bounded ring
    # of recent payloads is buffered per event and drained on read: the client
    # gets every occurrence since its last read (up to the ring size), a monotonic
    # ``seq`` to order them, and a ``dropped`` count so a gap (more unread events
    # than the ring holds) is detectable rather than silent.
    event_names = list(client.list_events())
    observable_props = [
        n
        for n in client.list_properties()
        if (p := client.property_for(n)) is not None and p.observable
    ]

    # Safe, read-only actions with a single uriVariable become parameterized
    # resource reads (MCP resource templates), e.g. thing://<tool>/{id}, in
    # addition to staying callable as tools.
    template_reads: dict[str, str] = {}  # tool name -> uriVariable name
    for spec in client.list_actions():
        nm = spec["function"]["name"]
        act = client.action_for(nm)
        uvars = list(getattr(act, "uri_variables", None) or {}) if act is not None else []
        if act is not None and act.read_only and len(uvars) == 1:
            template_reads[nm] = uvars[0]

    def _prop_uri(name: str) -> str:
        return f"thing://{name}"

    def _event_uri(name: str) -> str:
        return f"event://{name}"

    subscribable: dict[str, str] = {  # subscribable uri -> bare affordance name
        **{_prop_uri(n): n for n in observable_props},
        **{_event_uri(n): n for n in event_names},
    }
    history = max(1, event_history)
    event_log: dict[str, collections.deque] = {}  # event uri -> deque[(seq, value)]
    event_dropped: dict[str, int] = {}  # event uri -> unread values shed since last read
    event_seq: dict[str, int] = {}  # event uri -> next sequence number
    pumps: dict[str, asyncio.Task] = {}  # uri -> background push task

    # The whole tool surface comes from client.tool_surface() (the same source
    # the LLM host uses), so the two stay in step: actions, an <action>.cancel
    # for each long-running action, and a <property>.set for each writable
    # property. Reads stay MCP resources, so property.get entries are skipped.
    # An action carries MCP annotations derived from the TD's semantics; a
    # `tc:mcp` block on the action passes any annotation through (and overrides).
    @server.list_tools()
    async def list_tools():
        out = []
        valid = set(types.ToolAnnotations.model_fields)
        for entry in client.tool_surface():
            if entry["kind"] == "property.get":
                continue
            name = entry["name"]
            ann = None
            output_schema = None
            if entry["kind"] == "action":
                action = client.action_for(name)
                if action is not None:
                    hints: dict = {
                        "destructiveHint": action.is_destructive(),
                        "idempotentHint": bool(action.read_only),
                        "readOnlyHint": bool(action.read_only) and not action.is_destructive(),
                    }
                    explicit = action.raw.get("tc:mcp") or action.raw.get("mcp") or {}
                    hints.update({k: v for k, v in explicit.items() if k in valid})
                    ann = types.ToolAnnotations(**hints)
                    # A long-running action returns a status envelope, not its
                    # raw output, so only advertise outputSchema for a
                    # synchronous action (whose structuredContent matches it).
                    if not client._is_async(action):
                        output_schema = entry.get("output_schema") or None
            elif entry["kind"] in ("property.set", "action.cancel"):
                ann = types.ToolAnnotations(
                    readOnlyHint=False, idempotentHint=True, destructiveHint=False
                )
            out.append(
                types.Tool(
                    name=name,
                    description=entry["description"],
                    inputSchema=entry["input_schema"],
                    outputSchema=output_schema,
                    annotations=ann,
                )
            )
        # Media affordances are continuous streams; MCP can't carry a stream
        # (and has no video content type), but it can carry images. Each becomes
        # a read only ``<slug>.snapshot`` tool that returns one still by default,
        # or a short burst of frames (``frames`` > 1) the model reads as a clip.
        for tool_name, media_name in media_tools.items():
            out.append(
                types.Tool(
                    name=tool_name,
                    description=(
                        f"Capture frames from the {media_name} media stream and "
                        "return them as images: one still by default, or a short "
                        "clip (set frames > 1) sampled over time."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "seconds": {
                                "type": "number",
                                "description": "Seconds to seek before the first frame.",
                            },
                            "frames": {
                                "type": "integer",
                                "description": "How many frames to return (1 = a single still).",
                                "minimum": 1,
                            },
                            "every": {
                                "type": "number",
                                "description": "Seconds between sampled frames when frames > 1.",
                            },
                        },
                    },
                    annotations=types.ToolAnnotations(
                        readOnlyHint=True, idempotentHint=True, destructiveHint=False
                    ),
                )
            )
        # Expose a single ``connect`` tool when the registry has a user-authorized
        # Thing, so the agent can offer to sign you in and see what still needs it.
        # The consent still confirms with you before any browser opens.
        from thingctx.integrations.connect import CONNECT_TOOL, connect_status

        if connect_status(client):
            out.append(
                types.Tool(
                    name=CONNECT_TOOL,
                    description=(
                        "Sign in to a service this agent can reach that needs your "
                        "consent (e.g. a calendar). Call with no argument to list "
                        "what needs connecting, or with a service name to connect it. "
                        "Opens a browser for you to approve; no password or token is "
                        "shared with the agent."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "thing": {
                                "type": "string",
                                "description": "The service to connect. Omit to list what needs it.",  # noqa: E501
                            }
                        },
                    },
                    annotations=types.ToolAnnotations(
                        readOnlyHint=False, idempotentHint=True, destructiveHint=False
                    ),
                )
            )
        return out

    async def _snapshot(name: str, args: dict):
        """Grab one frame (or a short burst) from a media affordance and return
        them as MCP image content."""
        import base64

        from thingctx.bindings.builtin.media.encode import frame_to_jpeg
        from thingctx.bindings.builtin.media.sample import sample_frames

        form = client.media_form(name)
        hint = (getattr(form, "raw", {}) or {}).get("x-thingctx-media") or {}
        default_at = hint.get("snapshot_at", 0) if isinstance(hint, dict) else 0
        seconds = float(args.get("seconds", default_at) or 0)
        # Cap the frame count: each frame is decoded, JPEG encoded, and base64
        # expanded in memory, so an unbounded request could exhaust it.
        count = min(_MAX_SNAPSHOT_FRAMES, max(1, int(args.get("frames", 1) or 1)))
        every = float(args.get("every", 1.0) or 1.0)

        if count == 1:
            frame = None
            async for fr in await client.frames(name, track="video"):
                # ``seconds`` is best-effort: a source whose frames carry no pts
                # (some live streams) would otherwise loop forever waiting for a
                # timestamp that never arrives, so the first frame ends it.
                if frame is None and fr.pts is None:
                    frame = fr
                    break
                frame = fr
                if not seconds or (fr.pts is not None and fr.pts >= seconds):
                    break
            picked = [frame] if frame is not None else []
        else:
            # Skip ahead to ``seconds`` first, then sample ``count`` frames.
            async def _from(start: float):
                async for fr in await client.frames(name, track="video"):
                    if not start or (fr.pts is not None and fr.pts >= start):
                        yield fr

            picked = await sample_frames(_from(seconds), count=count, every=every)

        if not picked:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"no frame from {name}")],
                isError=True,
            )
        return [
            types.ImageContent(
                type="image",
                data=base64.b64encode(frame_to_jpeg(fr)).decode("ascii"),
                mimeType="image/jpeg",
            )
            for fr in picked
        ]

    def _tool_result(payload: Any):
        """Wrap a runtime result as an MCP tool result: text for any client,
        structured content for those that use it, and ``isError`` when the
        runtime reports a failure (so a model is not told a failed call
        succeeded)."""
        is_error = isinstance(payload, dict) and "error" in payload
        structured = payload if isinstance(payload, dict) else {"result": payload}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=to_text(payload))],
            structuredContent=structured,
            isError=is_error,
        )

    @server.call_tool()
    async def call_tool(tool: str, args: dict):
        args = args or {}
        from thingctx.integrations.connect import CONNECT_TOOL, connect_tool, ensure_connected

        try:
            session = server.request_context.session
        except Exception:  # noqa: BLE001 - no live session (e.g. a piped call)
            session = None
        # The explicit connect tool: the agent (or the user) drives a sign in.
        if tool == CONNECT_TOOL:
            return _tool_result(await connect_tool(client, args, session))
        # Connect a user-authorized Thing on demand: if this tool needs a sign in
        # the store does not have yet, confirm and run the one-time browser consent
        # locally, so the agent never handles the token.
        connect_err = await ensure_connected(client, tool, session)
        if connect_err is not None:
            return _tool_result({"error": connect_err})
        if tool in media_tools:
            return await _snapshot(media_tools[tool], args)
        # client.call_tool dispatches actions (a long-running action blocks to
        # completion here), <property>.set writes, and <action>.cancel stops an
        # in-flight run; the trust gate is enforced on the same path.
        return _tool_result(await client.call_tool(tool, args))

    # Properties -> readable resources; events -> resources draining the recent
    # pushed payloads. Observable properties and events are also subscribable.
    @server.list_resources()
    async def list_resources():
        out = []
        for name in client.list_properties():
            prop = client.property_for(name)
            tag = " (observable)" if prop is not None and prop.observable else ""
            out.append(
                types.Resource(uri=_prop_uri(name), name=name, description=f"Property {name}{tag}")
            )
        for name in event_names:
            out.append(types.Resource(uri=_event_uri(name), name=name, description=f"Event {name}"))
        return out

    @server.list_resource_templates()
    async def list_resource_templates():
        # Parameterized reads: a safe action with one uriVariable is exposed as
        # a templated resource the client fills (e.g. thing://<tool>/{id}).
        return [
            types.ResourceTemplate(
                uriTemplate=f"thing://{nm}/{{{var}}}",
                name=nm,
                description=f"Read {nm} by {var}.",
            )
            for nm, var in template_reads.items()
        ]

    @server.read_resource()
    async def read_resource(uri):
        u = str(uri)
        if u.startswith("event://"):
            name = u.removeprefix("event://")
            buf = event_log.get(u)
            if not buf:
                return to_text({"event": name, "pending": True})
            batch = list(buf)
            buf.clear()
            # Drain: every occurrence since the last read, in order. ``seq`` is
            # the last delivered sequence number; ``dropped`` counts occurrences
            # shed before this read (a burst deeper than the ring), so a gap is
            # visible to the client rather than silent.
            return to_text(
                {
                    "event": name,
                    "values": [v for _, v in batch],
                    "count": len(batch),
                    "seq": batch[-1][0],
                    "dropped": event_dropped.pop(u, 0),
                }
            )
        if u.startswith("thing://"):
            rest = u.removeprefix("thing://")
            if "/" in rest:
                # a templated read: thing://<tool>/<value> -> invoke read action
                tool, value = rest.split("/", 1)
                if tool in template_reads:
                    return to_text(await client.invoke(tool, {template_reads[tool]: value}))
                return to_text({"error": f"unknown resource: {u}"})
            return to_text(await client.read_property(rest))
        return to_text({"error": f"unknown resource: {u}"})

    async def _pump(uri: str, name: str, session) -> None:
        """Relay a subscription onto MCP and notify the client that the resource
        changed (it then re-reads). Event payloads are buffered in a bounded ring
        so a burst is delivered whole on the next read; observable properties
        carry no buffer (their read is live). Always deregisters on exit, so a
        stream that ends (or a dropped connection) leaves the resource
        subscribable again."""
        is_event = uri.startswith("event://")
        try:
            stream = await client.subscribe(name)
            async for value in stream:
                if is_event:
                    buf = event_log.setdefault(uri, collections.deque(maxlen=history))
                    if len(buf) == history:
                        # The append below evicts an unread payload: a real gap.
                        event_dropped[uri] = event_dropped.get(uri, 0) + 1
                    event_seq[uri] = event_seq.get(uri, 0) + 1
                    buf.append((event_seq[uri], value))
                await session.send_resource_updated(uri)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  (a dead stream/session ends the pump)
            logger.warning("subscription relay for %s stopped", uri, exc_info=True)
        finally:
            pumps.pop(uri, None)

    @server.subscribe_resource()
    async def subscribe_resource(uri):
        u = str(uri)
        name = subscribable.get(u)
        if name is None or u in pumps:
            return  # not subscribable, or already relaying
        try:
            session = server.request_context.session
        except Exception:  # noqa: BLE001  (no live session to notify)
            return
        pumps[u] = asyncio.create_task(_pump(u, name, session))

    @server.unsubscribe_resource()
    async def unsubscribe_resource(uri):
        task = pumps.pop(str(uri), None)
        if task is not None:
            task.cancel()

    # tc:PromptTemplate actions -> prompts
    from thingctx.extensions.prompts import get_prompt, list_prompts

    @server.list_prompts()
    async def list_prompts_handler():
        out = []
        for p in list_prompts(client):
            out.append(
                types.Prompt(
                    name=p["name"],
                    description=p.get("description", ""),
                    arguments=[
                        types.PromptArgument(
                            name=a["name"],
                            description=a.get("description", ""),
                            required=a.get("required", False),
                        )
                        for a in p.get("arguments", [])
                    ],
                )
            )
        return out

    @server.get_prompt()
    async def get_prompt_handler(name: str, arguments: dict | None):
        messages = await get_prompt(client, name, arguments or {})
        return types.GetPromptResult(
            messages=[
                types.PromptMessage(
                    role=m.get("role", "user"),
                    content=types.TextContent(type="text", text=str(m.get("content", ""))),
                )
                for m in messages
            ]
        )

    return server


def client_from_registry(
    registry, credentials: dict | None = None, approve_when: str = "declared", verbose: bool = False
) -> ThingClient:
    """Build one ThingClient over all the TDs a registry yields, with the
    bindings whose deps are installed (local always; http/mqtt if
    importable). `registry` is anything with a fetch() -> list[dict].
    ``approve_when`` sets the trust policy (the MCP server wires the approver).

    ``verbose`` prints a startup line naming any bound local handlers; it is off
    by default so a piped ``thingctx list``/``invoke`` emits only its result. The
    MCP server (a long-running process, not a pipe) sets it.

    A Thing whose forms are ``local://`` needs a live in-process object; the
    binary binds one per Thing from the ``thingctx.local_handlers`` entry point
    group, keyed by Thing slug. Only handlers for Things present in this
    registry are imported. A single handler is bound directly (so its events
    push as usual); several are bound per slug so colliding action names stay
    distinct."""
    from thingctx.bindings import LocalBinding, discover_local_handlers
    from thingctx.thing import thing_slug

    tds = registry.fetch()
    present = {thing_slug(td["id"]): td for td in tds if isinstance(td, dict) and td.get("id")}
    handlers = discover_local_handlers(set(present))
    if len(handlers) == 1 and len(present) == 1:
        local = LocalBinding(next(iter(handlers.values())))
    else:
        local = LocalBinding()
        for slug, handler in handlers.items():
            local.register_thing(slug, handler)
    if handlers and verbose:
        print(
            f"thingctx-mcp: bound local handler(s) for {', '.join(sorted(handlers))}",
            file=sys.stderr,
        )
    bindings: list[Any] = [local]
    try:
        from thingctx.bindings import HttpBinding

        bindings.append(HttpBinding(credentials=credentials or {}))
    except Exception:  # noqa: BLE001
        pass
    try:
        from thingctx.bindings import MqttBinding

        bindings.append(MqttBinding())
    except Exception:  # noqa: BLE001
        pass
    try:
        from thingctx.bindings.builtin.media import MediaBinding

        bindings.append(MediaBinding())
    except Exception:  # noqa: BLE001
        pass
    return ThingClient(tds=tds, bindings=bindings, approve_when=approve_when)


def _build_server(registry):
    """Build the MCP server over a registry of TDs, shared by every transport.

    Per-Thing secrets are read from the environment (THINGCTX_TOKEN_<SLUG>)
    and bound to each Thing's declared security scheme, so authenticated
    surfaces are drivable without baking secrets into any TD.
    """
    creds = _credentials_from_env()
    # Trust policy from the environment; default "declared" honors exactly what
    # each TD marks risky. The server wires an elicitation approver, so a gated
    # tool prompts the client user to confirm before it runs.
    approve_when = os.environ.get("THINGCTX_APPROVE_WHEN", "declared")
    client = client_from_registry(
        registry, credentials=creds, approve_when=approve_when, verbose=True
    )
    if creds:
        print(
            f"thingctx-mcp: loaded {len(creds)} credential(s) for {', '.join(sorted(creds))}",
            file=sys.stderr,
        )
    print(f"thingctx-mcp: approval policy = {approve_when}", file=sys.stderr)
    n = len(client.things)
    name = client.things[0].title if n == 1 else f"things ({n})"
    return build_mcp_server(client, name=name or "things")


async def serve(registry) -> None:
    """Run the MCP server over stdio (the local, one-per-session transport)."""
    from mcp.server.stdio import stdio_server

    server = _build_server(registry)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def serve_http(registry, *, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the MCP server over streamable HTTP (the remote transport).

    A long lived server that many callers reach by URL, for a hosted gateway or a
    cloud agent runtime that only accepts a remote MCP endpoint. streamable-http is
    the go forward remote transport; legacy SSE is not served. Bind 0.0.0.0 in a
    container; keep 127.0.0.1 as the default so a bare run is not exposed by accident.
    """
    import contextlib

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    server = _build_server(registry)
    manager = StreamableHTTPSessionManager(app=server)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    async def handle(scope, receive, send):
        # The session manager wants the raw ASGI at the mount root; Mount at / (not
        # /mcp) so the client posts to the base URL with no trailing-slash redirect.
        await manager.handle_request(scope, receive, send)

    app = Starlette(routes=[Mount("/", app=handle)], lifespan=lifespan)
    print(f"thingctx-mcp: serving streamable-http on http://{host}:{port}/", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="thingctx-mcp",
        description="Serve a fleet of W3C WoT Things to an MCP client.",
    )
    parser.add_argument(
        "sources",
        nargs="+",
        metavar="SOURCE",
        help="a TD dir, file, URL, or tdd:url; one or more.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve over streamable HTTP instead of stdio (for a hosted gateway or a "
        "cloud agent runtime that takes a remote MCP URL).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default 8080).")
    args = parser.parse_args()

    from thingctx.registry import from_args

    registry = from_args(args.sources)
    if args.http:
        serve_http(registry, host=args.host, port=args.port)
    else:
        import asyncio

        asyncio.run(serve(registry))


if __name__ == "__main__":
    main()
