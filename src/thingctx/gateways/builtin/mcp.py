# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""MCP gateway binding: serve a WoT fleet to MCP clients, KEEPING MCP's rich surface.

MCP is not a plain address-mapped bus. It has a protocol-specific middleware
surface a bus lacks: tools (actions), resources (properties), prompts
(``tc:PromptTemplate`` actions), media snapshots, and elicitation-based approval.
The MCP specifics ride in ``mcpv:`` form vocabulary that the engine passes through
opaquely, exactly as MQTT's specifics ride in ``mqv:``.

This driver is THIN. It does not reimplement the MCP projection; it COMPOSES with
:func:`thingctx.integrations.mcp.build_mcp_server`, the existing bridge, which
already maps actions/properties/prompts/media and routes every call through
``client.invoke``. Because that path carries the authorization/trust gate, authz
on the bus is preserved for free: a guarded ``ThingClient`` is not bypassable by
going through the MCP server (asserted in the tests).

The driver's two jobs:

* ``project_forms``: emit an ``mcp://`` form per affordance carrying ``mcpv:``
  vocabulary, so the projected TD is honest and discoverable about what MCP
  exposes for each affordance::

      mcpv:kind         "tool" | "resource" | "prompt"   the MCP surface it maps to
      mcpv:annotations  {readOnlyHint, idempotentHint, destructiveHint}  MCP tool hints

  The engine never sees these; a consumer reads its own ``mcpv:`` terms.
* ``serve``: stand up the existing MCP server over ``engine.client``.

Capabilities, advertised by presence (the anti-lowest-common-denominator rule):

* ``GatewayBinding`` (base) and ``RequestReply``: MCP is request/reply. A tool call
  returns a result, so the engine may route reply-bearing ops here.
* NOT ``EventMirroring``: the MCP bridge exposes actions + resources + prompts,
  not a live event push. This driver does not wire native events to MCP
  notifications, so it does NOT implement ``mirror_event``. The seam's whole
  point is to advertise only what you actually do; claiming EventMirroring
  without wiring it would be dishonest.
* NOT ``PubSubOnly`` (it replies), NOT ``QoSAware`` (MCP has no per-message QoS),
  NOT ``Announces`` (the MCP server's own list_tools/list_resources IS discovery).
"""

from __future__ import annotations

from typing import Any

from thingctx.gateways.engine import (
    INVOKE,
    READ,
    WRITE,
    Gateway,
    ServeRequest,
)
from thingctx.thing import TOOL_SEP, thing_slug

# The MCP surface each neutral op maps to. SUBSCRIBE has no MCP surface today (no
# live event push), so it gets no form and the projected TD stays honest.
_KIND_FOR_OP = {INVOKE: "tools", READ: "resources", WRITE: "resources"}


def _slug(thing: Any) -> str:
    return thing_slug(thing.id)


class McpGatewayBinding:
    """Serve a fleet to MCP clients. Implements GatewayBinding + RequestReply;
    not EventMirroring, PubSubOnly, QoSAware, or Announces (see the module
    docstring for why each is omitted).

    Args:
        server_name: the MCP server name a client sees; the projected ``mcp://``
            hrefs are rooted at it (``mcp://<server_name>/...``).
        approve: forwarded to ``build_mcp_server``; ``"elicit"`` (default) asks the
            connected client to confirm a gated call, a callable installs your own
            approver, ``None`` leaves the client's gate as-is.
        approve_when: forwarded to ``build_mcp_server`` to override the client's
            trust policy (declared/destructive/all/never).
    """

    scheme = "mcp"

    def __init__(
        self,
        server_name: str = "thingctx",
        *,
        approve: Any = "elicit",
        approve_when: str | None = None,
    ) -> None:
        self._server_name = server_name
        self._approve = approve
        self._approve_when = approve_when
        self._server: Any = None
        self._gateway: Gateway | None = None

    # -- GatewayBinding: projection ----------------------------------------- #

    def project_forms(self, thing: Any, affordance: str, op: str) -> list[dict]:
        """One mcp-faced form for this (affordance, op), carrying mcpv: vocab.

        The href is an ``mcp:`` URI keyed by the MCP surface (tools/resources) and
        the ``<slug>.<affordance>`` name the MCP server actually exposes. The MCP
        specifics (kind, tool annotation hints) ride under ``mcpv:`` so the engine
        passes them through without ever understanding them."""
        kind = _KIND_FOR_OP.get(op)
        if kind is None:
            return []  # an op MCP does not carry (e.g. subscribeevent) -> honest omit
        slug = _slug(thing)
        name = f"{slug}{TOOL_SEP}{affordance}"
        href = f"mcp://{self._server_name}/{kind}/{name}"
        form: dict[str, Any] = {"href": href, "op": [op]}
        # Protocol-specific vocabulary, namespaced. A consumer reads its own; the
        # engine never interprets these.
        form["mcpv:kind"] = _MCP_KIND[kind]
        if kind == "tools":
            form["mcpv:annotations"] = self._annotations(thing, affordance)
        return [form]

    def _annotations(self, thing: Any, affordance: str) -> dict:
        """The MCP tool annotation hints for an action, derived from the TD's own
        semantics (the same hints ``build_mcp_server`` puts on the live tool), so
        the projected form and the served tool agree."""
        action = thing.actions.get(affordance)
        if action is None:
            return {}
        destructive = _is_destructive(action)
        idempotent = bool(getattr(action, "idempotent", False))
        return {
            "readOnlyHint": idempotent and not destructive,
            "idempotentHint": idempotent,
            "destructiveHint": destructive,
        }

    # -- GatewayBinding: serve/teardown ------------------------------------- #

    async def serve(self, engine: Gateway) -> None:
        """Stand up the existing MCP server over the engine's native client.

        COMPOSES with ``build_mcp_server``: the MCP projection (actions->tools,
        properties->resources, prompts, media) and its ``client.invoke`` routing
        (which carries the authz/trust gate) are reused verbatim. This driver adds
        no per-device MCP logic; it just wires the server to the fleet's client."""
        from thingctx.integrations.mcp import build_mcp_server

        self._gateway = engine
        self._server = build_mcp_server(
            engine.client,
            name=self._server_name,
            approve=self._approve,
            approve_when=self._approve_when,
        )

    async def run_stdio(self) -> None:
        """Run the standing MCP server over stdio (the Claude/Copilot CLI path).

        ``serve`` builds the server; this pumps it. Kept separate so a test can
        build and drive the server in-memory without a stdio transport."""
        if self._server is None:
            raise RuntimeError("serve() must run before run_stdio()")
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read, write):
            await self._server.run(read, write, self._server.create_initialization_options())

    @property
    def server(self) -> Any:
        """The composed MCP server (available after ``serve``). A caller pumps it
        over stdio, or an in-memory session drives it directly in a test."""
        return self._server

    async def aclose(self) -> None:
        """Tear down: drop the server reference. The composed server holds no
        transport of its own until pumped (``run_stdio``), so there is nothing to
        disconnect here. The engine closes the native client separately."""
        self._server = None
        self._gateway = None

    # -- RequestReply capability ------------------------------------------ #

    async def reply(self, request: ServeRequest, result: Any) -> None:
        """The composed server returns the result to the caller inline (via
        ``client.invoke``), so a reply needs no separate publish. This method
        exists to ADVERTISE the RequestReply capability (the engine feature-detects
        it by presence)."""
        return


# The mcpv:kind value for each MCP surface segment.
_MCP_KIND = {"tools": "tool", "resources": "resource", "prompts": "prompt"}


def _is_destructive(action: Any) -> bool:
    """Whether an action is TD-declared destructive, matching the bridge's own
    hint derivation (``WoTAction.is_destructive``). Falls back to the
    ``tc:Destructive`` type marker if the action exposes no method."""
    is_destructive = getattr(action, "is_destructive", None)
    if callable(is_destructive):
        try:
            return bool(is_destructive())
        except Exception:  # noqa: BLE001
            pass
    raw = getattr(action, "raw", {}) or {}
    at_type = raw.get("@type")
    types = at_type if isinstance(at_type, list) else [at_type]
    return any(isinstance(t, str) and t.endswith("Destructive") for t in types)
