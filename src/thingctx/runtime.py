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
    SCALAR_INPUT_KEY,
    WoTAction,
    WoTEvent,
    WoTProperty,
    WoTThing,
    actions_to_tools,
    is_wrapped_input,
    parse_thing,
    thing_slug,
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
        validate: bool | str = False,
        approve: Approver | None = None,
        approve_when: ApprovePolicy = "declared",
        pdp: PolicyDecisionPoint | None = None,
        identity: Any = None,
        authz_raise: bool = True,
    ) -> None:
        # validate=True checks each TD against the W3C TD 1.1 schema and
        # raises TDValidationError on nonconformance (needs [validate]).
        # validate="strict" additionally runs semantic checks the schema
        # cannot (uriVariable presence, security/scope references, op legality).
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
        if validate == "strict":
            from thingctx.validate import assert_semantics

            for td in tds:
                assert_semantics(td)
        self._things: list[WoTThing] = [parse_thing(td, validate=bool(validate)) for td in tds]
        # Tool names, property maps, event maps, and per-Thing credentials are
        # all keyed by a Thing's slug. Two distinct Thing ids that collapse to
        # the same slug would silently overwrite each other (a call could reach
        # the wrong Thing), so refuse the set up front with a clear error.
        _slugs: dict[str, str] = {}
        for _t in self._things:
            _slug = thing_slug(_t.id)
            _prev = _slugs.get(_slug)
            if _prev is not None and _prev != _t.id:
                raise ValueError(
                    f"Thing ids {_prev!r} and {_t.id!r} map to the same tool namespace "
                    f"{_slug!r}; give them distinguishable ids"
                )
            _slugs[_slug] = _t.id
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
        # Strict validation also gates support: refuse a TD that needs a scheme,
        # subprotocol, or transport this client cannot drive, so a gap surfaces
        # at load rather than as a silent partial result at call time. Runs after
        # the reindex so every affordance's routability can be checked.
        if validate == "strict":
            self._assert_supported(tds)

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
        # In-flight long-running invocations, keyed by action name, so a
        # concurrent "<action>.cancel" tool can target the running job.
        self._inflight: dict[str, Any] = {}
        self._media: dict[str, WoTAction] = {}
        for name, action in list(self._route.items()):
            if any(is_media_form(f) for f in action.forms):
                self._media[name] = action
                del self._route[name]
        if self._media:
            self._tool_specs = [
                s for s in self._tool_specs if s.get("function", {}).get("name") not in self._media
            ]

    def _assert_supported(self, tds: list[dict[str, Any]]) -> None:
        from thingctx.validate import TDValidationError, validate_support

        problems: list[str] = []
        for td in tds:
            problems.extend(validate_support(td))
        # Every affordance needs at least one form a bound binding can route,
        # else the call would only fail when attempted.
        for thing in self._things:
            affordances = (
                list(thing.properties.values())
                + list(thing.actions.values())
                + list(thing.events.values())
            )
            for aff in affordances:
                forms = list(aff.forms)
                if forms and not any(self._registry.resolve(f) for f in forms):
                    schemes = sorted({f.scheme for f in forms})
                    problems.append(f"{aff.name!r}: no bound binding for transport(s) {schemes}")
        if problems:
            raise TDValidationError(problems)

    def add_things(self, tds: list[dict[str, Any]], *, validate: bool = False) -> list[str]:
        """Register TDs into a live client and return the added Thing ids.

        The runtime counterpart of the constructor's ``tds=`` for Things that
        appear after construction (a directory push, for example). Each TD is
        parsed, appended, and the client is fully reindexed (tool specs, route,
        property/event maps, media split, and the declared-security binding on
        every binding).

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
        """OpenAI-format tool specs for every exposed action. A fresh list, so a
        caller cannot mutate the client's internal tool definitions in place."""
        return list(self._tool_specs)

    def as_tools(self):
        """Return (tool_specs, invoke) to drive the Thing from your own
        agent loop. invoke is the same coroutine as self.invoke."""
        return self._tool_specs, self.invoke

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        return self._tool_specs

    def tool_surface(self) -> list[dict[str, Any]]:
        """The full set of callable tools a TD exposes, as a single list both
        the LLM host and the MCP bridge project from (so they stay in step):

        - every action (a long-running action blocks to completion), with an
          ``output_schema`` when the TD declares one;
        - an ``<action>.cancel`` tool for each long-running action;
        - a ``<property>.get`` for each readable (non-binary) property and a
          ``<property>.set`` for each writable one;
        - ``properties.read_all`` when a Thing declares a bulk-read form.

        Each entry has ``name``, ``description``, ``input_schema``,
        ``output_schema`` (or None), and ``kind``. Dispatch a name with
        :meth:`call_tool`."""
        surface: list[dict[str, Any]] = []
        for spec in self._tool_specs:
            fn = spec["function"]
            action = self._route.get(fn["name"])
            surface.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object"}),
                    "output_schema": getattr(action, "output_schema", None) if action else None,
                    "kind": "action",
                }
            )
            if action is not None and self._is_async(action):
                surface.append(
                    {
                        "name": f"{fn['name']}.cancel",
                        "description": f"Cancel an in-flight {fn['name']} (long-running action).",
                        "input_schema": {"type": "object", "properties": {}},
                        "output_schema": None,
                        "kind": "action.cancel",
                    }
                )
        for pname, prop in self._props.items():
            schema = dict(prop.schema) if getattr(prop, "schema", None) else {}
            is_binary = str(schema.get("contentMediaType", "")).startswith("image/")
            if getattr(prop, "readable", True) and not is_binary:
                surface.append(
                    {
                        "name": f"{pname}.get",
                        "description": f"Read the {pname} property.",
                        "input_schema": {"type": "object", "properties": {}},
                        "output_schema": schema or None,
                        "kind": "property.get",
                    }
                )
            if getattr(prop, "writable", False):
                surface.append(
                    {
                        "name": f"{pname}.set",
                        "description": f"Write the {pname} property.",
                        "input_schema": {
                            "type": "object",
                            "properties": {"value": schema or {"description": "the new value"}},
                            "required": ["value"],
                        },
                        "output_schema": None,
                        "kind": "property.set",
                    }
                )
        if any(self._bulk_form(t, "readallproperties") for t in self._things):
            surface.append(
                {
                    "name": "properties.read_all",
                    "description": "Read all readable properties of the Thing(s) at once.",
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": None,
                    "kind": "bulk.read",
                }
            )
        return surface

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        """Dispatch a :meth:`tool_surface` name to the right runtime call. A
        long-running action blocks to completion (its terminal status as a
        dict); an ``<action>.cancel`` stops the most recent in-flight run."""
        args = args or {}
        if name.endswith(".cancel") and (name[:-7] in self._route):
            handle = self._inflight.get(name[:-7])
            if handle is None:
                return {"error": f"no in-flight {name[:-7]} to cancel"}
            return (await self.cancel_action(handle)).as_dict()
        if name.endswith(".set") and (name[:-4] in self._props):
            return await self.write_property(name[:-4], args.get("value"))
        if name.endswith(".get") and (name[:-4] in self._props):
            return await self.read_property(name[:-4])
        if name == "properties.read_all":
            return await self.read_all_properties()
        action = self._route.get(name)
        if action is not None and self._is_async(action):
            result = await self.invoke(name, args, wait=True)
            return result.as_dict() if hasattr(result, "as_dict") else result
        return await self.invoke(name, args)

    def action_for(self, tool_name: str) -> WoTAction | None:
        return self._route.get(tool_name)

    def property_for(self, name: str) -> WoTProperty | None:
        """The WoTProperty behind a property name (its readable/writable/
        observable flags and schema), or None. For callers that project
        properties onto another surface."""
        return self._props.get(name)

    def event_for(self, name: str) -> WoTEvent | None:
        """The WoTEvent behind an event name (its data schema), or None."""
        return self._events.get(name)

    @property
    def things(self) -> list[WoTThing]:
        return self._things

    def gateway(self):
        """A constant six-verb projection over this client, for fleets too large
        for a flat one-tool-per-action surface. See :mod:`thingctx.gateway`."""
        from thingctx.gateway import GatewayProjection

        return GatewayProjection(self)

    def projection(self, mode: str = "auto", *, flat_max: int = 24):
        """Pick the projection the model should see by fleet size.

        - ``"flat"``: today's one tool per action (best for a short list).
        - ``"gateway"``: the six generic verbs (constant at any fleet size).
        - ``"auto"`` (default): flat while the flat surface stays at or under
          ``flat_max`` tools, gateway once it would exceed it.

        Returns an object exposing ``tool_specs`` and an async ``call_tool``;
        both modes share that shape, so a caller swaps modes without other
        changes. Only one mode is ever active; a duplicated surface hurts
        selection more than either mode alone.
        """
        if mode == "flat":
            return _FlatProjection(self)
        if mode == "gateway":
            return self.gateway()
        if mode == "auto":
            return _FlatProjection(self) if len(self._tool_specs) <= flat_max else self.gateway()
        raise ValueError(f"unknown projection mode {mode!r} (flat|gateway|auto)")

    def set_approval(
        self, approve: Approver | None, *, approve_when: ApprovePolicy | None = None
    ) -> None:
        """Set or replace the approval gate after construction. The MCP bridge
        uses this to bind an approver to the live server session (which does
        not exist when the client is built)."""
        self._approve = approve
        if approve_when is not None:
            self._approve_when = approve_when

    def http_binding(self):
        """The binding that drives http(s) forms. Response chaining and the
        resumable upload helper use it to send follow-up requests and to resolve
        a Thing's declared auth the same way :meth:`invoke` does. Returns None
        when no http binding is registered."""
        from thingctx.thing import WoTForm

        probe = WoTForm(href="https://thingctx.invalid/")
        return self._registry.resolve(probe)

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

    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        wait: bool = False,
        timeout: float = 30.0,
    ) -> Any:
        """Invoke one action by routing to the transport its form names.
        ``arguments`` defaults to ``{}`` (for no-input actions).

        A long-running action (``synchronous: false`` or a form declaring
        ``queryaction``) returns an :class:`~thingctx.lifecycle.ActionStatus`
        handle; poll it with :meth:`query_action` and stop it with
        :meth:`cancel_action`. Pass ``wait=True`` to block until the action
        reaches a terminal state and return the final status."""
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
        if self._is_async(action):
            return await self._invoke_async(
                action, tool_name, arguments, wait=wait, timeout=timeout
            )
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
        # A form whose response feeds a follow-up call runs through the response
        # chaining engine (resumable/presigned upload, async-job polling, ...).
        if form.raw.get("x-thingctx-next") and form.scheme in ("http", "https"):
            from thingctx.chain import run_chain

            return await run_chain(self, action, form, arguments)
        # A scalar or array action input is projected to the model wrapped
        # under a single key (thing._project_input); unwrap it here so the
        # transport receives the bare value the TD declared, not the envelope.
        if is_wrapped_input(action.input_schema) and isinstance(arguments, dict):
            arguments = arguments.get(SCALAR_INPUT_KEY)
        # Resolve uriVariables: {id} fills from args and leaves the body.
        import dataclasses

        from thingctx.reliability import TransportError

        if isinstance(arguments, dict):
            href, rest = form.fill(arguments)
            filled = dataclasses.replace(form, href=href) if href != form.href else form
        else:
            # An unwrapped scalar/array body has no uriVariables to fill.
            filled, rest = form, arguments
        try:
            return await binding.invoke(action, filled, rest)
        except TransportError as exc:
            declared = self._error_response(action, filled, getattr(exc, "status", None))
            if declared is None:
                raise
            return {"error": str(exc), "status": getattr(exc, "status", None), "response": declared}

    def _error_response(self, action: WoTAction, form, status):
        """The declared ``additionalResponses`` descriptor matching a failed
        call (by ``htv:statusCodeNumber`` when present, else the first
        error response), enriched with the referenced schemaDefinition. Returns
        None when the form declares no error response, so the error re-raises."""
        descriptors = [d for d in form.additional_responses if d.get("success") is False]
        if not descriptors:
            return None

        def _code(d):
            return d.get("htv:statusCodeNumber") or d.get("statusCodeNumber")

        match = next((d for d in descriptors if _code(d) == status), descriptors[0])
        out: dict[str, Any] = {"success": False, "contentType": match.get("contentType")}
        schema_name = match.get("schema")
        if schema_name:
            out["schema"] = schema_name
            thing = next((t for t in self._things if t.id == action.thing_id), None)
            defs = getattr(thing, "schema_definitions", None) or {}
            if schema_name in defs:
                out["schemaDefinition"] = defs[schema_name]
        return out

    @staticmethod
    def _is_async(action: WoTAction) -> bool:
        """A long-running action: declared non-synchronous, or any form carries
        the ``queryaction`` lifecycle op."""
        if getattr(action, "synchronous", None) is False:
            return True
        return any("queryaction" in f.op for f in action.forms)

    def _lifecycle_form(self, action: WoTAction):
        """Pick the action form that drives the async lifecycle (declares
        ``queryaction``), honoring transport preference; else any form."""
        cand = [f for f in action.forms if "queryaction" in f.op] or list(action.forms)
        for scheme in self._prefer:
            for f in cand:
                if f.scheme == scheme:
                    return f
        return cand[0] if cand else None

    async def _invoke_async(self, action, tool_name, arguments, *, wait, timeout):
        form = self._lifecycle_form(action)
        binding = self._registry.resolve(form) if form else None
        if binding is None or not hasattr(binding, "invoke_async"):
            # No lifecycle-capable transport: fall back to a plain invoke so the
            # call still completes (returns the raw result, not a handle).
            if binding is not None and hasattr(binding, "invoke") and form is not None:
                import dataclasses

                href, rest = form.fill(arguments or {})
                filled = dataclasses.replace(form, href=href) if href != form.href else form
                return await binding.invoke(action, filled, rest)
            return {"error": f"action {tool_name} has no lifecycle transport"}
        import dataclasses

        href, rest = form.fill(arguments or {})
        filled = dataclasses.replace(form, href=href) if href != form.href else form
        status = await binding.invoke_async(action, filled, rest)
        if not wait:
            return status
        # Register the handle while blocking so a concurrent cancel can find it.
        self._inflight[tool_name] = status
        try:
            return await self._wait_for(status, timeout=timeout)
        finally:
            self._inflight.pop(tool_name, None)

    async def _wait_for(self, status, *, timeout: float):
        import asyncio
        import time

        deadline = time.monotonic() + timeout
        while not status.terminal and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            status = await self.query_action(status)
        return status

    async def query_action(self, status):
        """Poll a long-running action's status (the ``queryaction`` op)."""
        binding = self._registry.resolve(status.form) if status.form else None
        if binding is None or not hasattr(binding, "query_action"):
            return status
        return await binding.query_action(status)

    async def cancel_action(self, status):
        """Cancel a long-running action (the ``cancelaction`` op)."""
        binding = self._registry.resolve(status.form) if status.form else None
        if binding is None or not hasattr(binding, "cancel_action"):
            return status
        return await binding.cancel_action(status)

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

    def _bulk_form(self, thing: WoTThing, *ops: str):
        """The Thing-level form declaring any of ``ops`` (a bulk operation), or None."""
        for f in thing.forms:
            if any(op in f.op for op in ops):
                return f
        return None

    async def read_all_properties(self) -> dict[str, Any]:
        """Read every readable property. Uses a Thing's ``readallproperties``
        bulk form when declared, else reads each property individually. Returns
        a ``{property: value}`` map."""
        out: dict[str, Any] = {}
        for thing in self._things:
            form = self._bulk_form(thing, "readallproperties")
            binding = self._registry.resolve(form) if form else None
            # The bulk fast path reaches the device in one request and cannot
            # authorize per property, so it is used only when authorization is
            # off. With a PDP set, every read goes through the per-property
            # authorized path below; a bulk read is not an authorization bypass.
            if (
                self._pdp is None
                and form is not None
                and binding is not None
                and hasattr(binding, "read_all")
            ):
                res = await binding.read_all(thing, form)
                if isinstance(res, dict):
                    out.update(res)
                    continue
            for name, prop in self._props.items():
                if prop.thing_id == thing.id and prop.readable:
                    out[prop.name] = await self.read_property(name)
        return out

    async def read_properties(self, names: list[str]) -> dict[str, Any]:
        """Read a named subset of properties. Uses a Thing's
        ``readmultipleproperties`` bulk form when declared, else reads each."""
        out: dict[str, Any] = {}
        wanted = set(names)
        for thing in self._things:
            mine = [p.name for p in thing.properties.values() if p.name in wanted]
            if not mine:
                continue
            form = self._bulk_form(thing, "readmultipleproperties", "readallproperties")
            binding = self._registry.resolve(form) if form else None
            # Bulk fast path only when authorization is off (see read_all_properties).
            if (
                self._pdp is None
                and form is not None
                and binding is not None
                and hasattr(binding, "read_all")
            ):
                res = await binding.read_all(thing, form, names=mine)
                if isinstance(res, dict):
                    out.update(res)
                    continue
            for name, prop in self._props.items():
                if prop.thing_id == thing.id and prop.name in wanted:
                    out[prop.name] = await self.read_property(name)
        return out

    async def write_properties(self, values: dict[str, Any]) -> dict[str, Any]:
        """Write several properties at once. Uses a Thing's
        ``writeallproperties`` / ``writemultipleproperties`` bulk form when
        declared, else writes each (which rejects read-only properties)."""
        out: dict[str, Any] = {}
        for thing in self._things:
            mine = {k: v for k, v in values.items() if k in thing.properties}
            if not mine:
                continue
            form = self._bulk_form(thing, "writeallproperties", "writemultipleproperties")
            binding = self._registry.resolve(form) if form else None
            # Bulk fast path only when authorization is off (see read_all_properties).
            if (
                self._pdp is None
                and form is not None
                and binding is not None
                and hasattr(binding, "write_all")
            ):
                res = await binding.write_all(thing, form, mine)
                out.update(res if isinstance(res, dict) else {"result": res})
                continue
            for name, prop in self._props.items():
                if prop.thing_id == thing.id and prop.name in mine:
                    out[prop.name] = await self.write_property(name, mine[prop.name])
        return out

    async def subscribe(self, name: str, args: dict[str, Any] | None = None):
        """Subscribe to an event or observable property. Returns an async
        iterator that yields each pushed value. ``args`` are subscribe-time
        parameters (an event's ``subscription`` schema), e.g. a filter.

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
        stream = await binding.subscribe(target, form, args or {})
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
        audio=None,
    ) -> None:
        """Push an async iterator of frames to a media affordance's ingest
        target (a URL or a file). The outbound mirror of ``frames()``; returns
        when the source is exhausted. With ``audio`` supplied, ``frames`` is the
        video track and ``audio`` is muxed alongside it into one A/V output.

            await client.publish("studio.broadcast", frame_source())
            await client.publish(
                "studio.broadcast", video, audio=await client.frames("cam.watch", track="audio")
            )
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
        await binding.publish(action, filled, frames, rest, track=track, audio=audio)

    async def save(
        self,
        name: str,
        target: str,
        arguments: dict[str, Any] | None = None,
        *,
        track: str | None = None,
    ) -> None:
        """Remux a media affordance's source to ``target`` (a local file) by
        stream copy: the file is bit exact (same codecs, frame rate, A/V sync)
        with no re-encode. The clean "save the source"; ``publish`` re-encodes
        for a transform. ``track`` (``video``/``audio``) limits the copy to one
        stream; by default every media stream is copied.

            await client.save("cam.watch", "clip.mp4")
        """
        action = self._media.get(name)
        if action is None:
            raise KeyError(f"unknown media affordance: {name}")
        form = next((f for f in action.forms if is_media_form(f)), None)
        binding = self._registry.resolve(form) if form else None
        if binding is None or not hasattr(binding, "save"):
            raise RuntimeError(f"no media transport for {name}; register MediaBinding")
        import dataclasses

        href, rest = form.fill(arguments or {})
        filled = dataclasses.replace(form, href=href) if href != form.href else form
        await binding.save(action, filled, target, rest, track=track)

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


class _FlatProjection:
    """The flat surface (one tool per action) behind the same
    ``tool_specs`` / ``call_tool`` shape the gateway offers, so
    :meth:`ThingClient.projection` returns one interface for either mode."""

    def __init__(self, client: ThingClient) -> None:
        self._client = client

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        return self._client.tool_specs

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        return await self._client.call_tool(name, args)
