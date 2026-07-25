# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Parse a WoT Thing Description (JSON) into actions, properties, events,
and their transport bindings. Stdlib only.

Models the W3C WoT Thing Description 1.1 information model
(1.1-12-March-2025): interaction affordances and their forms, the full
form vocabulary (op, subprotocol, content coding, expected responses,
form-level security), per-affordance ``uriVariables``, the security
scheme variants, the Thing-level bulk-operation forms, and metadata
(version, links, lifecycle dates, ``@context``). Unmodeled vendor keys
are kept verbatim on each ``raw`` for extensions to read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


def _text(defn: dict[str, Any], multi_key: str, single_key: str, fallback: str = "") -> str:
    """Resolve a human-readable string, preferring the single-language key
    (``title``/``description``) then a multi-language map (``titles``/
    ``descriptions``), favoring English, then any entry, else ``fallback``."""
    single = defn.get(single_key)
    if single:
        return str(single)
    multi = defn.get(multi_key)
    if isinstance(multi, dict) and multi:
        for lang in ("en", "en-US"):
            if lang in multi:
                return str(multi[lang])
        return str(next(iter(multi.values())))
    return fallback


def _lang_map(defn: dict[str, Any], multi_key: str) -> dict[str, str]:
    """The multi-language map (``titles``/``descriptions``) as a dict."""
    multi = defn.get(multi_key)
    return {str(k): str(v) for k, v in multi.items()} if isinstance(multi, dict) else {}


@dataclass
class WoTLink:
    """A hypermedia link on a Thing (``links``)."""

    href: str
    rel: str | None = None
    type: str | None = None
    anchor: str | None = None
    sizes: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WoTVersion:
    """Version information (``version``): the TD instance and its model."""

    instance: str | None = None
    model: str | None = None


@dataclass
class WoTForm:
    """A form: the transport binding (href) for an interaction, plus the
    operations it serves and how its payloads are encoded and protected."""

    href: str
    op: tuple[str, ...] = ()
    content_type: str | None = None
    subprotocol: str | None = None  # e.g. longpoll, sse, websub
    content_coding: str | None = None  # e.g. gzip, deflate
    response_type: str | None = None  # expected response contentType
    additional_responses: tuple[dict[str, Any], ...] = ()  # error/extra responses
    security: tuple[str, ...] = ()  # form-level security scheme names (override)
    scopes: tuple[str, ...] = ()  # form-level oauth2 scopes
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def scheme(self) -> str:
        """URI scheme of the href (http, mqtt, ...); local if none."""
        s = urlparse(self.href).scheme
        return s or "local"

    def fill(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Substitute {var} placeholders in the href from args, and return
        (href, remaining_args) with the consumed vars removed.

        ``{var}`` percent encodes the value (safe for a path or query segment).
        ``{+var}`` (RFC 6570 reserved expansion) substitutes it verbatim, for
        when the variable *is* a URL; for example a media href that takes any
        source URL as an argument (``"href": "{+url}"``).
        """
        import re as _re

        used: set[str] = set()

        def _sub(m):
            key = m.group(1)
            raw = key.startswith("+")
            if raw:
                key = key[1:]
            if key in args:
                used.add(key)
                from urllib.parse import quote

                return str(args[key]) if raw else quote(str(args[key]), safe="")
            return m.group(0)

        href = _re.sub(r"\{(\+?[^}]+)\}", _sub, self.href)
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
    safe: bool = False  # no state change (TD ``safe``)
    synchronous: bool | None = None  # None = unspecified
    uri_variables: dict[str, Any] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
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

    def _ops(self) -> set[str]:
        ops: set[str] = set()
        for f in self.forms:
            ops.update(f.op)
        return ops

    @property
    def read_only(self) -> bool:
        """Safe to issue without side effects (TD ``safe`` or ``idempotent``).
        Drives GET selection and read-only hints."""
        return self.safe or self.idempotent

    def supports_query(self) -> bool:
        """The TD exposes status query for this (long-running) action
        (``queryaction``)."""
        return "queryaction" in self._ops()

    def supports_cancel(self) -> bool:
        """The TD exposes cancellation for this action (``cancelaction``)."""
        return "cancelaction" in self._ops()

    def requires_approval(self) -> bool:
        """True if the TD gates this action behind human approval
        (tc:requiresApproval, or @type tc:Destructive)."""
        return self._flag("tc:requiresApproval") or self.has_type("tc:Destructive")

    def is_destructive(self) -> bool:
        """True if the action changes/affects the device irreversibly
        (tc:requiresApproval, @type tc:Destructive, or a non-idempotent
        non-safe action). Used for MCP destructiveHint."""
        return self.requires_approval() or not self.read_only

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
    uri_variables: dict[str, Any] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    at_type: tuple[str, ...] = ()

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
    subscription_schema: dict[str, Any] | None = None  # subscribe-time params
    data_response_schema: dict[str, Any] | None = None  # per-notification response
    cancellation_schema: dict[str, Any] | None = None  # unsubscribe-time params
    uri_variables: dict[str, Any] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    at_type: tuple[str, ...] = ()

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
    scheme: str  # nosec, basic, digest, apikey, bearer, psk, oauth2, auto, combo
    in_: str = "header"  # credential location: header, query, body, cookie, uri, auto
    key_name: str = "Authorization"  # header/query/cookie name (basic/digest/apikey/bearer)
    # bearer / digest specifics
    alg: str = ""  # bearer signing alg (e.g. ES256)
    format_: str = ""  # bearer token format (e.g. jwt, jwk)
    qop: str = ""  # digest quality of protection (auth, auth-int)
    # psk
    identity: str = ""
    # oauth2 (the TD declares the endpoints; the client supplies client creds)
    flow: str = ""  # client_credentials, password, code, ...
    token: str = ""  # token endpoint URL
    authorization: str = ""  # authorization endpoint URL
    refresh: str = ""  # refresh endpoint URL
    scopes: tuple[str, ...] = ()
    proxy: str = ""  # URI of a proxy that secures the resource
    # combo: a composite of other named schemes
    combo_kind: str = ""  # "oneOf" or "allOf"
    combo_of: tuple[str, ...] = ()  # referenced scheme names
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
    # Thing-level forms: bulk operations over the whole Thing
    # (readallproperties, writemultipleproperties, subscribeallevents, ...).
    forms: tuple[WoTForm, ...] = ()
    # Metadata
    at_type: tuple[str, ...] = ()
    context: Any = None  # raw @context (str, or list of str/dict)
    context_prefixes: dict[str, str] = field(default_factory=dict)
    version: WoTVersion | None = None
    created: str | None = None
    modified: str | None = None
    support: str | None = None
    links: tuple[WoTLink, ...] = ()
    profile: tuple[str, ...] = ()
    schema_definitions: dict[str, Any] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)

    def bulk_ops(self) -> set[str]:
        """The Thing-level operations declared by its top-level forms."""
        ops: set[str] = set()
        for f in self.forms:
            ops.update(f.op)
        return ops


def parse_thing(td: dict[str, Any], *, validate: bool = False) -> WoTThing:
    """Parse a TD dict into a WoTThing. Lenient: missing pieces degrade.

    validate=True checks the TD against the W3C TD 1.1 schema first and
    raises TDValidationError (needs the [validate] extra).
    """
    if validate:
        from thingctx.validate import assert_valid_td

        assert_valid_td(td)
    thing_id = td.get("id") or td.get("@id") or td.get("title", "thing")
    title = _text(td, "titles", "title", fallback=str(thing_id))
    base = td.get("base")  # relative form hrefs resolve against this
    thing_uri_vars = td.get("uriVariables") or {}
    actions: dict[str, WoTAction] = {}
    for name, adef in (td.get("actions") or {}).items():
        adef = adef or {}
        forms = _parse_forms(adef, base=base)
        actions[name] = WoTAction(
            name=name,
            thing_id=thing_id,
            description=_text(adef, "descriptions", "description", fallback=name),
            input_schema=adef.get("input") or {"type": "object"},
            output_schema=adef.get("output"),
            idempotent=bool(adef.get("idempotent")),
            forms=forms,
            safe=bool(adef.get("safe")),
            synchronous=adef.get("synchronous"),
            uri_variables=dict(adef.get("uriVariables") or {}),
            titles=_lang_map(adef, "titles"),
            descriptions=_lang_map(adef, "descriptions"),
            at_type=_as_tuple(adef.get("@type")),
            raw=adef,
        )

    properties: dict[str, WoTProperty] = {}
    for name, pdef in (td.get("properties") or {}).items():
        pdef = pdef or {}
        ops = _all_ops(pdef)
        # TD flags are the baseline; when forms declare an explicit op list that
        # omits writeproperty, the write surface is closed even without readOnly
        # (dogfood: MQTT observe/read forms were projecting a spurious .set).
        writable = not bool(pdef.get("readOnly"))
        if ops and "writeproperty" not in ops:
            writable = False
        readable = not bool(pdef.get("writeOnly"))
        if ops and "readproperty" not in ops and "observeproperty" not in ops:
            readable = False
        properties[name] = WoTProperty(
            name=name,
            thing_id=thing_id,
            description=_text(pdef, "descriptions", "description", fallback=name),
            schema=_value_schema(pdef),
            readable=readable,
            writable=writable,
            observable=bool(pdef.get("observable")) or "observeproperty" in ops,
            forms=_parse_forms(pdef, base=base),
            uri_variables=dict(pdef.get("uriVariables") or {}),
            titles=_lang_map(pdef, "titles"),
            descriptions=_lang_map(pdef, "descriptions"),
            at_type=_as_tuple(pdef.get("@type")),
        )

    events: dict[str, WoTEvent] = {}
    for name, edef in (td.get("events") or {}).items():
        edef = edef or {}
        events[name] = WoTEvent(
            name=name,
            thing_id=thing_id,
            description=_text(edef, "descriptions", "description", fallback=name),
            data_schema=edef.get("data"),
            forms=_parse_forms(edef, base=base),
            subscription_schema=edef.get("subscription"),
            data_response_schema=edef.get("dataResponse"),
            cancellation_schema=edef.get("cancellation"),
            uri_variables=dict(edef.get("uriVariables") or {}),
            titles=_lang_map(edef, "titles"),
            descriptions=_lang_map(edef, "descriptions"),
            at_type=_as_tuple(edef.get("@type")),
        )

    schemes: dict[str, WoTSecurityScheme] = {}
    for sname, sdef in (td.get("securityDefinitions") or {}).items():
        schemes[sname] = _parse_security_scheme(sname, sdef or {})
    sec = td.get("security")
    security = tuple(sec) if isinstance(sec, list) else ((sec,) if sec else ())

    version = None
    vraw = td.get("version")
    if isinstance(vraw, dict):
        version = WoTVersion(instance=vraw.get("instance"), model=vraw.get("model"))

    return WoTThing(
        id=thing_id,
        title=title,
        description=_text(td, "descriptions", "description"),
        actions=actions,
        properties=properties,
        events=events,
        security=security,
        security_schemes=schemes,
        base=base,
        uri_variables=thing_uri_vars,
        forms=_parse_forms(td, base=base),
        at_type=_as_tuple(td.get("@type")),
        context=td.get("@context"),
        context_prefixes=_context_prefixes(td.get("@context")),
        version=version,
        created=td.get("created"),
        modified=td.get("modified"),
        support=td.get("support"),
        links=_parse_links(td, base=base),
        profile=_as_tuple(td.get("profile")),
        schema_definitions=dict(td.get("schemaDefinitions") or {}),
        titles=_lang_map(td, "titles"),
        descriptions=_lang_map(td, "descriptions"),
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
            subprotocol=f.get("subprotocol"),
            content_coding=f.get("contentCoding"),
            response_type=(f.get("response") or {}).get("contentType")
            if isinstance(f.get("response"), dict)
            else None,
            additional_responses=tuple(_as_list(f.get("additionalResponses"))),
            security=_as_tuple(f.get("security")),
            scopes=_as_tuple(f.get("scopes")),
            raw=f,
        )
        for f in (defn.get("forms") or [])
    )


def _parse_links(defn: dict[str, Any], *, base: str | None = None) -> tuple[WoTLink, ...]:
    return tuple(
        WoTLink(
            href=_resolve_href(link.get("href", ""), base),
            rel=link.get("rel"),
            type=link.get("type"),
            anchor=link.get("anchor"),
            sizes=link.get("sizes"),
            raw=link,
        )
        for link in (defn.get("links") or [])
        if isinstance(link, dict)
    )


def _parse_security_scheme(name: str, sdef: dict[str, Any]) -> WoTSecurityScheme:
    combo_kind = ""
    combo_of: tuple[str, ...] = ()
    if "oneOf" in sdef:
        combo_kind, combo_of = "oneOf", _as_tuple(sdef.get("oneOf"))
    elif "allOf" in sdef:
        combo_kind, combo_of = "allOf", _as_tuple(sdef.get("allOf"))
    return WoTSecurityScheme(
        name=name,
        scheme=sdef.get("scheme", "nosec"),
        in_=sdef.get("in", "header"),
        key_name=sdef.get("name", "Authorization"),
        alg=sdef.get("alg", ""),
        format_=sdef.get("format", ""),
        qop=sdef.get("qop", ""),
        identity=sdef.get("identity", ""),
        flow=sdef.get("flow", ""),
        token=sdef.get("token", ""),
        authorization=sdef.get("authorization", ""),
        refresh=sdef.get("refresh", ""),
        scopes=_as_tuple(sdef.get("scopes")),
        proxy=sdef.get("proxy", ""),
        combo_kind=combo_kind,
        combo_of=combo_of,
        raw=dict(sdef),
    )


def _context_prefixes(context: Any) -> dict[str, str]:
    """Collect prefix -> IRI mappings declared in ``@context`` (the object
    entries; bare string contexts contribute none)."""
    prefixes: dict[str, str] = {}
    entries = context if isinstance(context, list) else [context]
    for entry in entries:
        if isinstance(entry, dict):
            for k, v in entry.items():
                if isinstance(v, str) and not k.startswith("@"):
                    prefixes[k] = v
    return prefixes


def _as_tuple(v: Any) -> tuple[str, ...]:
    """Normalize a string-or-list term to a tuple of strings."""
    if isinstance(v, list):
        return tuple(str(x) for x in v)
    return (str(v),) if v else ()


def _as_list(v: Any) -> list[Any]:
    """Normalize a value-or-list to a list (empty if absent)."""
    if isinstance(v, list):
        return v
    return [v] if v else []


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
    drop = {
        "forms",
        "observable",
        "writeOnly",
        "readOnly",
        "title",
        "titles",
        "description",
        "descriptions",
        "@type",
        "op",
        "uriVariables",
    }
    schema = {k: v for k, v in pdef.items() if k not in drop}
    return schema or {"type": "string"}


# WoT allows an action ``input`` to be any JSON Schema, including a scalar
# (``{"type": "number"}``). The OpenAI function-calling format requires the
# tool ``parameters`` to be an object schema, so a scalar input projects to a
# tool the provider cannot call. Wrap a non-object input under this single key
# for the model; the runtime unwraps it back to the bare value before invoke.
SCALAR_INPUT_KEY = "value"


def _project_input(input_schema: dict[str, Any]) -> dict[str, Any]:
    """The OpenAI-format ``parameters`` for an action's input schema. An object
    schema passes through; a scalar or array schema is wrapped under
    ``SCALAR_INPUT_KEY`` so the tool is callable."""
    schema = input_schema or {"type": "object"}
    if schema.get("type") == "object" or "properties" in schema:
        return schema
    return {
        "type": "object",
        "properties": {SCALAR_INPUT_KEY: schema},
        "required": [SCALAR_INPUT_KEY],
    }


def _href_var_names(href: str) -> set[str]:
    """The uriVariable names an href references, e.g. {+broker}/{+topic}."""
    import re as _re

    return {m.lstrip("+") for m in _re.findall(r"\{(\+?[^}]+)\}", href)}


def _project_action_params(action: WoTAction, thing_uri_vars: dict[str, Any]) -> dict[str, Any]:
    """The tool ``parameters`` for an action, INCLUDING the uriVariables its form
    needs. The bare input schema omits them (they live in ``uriVariables``, not
    ``input``), so a model reading only the schema cannot supply the broker/topic
    a form like ``mqtt://{+broker}/{+topic}`` requires. Fold in the action-level
    uriVariables plus any Thing-level ones the href references, as required
    string properties, so the flat tool advertises exactly what the call needs.

    Uses the '{+broker}' expansion form's naming; a scalar/array input still wraps
    under SCALAR_INPUT_KEY, and its uriVariables sit alongside that key."""
    base = _project_input(action.input_schema)
    # Which uriVariables does this action's form(s) actually reference?
    referenced: set[str] = set()
    for form in action.forms:
        referenced |= _href_var_names(form.href)
    if not referenced:
        return base
    # Merge action-level uriVariables and referenced Thing-level ones.
    var_defs = dict(action.uri_variables or {})
    for name in referenced:
        if name not in var_defs and name in (thing_uri_vars or {}):
            var_defs[name] = thing_uri_vars[name]
    var_defs = {n: d for n, d in var_defs.items() if n in referenced}
    if not var_defs:
        return base
    out = dict(base)
    props = dict(out.get("properties") or {})
    for name, vdef in var_defs.items():
        if name not in props:  # never shadow a real input property
            props[name] = vdef if isinstance(vdef, dict) else {"type": "string"}
    out["properties"] = props
    # A uriVariable the href needs is required to make the call.
    required = list(out.get("required") or [])
    for name in var_defs:
        if name not in required:
            required.append(name)
    out["required"] = required
    return out


def is_wrapped_input(input_schema: dict[str, Any]) -> bool:
    """True when :func:`_project_input` wrapped this schema, so the runtime
    must unwrap the model's ``{"value": x}`` back to ``x`` before invoke."""
    schema = input_schema or {"type": "object"}
    return not (schema.get("type") == "object" or "properties" in schema)


# The tool-name namespace separator. A double underscore, NOT a dot.
#
# The tool name namespaces the action under its Thing (``<slug><SEP><action>``) so
# actions of the same name on different Things do not collide. The separator must be
# in the charset EVERY agent runtime accepts. That intersection is ``[A-Za-z0-9_-]``:
# a single dot passes the OpenAI/Anthropic function-name charset but is REJECTED by
# strict MCP clients (Claude Desktop enforces ``^[a-zA-Z0-9_-]{1,64}$``; a ``.`` fails
# it, as do ``:`` and ``/``). ``__`` is legal in all of them. A single ``_`` is left
# free for use INSIDE a slug or action name, so ``__`` reads unambiguously as the
# namespace boundary. Recovery of the (Thing, action) split is by a stored map at the
# call site, never by re-splitting the string (an action name may contain ``_``).
TOOL_SEP = "__"


def thing_slug(thing_id: str) -> str:
    """Short slug for a Thing id: urn:demo:pump:v1 -> pump. A trailing
    version segment (v1, 2, ...) is dropped; the last remaining segment is
    kept and reduced to the universal tool-name charset (letters, digits, ``-``).
    A ``.`` is NOT kept: it is illegal in strict MCP tool names."""
    parts = [p for p in str(thing_id).split(":") if p]
    if len(parts) >= 2 and parts[-1].lower().lstrip("v").isdigit():
        parts = parts[:-1]
    slug = parts[-1] if parts else str(thing_id)
    return "".join(c if (c.isalnum() or c in "_-") else "-" for c in slug)


def _tool_name(thing_id: str, action_name: str) -> str:
    """Short tool name: urn:demo:pump:v1 + set_speed -> pump__set_speed."""
    return f"{thing_slug(thing_id)}{TOOL_SEP}{action_name}"


def _tool_slug(tool_name: str) -> str:
    """Recover the Thing slug from a tool name: pump__set_speed -> pump.

    Splits on the FIRST ``__`` (the namespace boundary), so an action name that
    itself contains ``_`` (or ``__``) is preserved intact on the action side."""
    return tool_name.split(TOOL_SEP, 1)[0]


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
            if only_idempotent and not action.read_only:
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
                        "parameters": _project_action_params(action, thing.uri_variables),
                    },
                }
            )
            route[name] = action
    return specs, route
