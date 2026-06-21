"""Parse a WoT Thing Description (JSON) into actions, properties, events,
and their transport bindings. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass
class WoTForm:
    """A form: the transport binding (href) for an interaction."""

    href: str
    op: tuple[str, ...] = ()
    content_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def scheme(self) -> str:
        """URI scheme of the href (http, mqtt, ...); local if none."""
        s = urlparse(self.href).scheme
        return s or "local"

    def fill(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Substitute {var} placeholders in the href from args, and return
        (href, remaining_args) with the consumed vars removed.
        """
        import re as _re

        used: set[str] = set()

        def _sub(m):
            key = m.group(1)
            if key in args:
                used.add(key)
                from urllib.parse import quote

                return quote(str(args[key]), safe="")
            return m.group(0)

        href = _re.sub(r"\{([^}]+)\}", _sub, self.href)
        rest = {k: v for k, v in args.items() if k not in used}
        return href, rest


@dataclass
class WoTAction:
    """A callable action on a Thing."""

    name: str
    thing_id: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    idempotent: bool
    forms: tuple[WoTForm, ...]
    # JSON-LD @type annotations (e.g. tc:PromptTemplate). raw keeps the
    # source dict so extensions can read their own fields.
    at_type: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def has_type(self, term: str) -> bool:
        """True if annotated @type term (exact, or local name after a
        prefix:)."""
        for t in self.at_type:
            if t == term or t.split(":")[-1] == term.split(":")[-1]:
                return True
        return False

    def _flag(self, *keys: str) -> bool:
        """True if any of the given keys is truthy in the action's raw def
        (e.g. tc:requiresApproval). Checks the bare key too."""
        for k in keys:
            v = self.raw.get(k)
            if v is None and ":" in k:
                v = self.raw.get(k.split(":")[-1])
            if v:
                return True
        return False

    def requires_approval(self) -> bool:
        """True if the TD gates this action behind human approval
        (tc:requiresApproval, or @type tc:Destructive)."""
        return self._flag("tc:requiresApproval") or self.has_type("tc:Destructive")

    def is_destructive(self) -> bool:
        """True if the action changes/affects the device irreversibly
        (tc:requiresApproval, @type tc:Destructive, or a non-idempotent
        non-safe action). Used for MCP destructiveHint."""
        return self.requires_approval() or not self.idempotent

    def primary_form(self, *, prefer: tuple[str, ...] = ()) -> WoTForm | None:
        """Pick a form by preferred transport scheme order; else the
        first."""
        for scheme in prefer:
            for f in self.forms:
                if f.scheme == scheme:
                    return f
        return self.forms[0] if self.forms else None


@dataclass
class WoTProperty:
    """A property: typed Thing state. readable, writable, and/or
    observable per its ops."""

    name: str
    thing_id: str
    description: str
    schema: dict[str, Any]
    readable: bool
    writable: bool
    observable: bool
    forms: tuple[WoTForm, ...]

    def primary_form(self, *, prefer: tuple[str, ...] = ()) -> WoTForm | None:
        for scheme in prefer:
            for f in self.forms:
                if f.scheme == scheme:
                    return f
        return self.forms[0] if self.forms else None


@dataclass
class WoTEvent:
    """An event the Thing emits. Subscribe to receive pushed payloads."""

    name: str
    thing_id: str
    description: str
    data_schema: dict[str, Any] | None
    forms: tuple[WoTForm, ...]

    def primary_form(self, *, prefer: tuple[str, ...] = ()) -> WoTForm | None:
        for scheme in prefer:
            for f in self.forms:
                if f.scheme == scheme:
                    return f
        return self.forms[0] if self.forms else None


@dataclass
class WoTSecurityScheme:
    """A declared auth scheme. The secret is supplied at runtime, not in
    the TD."""

    name: str
    scheme: str  # bearer, basic, apikey, oauth2, nosec
    in_: str = "header"  # apikey location: header or query
    key_name: str = "Authorization"  # header/query name for apikey
    # oauth2 (the TD declares the endpoints; the client supplies client creds)
    flow: str = ""  # client_credentials, password, code, ...
    token: str = ""  # token endpoint URL
    authorization: str = ""  # authorization endpoint URL
    refresh: str = ""  # refresh endpoint URL
    scopes: tuple[str, ...] = ()
    # The full security-definition dict, verbatim. Carries vendor/extension
    # fields (e.g. an AWS region/service, a custom scheme's settings) so that
    # custom auth strategies can read whatever the TD declared.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WoTThing:
    """A parsed Thing Description."""

    id: str
    title: str
    description: str
    actions: dict[str, WoTAction]
    properties: dict[str, WoTProperty] = field(default_factory=dict)
    events: dict[str, WoTEvent] = field(default_factory=dict)
    security: tuple[str, ...] = ()  # active scheme names
    security_schemes: dict[str, WoTSecurityScheme] = field(default_factory=dict)
    base: str | None = None
    uri_variables: dict[str, Any] = field(default_factory=dict)


def parse_thing(td: dict[str, Any], *, validate: bool = False) -> WoTThing:
    """Parse a TD dict into a WoTThing. Lenient: missing pieces degrade.

    validate=True checks the TD against the W3C TD 1.1 schema first and
    raises TDValidationError (needs the [validate] extra).
    """
    if validate:
        from thingctx.validate import assert_valid_td

        assert_valid_td(td)
    thing_id = td.get("id") or td.get("@id") or td.get("title", "thing")
    title = td.get("title", thing_id)
    base = td.get("base")  # relative form hrefs resolve against this
    thing_uri_vars = td.get("uriVariables") or {}
    actions: dict[str, WoTAction] = {}
    for name, adef in (td.get("actions") or {}).items():
        adef = adef or {}
        forms = _parse_forms(adef, base=base)
        actions[name] = WoTAction(
            name=name,
            thing_id=thing_id,
            description=(adef.get("description") or adef.get("title") or name),
            input_schema=adef.get("input") or {"type": "object"},
            output_schema=adef.get("output"),
            idempotent=bool(adef.get("idempotent") or adef.get("safe")),
            forms=forms,
            at_type=_as_tuple(adef.get("@type")),
            raw=adef,
        )

    properties: dict[str, WoTProperty] = {}
    for name, pdef in (td.get("properties") or {}).items():
        pdef = pdef or {}
        ops = _all_ops(pdef)
        properties[name] = WoTProperty(
            name=name,
            thing_id=thing_id,
            description=pdef.get("description") or pdef.get("title") or name,
            schema=_value_schema(pdef),
            readable=not bool(pdef.get("writeOnly")),
            writable=not bool(pdef.get("readOnly")),
            observable=bool(pdef.get("observable")) or "observeproperty" in ops,
            forms=_parse_forms(pdef, base=base),
        )

    events: dict[str, WoTEvent] = {}
    for name, edef in (td.get("events") or {}).items():
        edef = edef or {}
        events[name] = WoTEvent(
            name=name,
            thing_id=thing_id,
            description=edef.get("description") or edef.get("title") or name,
            data_schema=edef.get("data"),
            forms=_parse_forms(edef, base=base),
        )

    schemes: dict[str, WoTSecurityScheme] = {}
    for sname, sdef in (td.get("securityDefinitions") or {}).items():
        sdef = sdef or {}
        schemes[sname] = WoTSecurityScheme(
            name=sname,
            scheme=sdef.get("scheme", "nosec"),
            in_=sdef.get("in", "header"),
            key_name=sdef.get("name", "Authorization"),
            flow=sdef.get("flow", ""),
            token=sdef.get("token", ""),
            authorization=sdef.get("authorization", ""),
            refresh=sdef.get("refresh", ""),
            scopes=tuple(sdef.get("scopes") or ()),
            raw=dict(sdef),
        )
    sec = td.get("security")
    security = tuple(sec) if isinstance(sec, list) else ((sec,) if sec else ())

    return WoTThing(
        id=thing_id,
        title=title,
        description=td.get("description", ""),
        actions=actions,
        properties=properties,
        events=events,
        security=security,
        security_schemes=schemes,
        base=base,
        uri_variables=thing_uri_vars,
    )


def _parse_forms(
    defn: dict[str, Any],
    *,
    base: str | None = None,
) -> tuple[WoTForm, ...]:
    return tuple(
        WoTForm(
            href=_resolve_href(f.get("href", ""), base),
            op=tuple(
                f["op"] if isinstance(f.get("op"), list) else ([f["op"]] if f.get("op") else [])
            ),
            content_type=f.get("contentType"),
            raw=f,
        )
        for f in (defn.get("forms") or [])
    )


def _as_tuple(v: Any) -> tuple[str, ...]:
    """Normalize @type (str or list) to a tuple."""
    if isinstance(v, list):
        return tuple(str(x) for x in v)
    return (str(v),) if v else ()


def _resolve_href(href: str, base: str | None) -> str:
    """Resolve a relative href against base; absolute hrefs pass through."""
    if not href or not base:
        return href
    if urlparse(href).scheme:
        return href
    from urllib.parse import urljoin

    return urljoin(base if base.endswith("/") else base + "/", href.lstrip("/"))


def _all_ops(defn: dict[str, Any]) -> set[str]:
    ops: set[str] = set()
    for f in defn.get("forms") or []:
        op = f.get("op")
        if isinstance(op, list):
            ops.update(op)
        elif op:
            ops.add(op)
    return ops


def _value_schema(pdef: dict[str, Any]) -> dict[str, Any]:
    # The property def minus housekeeping keys is its value schema.
    drop = {"forms", "observable", "writeOnly", "readOnly", "title", "description", "@type", "op"}
    schema = {k: v for k, v in pdef.items() if k not in drop}
    return schema or {"type": "string"}


def _tool_name(thing_id: str, action_name: str) -> str:
    """Short tool name: urn:demo:pump:v1 + set_speed -> pump.set_speed."""
    parts = [p for p in str(thing_id).split(":") if p]
    if len(parts) >= 2 and parts[-1].lower().lstrip("v").isdigit():
        parts = parts[:-1]
    slug = parts[-1] if parts else str(thing_id)
    slug = "".join(c if (c.isalnum() or c in "._-") else "-" for c in slug)
    return f"{slug}.{action_name}"


def actions_to_tools(
    things: list[WoTThing],
    *,
    only_idempotent: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, WoTAction]]:
    """Project actions to OpenAI tool specs and a name->action map.

    Returns (tool_specs, route): tool_specs for the model, route[name]
    the WoTAction to invoke when the model calls name.
    """
    import json as _json

    specs: list[dict[str, Any]] = []
    route: dict[str, WoTAction] = {}
    for thing in things:
        for action in thing.actions.values():
            if only_idempotent and not action.idempotent:
                continue
            name = _tool_name(thing.id, action.name)
            desc = action.description
            # OpenAI's function format has no output field; fold the
            # output schema into the description.
            if action.output_schema:
                desc = f"{desc}\nReturns: {_json.dumps(action.output_schema)}"
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": action.input_schema or {"type": "object"},
                    },
                }
            )
            route[name] = action
    return specs, route
