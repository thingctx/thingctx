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

import contextlib
import logging
import os
import secrets
import sys
import time
from typing import TYPE_CHECKING, Any, cast

from thingctx.runtime import ThingClient, to_text
from thingctx.thing import TOOL_SEP, _tool_slug

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mcp import types
    from mcp.server.lowlevel import Server

    from thingctx.trust import ApprovePolicy

logger = logging.getLogger("thingctx.mcp")

# Upper bound on frames a single snapshot tool call will decode and return, so a
# client cannot request an arbitrarily large in-memory image batch.
_MAX_SNAPSHOT_FRAMES = 32

# How long a parked approval stays redeemable. Bounds the window in which a
# leaked token could release a parked destructive call; a confirm that arrives
# later than this re-runs the action and parks it afresh.
_APPROVAL_TTL_S = 300.0


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


class _NeedsManualApproval(Exception):  # noqa: N818 (a control-flow signal, not an error)
    """Raised by the approver when the connected client cannot show an
    elicitation dialog. The bridge catches it and returns a pending-approval
    envelope with a token; the user then approves by calling the ``approve``
    tool (a chat-native human-in-the-loop that works on ANY client, not only
    elicitation-capable ones). Without this, a gated call on a non-eliciting
    client (e.g. Claude Desktop) would hang or silently deny."""


def _client_can_elicit(session: Any) -> bool:
    """True if the connected client declared the elicitation capability at
    initialize. A client that did not cannot answer session.elicit(), so asking
    would hang; the bridge routes to the approve-tool path instead."""
    from mcp import types

    check = getattr(session, "check_client_capability", None)
    if check is None:
        return False
    try:
        return bool(check(types.ClientCapabilities(elicitation=types.ElicitationCapability())))
    except Exception:
        return False


def _elicit_approver(server: Any) -> Callable[[Any], Awaitable[bool]]:
    """An approver that asks the connected MCP client to confirm a gated call.

    If the client supports MCP elicitation, ask via a dialog and honor the
    answer. If it does NOT (many GUI clients today), raise _NeedsManualApproval
    so the bridge falls back to the approve-tool flow, rather than hanging on an
    elicit nobody answers or silently denying. Denies only when there is no live
    session at all (a gate with nobody to open stays shut)."""

    async def approve(req: Any) -> bool:
        try:
            session = server.request_context.session
        except Exception:
            return False
        if not _client_can_elicit(session):
            # No dialog channel: hand off to the approve tool via the bridge.
            raise _NeedsManualApproval()
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
        except Exception:
            raise _NeedsManualApproval() from None
        return getattr(result, "action", None) == "accept"

    return approve


def build_mcp_server(
    client: ThingClient,
    *,
    name: str = "thingctx",
    approve: Any = "elicit",
    approve_when: ApprovePolicy | None = None,
    event_history: int = 16,
    tool_mode: str | None = None,
) -> Server:
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
    import os

    from mcp import types
    from mcp.server.lowlevel import Server
    from pydantic import AnyUrl

    # Tool projection mode. The flat surface (one tool per action) grows with the
    # fleet: both the tool count and the context they cost every turn. The gateway
    # surface is a constant set of generic verbs (search_things / describe /
    # invoke_action / read_property / write_property) that reach the fleet through
    # arguments, so the count never grows. Events are read only through the
    # start/read/stop background trio (a tool cannot hold a live stream), which
    # forwards a parameterized event's uriVariables (e.g. mqtt broker/topic).
    #
    # DEFAULT IS "auto": flat while the flat surface stays at or under flat_max
    # tools, gateway once it would exceed it. Flat's per-Thing names (mqtt__publish)
    # match user intent, so they win tool selection against a client's own code
    # sandbox; the gateway's generic invoke_action does not, so it is bypass-prone
    # in an open agent. A large surface, though, sprawls under flat, so it flips to
    # the constant gateway surface. Force either with THINGCTX_TOOL_MODE.
    flat_max = 60
    tool_mode = (tool_mode or os.environ.get("THINGCTX_TOOL_MODE") or "auto").strip().lower()
    if tool_mode not in ("gateway", "flat", "auto"):
        tool_mode = "auto"
    if tool_mode == "auto":
        # The flat surface size is the tool-spec count (one per action, plus the
        # property setters/cancels the flat route adds); compare to flat_max.
        _flat_n = len(client.tool_specs)
        tool_mode = "flat" if _flat_n <= flat_max else "gateway"
        # Best effort: a broken stderr must not sink the server startup.
        with contextlib.suppress(Exception):
            print(
                f"thingctx-mcp: tool mode = {tool_mode} (auto: {_flat_n} actions "
                f"{'<=' if tool_mode == 'flat' else '>'} {flat_max})",
                file=sys.stderr,
            )

    # Instructions the client puts in the model's system context at initialize.
    # This is the strongest steer thingctx has against the agent bypassing a Thing
    # to write raw code/shell for the same service: the tools are the sanctioned
    # path (the agent never holds credentials; each operation is policy-gated), so
    # reaching the service any other way loses that guarantee. Selection is still
    # the client's call — thingctx cannot force it — but this moves the default.
    _how_to = (
        "use search_things to find one, describe to read its schema, then "
        "invoke_action / read_property / write_property"
        if tool_mode == "gateway"
        else "call the matching tool directly"
    )
    _instructions = (
        "These tools drive real devices and services described as W3C Web of Things "
        "Things (an MQTT broker, a camera, a filesystem, an API, and so on). When a "
        "request maps to one of these Things, USE THESE TOOLS to do it — "
        f"{_how_to}. Do NOT write code, shell commands, or use a separate client "
        "(e.g. mosquitto_pub, curl, a paho script) to reach the same service directly. "
        "The tools are the sanctioned path: the agent never handles the credentials, "
        "and every operation is checked against the configured policy, so a gated or "
        "risky action is refused or asks for your approval. Reaching the service by "
        "raw code bypasses that protection. If a tool reports needs_approval, ask the "
        "user, and only on an explicit yes call approve with the token."
    )
    server: Server = Server(name, instructions=_instructions)
    from thingctx.gateway import GATEWAY_TOOL_NAMES, GATEWAY_TOOLS

    gateway = client.gateway() if tool_mode == "gateway" else None
    # The gateway's own ``subscribe_event`` returns a live stream, which a direct
    # Python caller can iterate but MCP cannot carry. So over MCP there is ONE
    # event-read model: the background-subscription trio (start/read/stop). It
    # covers both the persistent case and the quick "listen a moment" case (start,
    # read after a beat, stop), so a separate collect verb would be redundant
    # surface. subscribe_event is therefore dropped from the MCP gateway surface.
    # Background-subscription verbs: receive an event's messages BETWEEN prompts,
    # not only during a call. Available in both tool modes.
    _bg_sub_tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "start_subscription",
                "description": (
                    "Start a background subscription to an event: its messages buffer as "
                    "they arrive, even between your turns. Returns a subscription_id. Call "
                    "read_subscription later to drain what accumulated, stop_subscription to "
                    "end it. For a parameterized event (e.g. mqtt) pass broker/topic in arguments."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "thing_id": {"type": "string"},
                        "event": {"type": "string"},
                        "arguments": {
                            "type": "object",
                            "description": "the event's uriVariables (e.g. broker, topic)",
                        },
                    },
                    "required": ["thing_id", "event"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_subscription",
                "description": (
                    "Drain the messages a background subscription has buffered since your last "
                    "read. Returns messages, a dropped count (how many were shed if the buffer "
                    "overflowed), and ended (whether the source has closed)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"subscription_id": {"type": "string"}},
                    "required": ["subscription_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "stop_subscription",
                "description": "End a background subscription started with start_subscription.",
                "parameters": {
                    "type": "object",
                    "properties": {"subscription_id": {"type": "string"}},
                    "required": ["subscription_id"],
                },
            },
        },
    ]
    _bg_sub_names = {t["function"]["name"] for t in _bg_sub_tools}
    # A media (RTSP/video) affordance is not a discrete-message event; it is
    # captured as an image. In gateway mode it gets its own verb so the surface
    # stays consistent (one shape for every affordance), instead of a per-Thing
    # <slug>__snapshot tool the model must cross over to.
    _snapshot_verb = {
        "type": "function",
        "function": {
            "name": "snapshot",
            "description": (
                "Capture a still image (or a short clip) from a Thing's media stream "
                "(a video/RTSP affordance, shown as media in describe). Pass the media "
                "affordance's uriVariables in arguments (e.g. host, path). frames > 1 "
                "returns a short clip sampled over time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "thing_id": {"type": "string"},
                    "affordance": {
                        "type": "string",
                        "description": "the media affordance (from describe)",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "its uriVariables (e.g. host, path)",
                    },
                    "seconds": {"type": "number"},
                    "frames": {"type": "integer", "minimum": 1},
                    "every": {"type": "number"},
                },
                "required": ["thing_id", "affordance"],
            },
        },
    }
    _has_media = bool(client.list_media())
    # The approve tool: the chat-native half of the human-in-the-loop. When a gated
    # action can't be confirmed by an elicitation dialog, the bridge parks it and
    # returns a token; the user says "yes" and the agent calls approve(token) to
    # run it. Present unless the approval gate is off (approve_when="never").
    _approve_tool = {
        "type": "function",
        "function": {
            "name": "approve",
            "description": (
                "Approve and run an action that was parked awaiting your confirmation. "
                "Pass the approval_token from the needs_approval result. Only call this "
                "after the user has explicitly confirmed they want the action to proceed."
            ),
            "parameters": {
                "type": "object",
                "properties": {"approval_token": {"type": "string"}},
                "required": ["approval_token"],
            },
        },
    }
    # The effective approval policy: the explicit param wins, else whatever the
    # client was built with (the env-driven THINGCTX_APPROVE_WHEN flows onto the
    # client, not this param). "never" means no gate, so no approve tool.
    _effective_approve_when = (
        approve_when if approve_when is not None else getattr(client, "_approve_when", "declared")
    )
    _approvals_on = (_effective_approve_when or "").strip().lower() != "never"
    _gateway_specs = (
        # Drop the projection's streaming subscribe_event (MCP can't carry a stream);
        # the trio below is the single event-read model over MCP.
        [t for t in GATEWAY_TOOLS if t["function"]["name"] != "subscribe_event"]
        + _bg_sub_tools
        + ([_snapshot_verb] if _has_media else [])
        + ([_approve_tool] if _approvals_on else [])
    )
    _gateway_names = (
        (GATEWAY_TOOL_NAMES - {"subscribe_event"})
        | _bg_sub_names
        | ({"snapshot"} if _has_media else set())
        | ({"approve"} if _approvals_on else set())
    )
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
        slug = _tool_slug(media_name)
        tool_name = f"{slug}{TOOL_SEP}snapshot"
        if tool_name in media_tools:
            tool_name = f"{media_name}{TOOL_SEP}snapshot"
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
    async def list_tools() -> list[types.Tool]:
        out = []
        valid = set(types.ToolAnnotations.model_fields)
        if gateway is not None:
            # Gateway mode: a constant verb surface, not one tool per action.
            read_only = {"search_things", "describe", "read_property", "read_subscription"}
            for spec in _gateway_specs:
                fn = spec["function"]
                nm = fn["name"]
                out.append(
                    types.Tool(
                        name=nm,
                        description=fn["description"],
                        inputSchema=fn["parameters"],
                        annotations=types.ToolAnnotations(
                            readOnlyHint=nm in read_only,
                            destructiveHint=False,
                            idempotentHint=nm in read_only,
                        ),
                    )
                )
            # In gateway mode media is the `snapshot` VERB (added to _gateway_specs
            # above), not per-Thing <slug>__snapshot tools, so the surface keeps one
            # shape for every affordance. Nothing more to append here.
            return out
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
        # Background-subscription verbs (flat mode): only when the registry has an
        # event to subscribe to, so a registry with no events shows no noise.
        if event_names:
            bg_read_only = {"read_subscription"}
            for spec in _bg_sub_tools:
                fn = spec["function"]
                nm = fn["name"]
                out.append(
                    types.Tool(
                        name=nm,
                        description=fn["description"],
                        inputSchema=fn["parameters"],
                        annotations=types.ToolAnnotations(
                            readOnlyHint=nm in bg_read_only,
                            destructiveHint=False,
                            idempotentHint=nm in bg_read_only,
                        ),
                    )
                )
        # The approve tool (flat mode): the chat-native confirmation for a gated
        # action when the client can't show an elicitation dialog.
        if _approvals_on:
            fn = _approve_tool["function"]
            out.append(
                types.Tool(
                    name=fn["name"],
                    description=fn["description"],
                    inputSchema=fn["parameters"],
                    annotations=types.ToolAnnotations(
                        readOnlyHint=False, idempotentHint=False, destructiveHint=False
                    ),
                )
            )
        return out

    async def _snapshot(name: str, args: dict) -> Any:
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
            async for fr in await client.frames(name, args, track="video"):
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
            async def _from(start: float) -> AsyncIterator[Any]:
                async for fr in await client.frames(name, args, track="video"):
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

    # Background subscriptions: a start/read/stop trio so an event's messages
    # accumulate BETWEEN tool calls (a plain collect would only capture during its own
    # in-flight window). A start spawns a background task that fills the event's
    # form (broker/topic) once, subscribes, and buffers into a bounded ring as
    # messages arrive; read drains what accumulated since the last read; stop
    # cancels. Bounded ring so a firehose can't exhaust memory; a dropped counter
    # makes a fallen-behind reader visible rather than silently lossy. In-session
    # only (the ring lives in this process); durable cross-restart buffering is a
    # store's job, not the client's.
    _subs: dict[str, dict] = {}
    _sub_counter = [0]
    _sub_ring = 200

    # Pending approvals: when a gated call can't be confirmed by an elicitation
    # dialog (the client has none), it is parked here under a token, and the user
    # completes it by calling the approve tool. This makes the human-in-the-loop
    # work on any client, not only elicitation-capable ones. The token is a
    # random secret (never a guessable sequence: on a shared HTTP transport a
    # predictable token would let one caller release another's parked call) and
    # each entry expires after _APPROVAL_TTL_S.
    _pending: dict[str, dict] = {}

    async def _sub_pump(sub_id: str, tool: str, sub_args: dict) -> None:
        state = _subs[sub_id]
        try:
            stream = await client.subscribe(tool, sub_args)
            state["stream"] = stream
            async for value in stream:
                ring: collections.deque = state["ring"]
                if len(ring) == ring.maxlen:
                    state["dropped"] += 1  # this append evicts an unread message
                state["seq"] += 1
                ring.append((state["seq"], value))
        except asyncio.CancelledError:
            raise
        except Exception:
            state["ended"] = True
            logger.warning("background subscription %s stopped", sub_id, exc_info=True)

    async def _start_subscription(args: dict) -> Any:
        from thingctx.thing import _tool_name, thing_slug

        thing_id = str(args.get("thing_id") or "")
        event = str(args.get("event") or "")
        sub_args = dict(args.get("arguments") or {})
        thing = next((t for t in client.things if thing_slug(t.id) == thing_id), None)
        if thing is None:
            return {"error": f"no Thing {thing_id!r}"}
        if event not in thing.events:
            return {"error": f"no event {event!r} on {thing_id!r}", "events": sorted(thing.events)}
        _sub_counter[0] += 1
        sub_id = f"{thing_id}:{event}:{_sub_counter[0]}"
        _subs[sub_id] = {
            "ring": collections.deque(maxlen=_sub_ring),
            "dropped": 0,
            "seq": 0,
            "ended": False,
            "stream": None,
            "thing_id": thing_id,
            "event": event,
        }
        tool = _tool_name(thing.id, event)
        _subs[sub_id]["task"] = asyncio.create_task(_sub_pump(sub_id, tool, sub_args))
        return {
            "subscription_id": sub_id,
            "thing_id": thing_id,
            "event": event,
            "note": (
                "buffering in the background; call read_subscription to drain, "
                "stop_subscription to end"
            ),
        }

    async def _read_subscription(args: dict) -> Any:
        sub_id = str(args.get("subscription_id") or "")
        state = _subs.get(sub_id)
        if state is None:
            return {"error": f"no subscription {sub_id!r}", "active": sorted(_subs)}
        ring: collections.deque = state["ring"]
        batch = list(ring)
        ring.clear()
        dropped = state["dropped"]
        state["dropped"] = 0
        return {
            "subscription_id": sub_id,
            "messages": [v for _, v in batch],
            "count": len(batch),
            "dropped": dropped,  # messages shed since last read (ring overflow)
            "seq": batch[-1][0] if batch else state["seq"],
            "ended": state["ended"],  # the source stream closed; no more will arrive
        }

    async def _stop_subscription(args: dict) -> Any:
        sub_id = str(args.get("subscription_id") or "")
        state = _subs.pop(sub_id, None)
        if state is None:
            return {"error": f"no subscription {sub_id!r}"}
        task = state.get("task")
        if task is not None:
            task.cancel()
        stream = state.get("stream")
        aclose = getattr(stream, "aclose", None) if stream is not None else None
        if aclose is not None:
            # Teardown: a stream that errors while closing is already being torn
            # down, so the failure has nowhere useful to go.
            with contextlib.suppress(Exception):
                await aclose()
        return {"subscription_id": sub_id, "stopped": True, "remaining": len(state["ring"])}

    def _gateway_connect_target(verb: str, args: dict) -> str | None:
        """The flat tool name a gateway verb ultimately drives, for the on-demand
        connect check. ``invoke_action``/``read_property``/``write_property`` name
        their target Thing and affordance in the arguments; map them to the same
        ``<slug>__<name>`` the flat route would use so a Thing needing sign in is
        recognized. Returns None when the target can't be resolved (the verb then
        runs and the projection returns a clear not-found error)."""
        from thingctx.thing import _tool_name, thing_slug

        thing_id = str(args.get("thing_id") or "")
        affordance = str(args.get("action") or args.get("property") or args.get("event") or "")
        if not thing_id or not affordance:
            return None
        for t in client.things:
            if thing_slug(t.id) == thing_id:
                return _tool_name(t.id, affordance)
        return None

    def _tool_result(payload: Any) -> types.CallToolResult:
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

    async def _run_gated_call(
        tool: str, args: dict, *, bypass_approval: bool = False
    ) -> types.CallToolResult:
        """Run a tool through the gateway or the flat client, catching the
        can't-elicit approval signal. On that signal the call is parked under a
        token and a needs_approval envelope is returned, so the user can confirm
        via the approve tool (works on clients with no elicitation dialog).

        ``bypass_approval`` runs the call with the approval gate temporarily off,
        used only to replay a call the user already approved via the approve tool.
        The policy/authorization gate is NOT bypassed; only the human confirm is."""
        prior = None
        if bypass_approval:
            # Swap in an always-yes approver for this one call, then restore. The
            # PDP (policy) still runs; only the human-confirm step is satisfied.
            prior = client._approve

            async def _yes(_req: Any) -> bool:
                return True

            client.set_approval(_yes)
        try:
            if gateway is not None and tool in _gateway_names:
                return _tool_result(await gateway.call_tool(tool, args))
            return _tool_result(await client.call_tool(tool, args))
        except _NeedsManualApproval:
            if not _approvals_on:
                # Shouldn't happen (gate off => no approver raises), but fail safe.
                return _tool_result({"error": "approval required but the gate is off"})
            token = secrets.token_urlsafe(32)
            _pending[token] = {"tool": tool, "args": args, "created": time.monotonic()}
            summary = tool
            if isinstance(args, dict) and args.get("thing_id") and args.get("action"):
                summary = f"{args['action']} on {args['thing_id']}"
            return _tool_result(
                {
                    "needs_approval": True,
                    "approval_token": token,
                    "action": summary,
                    "message": (
                        "This action changes external state and needs your confirmation. "
                        "If the user approves, call approve with this approval_token; "
                        "otherwise do not."
                    ),
                }
            )
        finally:
            if bypass_approval:
                client.set_approval(prior)

    @server.call_tool()
    async def call_tool(tool: str, args: dict) -> Any:
        args = args or {}
        from thingctx.integrations.connect import CONNECT_TOOL, connect_tool, ensure_connected

        try:
            session = server.request_context.session
        except Exception:
            session = None
        # The explicit connect tool: the agent (or the user) drives a sign in.
        if tool == CONNECT_TOOL:
            return _tool_result(await connect_tool(client, args, session))
        # Connect a user-authorized Thing on demand: if this tool needs a sign in
        # the store does not have yet, confirm and run the one-time browser consent
        # locally, so the agent never handles the token. In gateway mode the verb
        # is generic (invoke_action/read_property/write_property) and the target
        # Thing lives in the arguments, so resolve the underlying flat tool name
        # first, else the connect check cannot find the Thing that needs auth.
        connect_target = tool
        if gateway is not None and tool in _gateway_names and tool not in _bg_sub_names:
            connect_target = _gateway_connect_target(tool, args) or tool
        connect_err = await ensure_connected(client, connect_target, session)
        if connect_err is not None:
            return _tool_result({"error": connect_err})
        if tool in media_tools:
            return await _snapshot(media_tools[tool], args)
        # Gateway snapshot verb: resolve thing_id + affordance to the media name,
        # then reuse the same _snapshot path the per-Thing tool uses.
        if tool == "snapshot" and gateway is not None:
            from thingctx.thing import _tool_name, thing_slug

            thing_id = str(args.get("thing_id") or "")
            affordance = str(args.get("affordance") or "")
            thing = next((t for t in client.things if thing_slug(t.id) == thing_id), None)
            if thing is None:
                return _tool_result({"error": f"no Thing {thing_id!r}"})
            media_name = _tool_name(thing.id, affordance)
            if media_name not in set(client.list_media()):
                return _tool_result(
                    {
                        "error": f"{affordance!r} on {thing_id!r} is not a media stream",
                        "media": [m for m in client.list_media() if m.startswith(thing_id + "__")],
                    }
                )
            # _snapshot reads uriVariables from its args dict; pass arguments merged
            # with the frame controls (seconds/frames/every).
            snap_args = dict(args.get("arguments") or {})
            for k in ("seconds", "frames", "every"):
                if k in args:
                    snap_args[k] = args[k]
            return await _snapshot(media_name, snap_args)
        # Background-subscription verbs, available in both modes: buffer an event's
        # messages between calls, drain on demand.
        if tool == "start_subscription":
            return _tool_result(await _start_subscription(args))
        if tool == "read_subscription":
            return _tool_result(await _read_subscription(args))
        if tool == "stop_subscription":
            return _tool_result(await _stop_subscription(args))
        # The approve tool: run a call the user just confirmed, with the gate
        # bypassed for THIS call only (the human already said yes).
        if tool == "approve" and _approvals_on:
            # Expire stale entries first, so an abandoned approval cannot sit
            # redeemable forever and an aged token is refused, not run.
            now = time.monotonic()
            for stale in [k for k, v in _pending.items() if now - v["created"] > _APPROVAL_TTL_S]:
                _pending.pop(stale, None)
            token = str(args.get("approval_token") or "")
            parked = _pending.pop(token, None)
            if parked is None:
                return _tool_result(
                    {"error": f"no pending approval {token!r} (it may have expired or already run)"}
                )
            return await _run_gated_call(parked["tool"], parked["args"], bypass_approval=True)
        # Every other tool runs through the gated dispatcher, which turns a
        # can't-elicit approval into a pending-approval envelope + token.
        return await _run_gated_call(tool, args)

    # Properties -> readable resources; events -> resources draining the recent
    # pushed payloads. Observable properties and events are also subscribable.
    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        def _prop_resource(prop_name: str) -> types.Resource:
            prop = client.property_for(prop_name)
            tag = " (observable)" if prop is not None and prop.observable else ""
            return types.Resource(
                uri=AnyUrl(_prop_uri(prop_name)),
                name=prop_name,
                description=f"Property {prop_name}{tag}",
            )

        out = [_prop_resource(prop_name) for prop_name in client.list_properties()]
        out.extend(
            types.Resource(uri=AnyUrl(_event_uri(ev)), name=ev, description=f"Event {ev}")
            for ev in event_names
        )
        return out

    @server.list_resource_templates()
    async def list_resource_templates() -> list[types.ResourceTemplate]:
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
    async def read_resource(uri: Any) -> str:
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

    async def _pump(uri: str, name: str, session: Any) -> None:
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
        except Exception:
            logger.warning("subscription relay for %s stopped", uri, exc_info=True)
        finally:
            pumps.pop(uri, None)

    @server.subscribe_resource()
    async def subscribe_resource(uri: Any) -> None:
        u = str(uri)
        name = subscribable.get(u)
        if name is None or u in pumps:
            return  # not subscribable, or already relaying
        try:
            session = server.request_context.session
        except Exception:
            return
        pumps[u] = asyncio.create_task(_pump(u, name, session))

    @server.unsubscribe_resource()
    async def unsubscribe_resource(uri: Any) -> None:
        task = pumps.pop(str(uri), None)
        if task is not None:
            task.cancel()

    # tc:PromptTemplate actions -> prompts
    from thingctx.extensions.prompts import get_prompt, list_prompts

    @server.list_prompts()
    async def list_prompts_handler() -> list[types.Prompt]:
        return [
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
            for p in list_prompts(client)
        ]

    @server.get_prompt()
    async def get_prompt_handler(name: str, arguments: dict | None) -> types.GetPromptResult:
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
    registry: Any,
    credentials: dict | None = None,
    approve_when: ApprovePolicy = "declared",
    verbose: bool = False,
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
    # THINGCTX_BLOCK_PRIVATE=1 refuses outbound requests to private, loopback,
    # and link-local hosts (cloud metadata included). Off by default: a laptop
    # operator legitimately drives LAN devices; a server/gateway operator sets
    # it to close SSRF from untrusted TDs or arguments.
    block_private = (os.environ.get("THINGCTX_BLOCK_PRIVATE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    # Skip a transport whose optional dependency is not installed; a real
    # construction error must still surface, so catch ImportError only.
    with contextlib.suppress(ImportError):
        from thingctx.bindings import HttpBinding

        bindings.append(HttpBinding(credentials=credentials or {}, block_private=block_private))
    with contextlib.suppress(ImportError):
        from thingctx.bindings import MqttBinding

        bindings.append(MqttBinding())
    with contextlib.suppress(ImportError):
        from thingctx.bindings.builtin.media import MediaBinding

        bindings.append(MediaBinding(credentials=credentials or {}, block_private=block_private))
    # THINGCTX_POLICY picks a coarse per-operation posture: read-only or
    # full. THINGCTX_IDENTITY (optional) gives the agent a named principal so the
    # decision, the audit, and any AuthZEN subject carry WHO acted, not just WHAT was
    # allowed. When either is set, wire a PDP so a denied op is refused before the
    # service is touched; unset means no PDP (previous behavior, unchanged). Build the
    # PDP's closed vocabulary from these TDs up front (the client does not rebuild it at
    # construction).
    pdp = None
    identity = None
    policy = os.environ.get("THINGCTX_POLICY")
    agent_identity = os.environ.get("THINGCTX_IDENTITY")
    if policy or agent_identity:
        from thingctx.authz.pdp import (
            GrantSource,
            LocalPolicyGrantSource,
            PolicyDecisionPoint,
            StaticGrantSource,
        )
        from thingctx.authz.vocabulary import build_vocabulary
        from thingctx.thing import parse_thing

        things = [parse_thing(td) for td in tds if isinstance(td, dict)]
        # An identity-free posture (policy only) grants the preset wildcard; an
        # identity binds the SAME preset grant to a named role, so the principal
        # appears in the decision and the audit. Default an identity-only run to
        # read-only (a named agent with no stated posture reads, never writes).
        preset = policy or "read-only"
        # Build the preset grant ONCE (safe-action aware for read-only) via
        # StaticGrantSource, then either serve it directly (no identity) or bind it to a
        # named role (identity). Passing ``things`` lets read-only permit safe actions.
        static = StaticGrantSource(preset, things=things)
        grants: GrantSource
        if agent_identity:
            # The identity IS a claims dict: its name is the subject and its role, so
            # a role -> grant map keys the fine grant off the coarse preset. Standard:
            # this maps cleanly to an AuthZEN subject (sub + roles claim).
            identity = {"sub": agent_identity, "roles": [agent_identity]}
            grants = LocalPolicyGrantSource({agent_identity: static.grants})
        else:
            grants = static
        pdp = PolicyDecisionPoint(build_vocabulary(things), grants)
        if verbose:
            print(f"thingctx-mcp: operation policy = {preset}", file=sys.stderr)
            if agent_identity:
                print(f"thingctx-mcp: agent identity = {agent_identity}", file=sys.stderr)
    return ThingClient(
        tds=tds, bindings=bindings, approve_when=approve_when, pdp=pdp, identity=identity
    )


def _build_server(registry: Any) -> Server:
    """Build the MCP server over a registry of TDs, shared by every transport.

    Per-Thing secrets are read from the environment (THINGCTX_TOKEN_<SLUG>)
    and bound to each Thing's declared security scheme, so authenticated
    surfaces are drivable without baking secrets into any TD.
    """
    creds = _credentials_from_env()
    # Trust policy from the environment; default "declared" honors exactly what
    # each TD marks risky. An unrecognized value clamps to the safe default rather
    # than degrading silently downstream. The server wires an elicitation approver,
    # so a gated tool prompts the client user to confirm before it runs.
    raw_when = os.environ.get("THINGCTX_APPROVE_WHEN", "declared")
    # The membership test IS the validation; mypy does not narrow `in` over a
    # Literal, hence the cast.
    approve_when = cast(
        "ApprovePolicy",
        raw_when if raw_when in ("declared", "destructive", "all", "never") else "declared",
    )
    client = client_from_registry(
        registry, credentials=creds, approve_when=approve_when, verbose=True
    )
    if creds:
        print(
            f"thingctx-mcp: loaded {len(creds)} credential(s) for {', '.join(sorted(creds))}",
            file=sys.stderr,
        )
    print(f"thingctx-mcp: approval policy = {approve_when}", file=sys.stderr)
    # build_mcp_server resolves and prints the effective tool mode (auto -> flat/gateway
    # by fleet size), so no mode line here to avoid a stale/duplicate report.
    n = len(client.things)
    name = client.things[0].title if n == 1 else f"things ({n})"
    return build_mcp_server(client, name=name or "things")


async def serve(registry: Any) -> None:
    """Run the MCP server over stdio (the local, one-per-session transport)."""
    from mcp.server.stdio import stdio_server

    server = _build_server(registry)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _check_http_exposure(host: str) -> None:
    """Warn (or refuse) when the HTTP transport binds a non-loopback host.

    The streamable-http endpoint performs no inbound authentication, so binding
    beyond loopback hands the whole tool surface, driven with this process's
    credentials, to anyone who can reach the port. A warning (not a refusal)
    keeps a deploy behind an authenticating reverse proxy working; setting
    ``THINGCTX_REQUIRE_AUTH=1`` turns the same condition into a startup error
    for operators who want the hard stop."""
    if (host or "").strip().lower() in ("127.0.0.1", "localhost", "::1"):
        return
    if (os.environ.get("THINGCTX_REQUIRE_AUTH") or "").strip().lower() in ("1", "true", "yes"):
        raise SystemExit(
            "thingctx-mcp: refusing to serve HTTP on a non-loopback host: "
            "THINGCTX_REQUIRE_AUTH=1 is set and the HTTP transport has no inbound "
            "authentication. Bind 127.0.0.1, or put an authenticating reverse "
            "proxy or gateway guard in front and unset THINGCTX_REQUIRE_AUTH."
        )
    print(
        "thingctx-mcp: WARNING: serving HTTP on a non-loopback host with NO inbound "
        "authentication. Anyone who can reach this port can drive every exposed tool "
        "with this process's credentials. Do not expose it to an untrusted network; "
        "put an authenticating reverse proxy or gateway guard in front. Set "
        "THINGCTX_REQUIRE_AUTH=1 to make this condition fatal.",
        file=sys.stderr,
    )


def serve_http(registry: Any, *, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the MCP server over streamable HTTP (the remote transport).

    A long lived server that many callers reach by URL, for a hosted gateway or a
    cloud agent runtime that only accepts a remote MCP endpoint. streamable-http is
    the go forward remote transport; legacy SSE is not served. Bind 0.0.0.0 in a
    container; keep 127.0.0.1 as the default so a bare run is not exposed by accident.
    The endpoint itself performs no inbound authentication: a non-loopback bind
    warns at startup, and refuses when ``THINGCTX_REQUIRE_AUTH=1`` is set.
    """
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    _check_http_exposure(host)
    server = _build_server(registry)
    manager = StreamableHTTPSessionManager(app=server)

    @contextlib.asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        async with manager.run():
            yield

    async def handle(scope: Any, receive: Any, send: Any) -> None:
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
