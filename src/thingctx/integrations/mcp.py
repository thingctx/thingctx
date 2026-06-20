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
"""

from __future__ import annotations

import os
import sys
from typing import Any

from thingctx.runtime import ThingClient, to_text


def _credentials_from_env() -> dict[str, str]:
    """Collect per-Thing secrets from the environment.

    ``THINGCTX_TOKEN_<SLUG>=<secret>`` binds a secret to the Thing whose slug
    is ``<SLUG>`` (lowercased, with ``_`` mapped to ``-`` so
    ``THINGCTX_TOKEN_GOOGLE_MAPS`` -> ``google-maps``). The slug is the same
    one used in tool names. The secret is applied per the Thing's declared
    scheme (bearer/basic/apikey). Secrets live only in the process
    environment -- never in a TD or on disk here.
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
        except Exception:  # noqa: BLE001 , no active request/session
            return False
        message = f"Approve {req.tool_name}({req.arguments})?  Reason: {req.reason}." + (
            f"  {req.description}" if req.description else ""
        )
        try:
            # An empty object schema asks for a plain accept / decline / cancel.
            result = await session.elicit(
                message=message, requestedSchema={"type": "object", "properties": {}}
            )
        except Exception:  # noqa: BLE001 , client has no elicitation capability
            return False
        return getattr(result, "action", None) == "accept"

    return approve


def build_mcp_server(
    client: ThingClient,
    *,
    name: str = "thingctx",
    approve: Any = "elicit",
    approve_when: str | None = None,
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
    """
    import mcp.types as types
    from mcp.server.lowlevel import Server

    server: Server = Server(name)
    if callable(approve):
        client.set_approval(approve, approve_when=approve_when)
    elif approve == "elicit" and client._approve is None:
        client.set_approval(_elicit_approver(server), approve_when=approve_when)
    elif approve_when is not None:
        client.set_approval(client._approve, approve_when=approve_when)

    # actions -> tools, carrying MCP annotations so a client can gate or
    # label them. The common hints are derived from the TD's own
    # semantics; a `tc:mcp` block on the action passes any MCP annotation
    # through verbatim (and overrides), so new MCP hints need no code here.
    @server.list_tools()
    async def list_tools():
        out = []
        valid = set(types.ToolAnnotations.model_fields)
        for spec in client.list_actions():
            fn = spec["function"]
            action = client.action_for(fn["name"])
            ann = None
            if action is not None:
                hints: dict = {
                    "destructiveHint": action.is_destructive(),
                    "idempotentHint": bool(action.idempotent),
                    "readOnlyHint": bool(action.idempotent) and not action.is_destructive(),
                }
                # passthrough: any MCP annotation declared on the action
                explicit = action.raw.get("tc:mcp") or action.raw.get("mcp") or {}
                hints.update({k: v for k, v in explicit.items() if k in valid})
                ann = types.ToolAnnotations(**hints)
            out.append(
                types.Tool(
                    name=fn["name"],
                    description=fn.get("description", ""),
                    inputSchema=fn.get("parameters", {"type": "object"}),
                    annotations=ann,
                )
            )
        return out

    @server.call_tool()
    async def call_tool(tool: str, args: dict):
        result = await client.invoke(tool, args or {})
        return [types.TextContent(type="text", text=to_text(result))]

    # readable properties -> resources
    def _prop_uri(name: str) -> str:
        return f"thing://{name}"

    @server.list_resources()
    async def list_resources():
        out = []
        for name in client.list_properties():
            out.append(
                types.Resource(uri=_prop_uri(name), name=name, description=f"Property {name}")
            )
        return out

    @server.read_resource()
    async def read_resource(uri):
        name = str(uri).replace("thing://", "")
        return to_text(await client.read_property(name))

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
    registry, credentials: dict | None = None, approve_when: str = "declared"
) -> ThingClient:
    """Build one ThingClient over all the TDs a registry yields, with the
    invokers whose deps are installed (local always; http/mqtt if
    importable). `registry` is anything with a fetch() -> list[dict].
    ``approve_when`` sets the trust policy (the MCP server wires the approver)."""
    from thingctx.invokers import LocalInvoker

    tds = registry.fetch()
    invokers: list[Any] = [LocalInvoker()]
    try:
        from thingctx.invokers import HttpInvoker

        invokers.append(HttpInvoker(credentials=credentials or {}))
    except Exception:  # noqa: BLE001
        pass
    try:
        from thingctx.invokers import MqttInvoker

        invokers.append(MqttInvoker())
    except Exception:  # noqa: BLE001
        pass
    return ThingClient(tds=tds, invokers=invokers, approve_when=approve_when)


async def serve(registry) -> None:
    """Run the stdio MCP server over a registry of TDs.

    Per-Thing secrets are read from the environment (THINGCTX_TOKEN_<SLUG>)
    and bound to each Thing's declared security scheme, so authenticated
    surfaces are drivable without baking secrets into any TD.
    """
    from mcp.server.stdio import stdio_server

    creds = _credentials_from_env()
    # Trust policy from the environment; default "declared" honors exactly what
    # each TD marks risky. The server wires an elicitation approver, so a gated
    # tool prompts the CLI user to confirm before it runs.
    approve_when = os.environ.get("THINGCTX_APPROVE_WHEN", "declared")
    client = client_from_registry(registry, credentials=creds, approve_when=approve_when)
    if creds:
        print(
            f"thingctx-mcp: loaded {len(creds)} credential(s) for {', '.join(sorted(creds))}",
            file=sys.stderr,
        )
    print(f"thingctx-mcp: approval policy = {approve_when}", file=sys.stderr)
    n = len(client.things)
    name = client.things[0].title if n == 1 else f"things ({n})"
    server = build_mcp_server(client, name=name or "things")
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "usage: python -m thingctx.integrations.mcp <dir | file | url | tdd:url> ...",
            file=sys.stderr,
        )
        raise SystemExit(2)
    import asyncio

    from thingctx.registry import from_args

    asyncio.run(serve(from_args(sys.argv[1:])))


if __name__ == "__main__":
    main()
