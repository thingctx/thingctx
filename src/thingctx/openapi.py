# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Compile an OpenAPI 3.x spec into a W3C WoT Thing Description.

Every OpenAPI operation becomes a TD action carrying a real HTTP form (method
and URL), so the resulting Thing is drivable directly by a ``ThingClient`` --
no server in the middle. Security schemes map across too (bearer, basic,
apikey, oauth2), so the generated TD authenticates the same way the API does.

    td = from_openapi(spec)                      # dict, a WoT TD 1.1
    td = from_openapi(load_spec("api.yaml"))     # from a file or URL

This is deliberately mechanical: it mirrors the spec rather than curating it.
Pass ``include`` to keep only the operations an agent should see.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

TD_CONTEXT = "https://www.w3.org/2022/wot/td/v1.1"
HTV = "http://www.w3.org/2011/http#"
_HTTP_METHODS = ("get", "put", "post", "delete", "patch")
# Kept from a vendor schema: the descriptive keys plus the constraint keys that
# are argument correctness (a model that respects minimum/maximum/pattern forms
# a valid call), not decoration.
_KEEP_KEYS = (
    "type",
    "description",
    "enum",
    "format",
    "default",
    "minimum",
    "maximum",
    "pattern",
)

# A header carrying a secret must never be written into a TD (a TD is meant to
# be committed and shared; the invoker holds secrets at call time).
_CREDENTIAL_HEADERS = {"authorization", "cookie", "proxy-authorization"}
_CREDENTIAL_HINT = re.compile(r"(api[-_]?key|token|secret|password|bearer)", re.I)


def _is_credential_header(field_name: str) -> bool:
    low = field_name.lower()
    return low in _CREDENTIAL_HEADERS or bool(_CREDENTIAL_HINT.search(low))


def _resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a local ``#/components/...`` JSON pointer."""
    node: Any = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _deref(spec: dict, node: Any) -> Any:
    """Single-level ``$ref`` resolution."""
    if isinstance(node, dict) and "$ref" in node:
        return _resolve_ref(spec, node["$ref"])
    return node


def _slim(spec: dict, schema: Any, depth: int = 0) -> dict:
    """Reduce a vendor schema to a lean, self-contained JSON Schema: resolve
    ``$ref``, keep only the keys an agent needs, recurse two levels. Vendor
    specs nest deeply; a TD input should be readable, not a spec mirror."""
    schema = _deref(spec, schema)
    if not isinstance(schema, dict):
        return {"type": "string"}
    if isinstance(schema.get("allOf"), list) and schema["allOf"]:
        merged: dict[str, Any] = {}
        for part in schema["allOf"]:
            merged.update(_deref(spec, part))
        schema = {**merged, **{k: v for k, v in schema.items() if k != "allOf"}}
    for k in ("oneOf", "anyOf"):
        if isinstance(schema.get(k), list) and schema[k]:
            first = _deref(spec, schema[k][0])
            if isinstance(first, dict):
                schema = {**first, **{kk: vv for kk, vv in schema.items() if kk != k}}
    out = {k: schema[k] for k in _KEEP_KEYS if k in schema}
    if depth < 2:
        if isinstance(schema.get("items"), dict):
            out["items"] = _slim(spec, schema["items"], depth + 1)
        if isinstance(schema.get("properties"), dict):
            out["properties"] = {
                n: _slim(spec, s, depth + 1) for n, s in schema["properties"].items()
            }
            if schema.get("required"):
                out["required"] = schema["required"]
    if "type" not in out:
        out["type"] = "object" if "properties" in out else "string"
    return out


_STRUCTURED_BODY = (
    "application/json",
    "application/x-www-form-urlencoded",
    "multipart/form-data",
)


def _request_media(spec: dict, op: dict) -> tuple[str | None, dict | None]:
    """The operation's representative request body media type and media object,
    or ``(None, None)``. A structured type (json, form, multipart) is preferred
    so its schema yields arguments; otherwise the first declared type (e.g. a
    binary ``application/octet-stream`` upload) is used."""
    body = _deref(spec, op.get("requestBody")) if op.get("requestBody") else None
    if not body:
        return None, None
    content = body.get("content") or {}
    for ct in _STRUCTURED_BODY:
        if ct in content:
            return ct, _deref(spec, content[ct])
    for ct, media in content.items():
        return ct, _deref(spec, media)
    return None, None


def _required_headers(spec: dict, op: dict) -> list[str]:
    """Names of the operation's required ``in: header`` parameters, excluding
    credential-shaped ones (a secret never enters a TD). These become declared
    ``htv:headers`` on the form; the value is supplied at call time."""
    out: list[str] = []
    for p in op.get("parameters", []):
        p = _deref(spec, p)
        if p.get("in") != "header" or not p.get("required"):
            continue
        field = p.get("name", "")
        if field and not _is_credential_header(field):
            out.append(field)
    return out


def _input_schema(
    spec: dict, op: dict, body_fields: list[str] | None = None
) -> tuple[dict | None, str | None]:
    """Build the action input JSON Schema from an operation's path/query
    parameters and (if present) its request body. A structured body contributes
    its schema properties; a binary body contributes a single ``body`` argument
    carrying the bytes. Returns ``(schema, content_type)``: schema is None when
    the operation takes no input; content_type is the request body's media type
    (or None when there is no body). ``body_fields`` limits the body properties
    kept (an override)."""
    props: dict[str, Any] = {}
    required: list[str] = []

    for p in op.get("parameters", []):
        p = _deref(spec, p)
        if p.get("in") not in ("path", "query"):
            continue
        schema = _deref(spec, p.get("schema", {"type": "string"}))
        entry = {k: schema[k] for k in ("type", "enum", "format") if k in schema}
        # An array parameter carries its allowed values in items (e.g. an enum
        # of legal names). Dropping items leaves the model guessing at valid
        # entries, so keep the slimmed item schema.
        if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
            item = _deref(spec, schema["items"])
            entry["items"] = {k: item[k] for k in ("type", "enum", "format") if k in item} or {
                "type": "string"
            }
        if p.get("description"):
            entry["description"] = p["description"]
        props[p["name"]] = entry or {"type": "string"}
        if p.get("required") or p.get("in") == "path":
            required.append(p["name"])

    # Derive the form contentType from the request body media key rather than
    # assuming JSON: a form-encoded or binary API needs the right content type
    # or the request is malformed.
    content_type, media = _request_media(spec, op)
    if media is not None:
        bschema = _deref(spec, media.get("schema", {}))
        if content_type in _STRUCTURED_BODY and bschema.get("properties"):
            for name, sub in (bschema.get("properties") or {}).items():
                if body_fields is not None and name not in body_fields:
                    continue
                props[name] = _slim(spec, sub)
            required += [
                r
                for r in bschema.get("required", [])
                if r not in required and (body_fields is None or r in body_fields)
            ]
        elif content_type not in _STRUCTURED_BODY:
            # A binary / opaque body: one argument carries the request bytes.
            props["body"] = {"type": "string", "format": "binary", "description": "request body"}
            if "body" not in required:
                required.append("body")

    if not props:
        return None, content_type
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out, content_type


def _output_schema(spec: dict, op: dict) -> dict | None:
    """The slimmed schema of the operation's success (2xx) JSON response, or
    None when it declares no JSON response body."""
    responses = op.get("responses") or {}
    for code in list(responses):
        if not (code == "default" or str(code).startswith("2")):
            continue
        resp = _deref(spec, responses[code])
        media = (resp.get("content") or {}).get("application/json")
        if media and media.get("schema"):
            return _slim(spec, media["schema"])
    return None


def _safe(method: str) -> bool:
    """GET and HEAD are safe (read-only) per the TD safety hint."""
    return method.upper() in ("GET", "HEAD")


def _action_name(op: dict, method: str, path: str) -> str:
    """operationId if present, else a readable slug from method and path."""
    if op.get("operationId"):
        return op["operationId"]
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")
    return f"{method.lower()}_{slug}"


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "api"


def _security_from_spec(spec: dict) -> tuple[dict, list[str]]:
    """Map OpenAPI components.securitySchemes + global security to a TD
    (securityDefinitions, active-names) pair. Returns ({}, []) if the spec
    declares no security (the TD then carries an explicit nosec scheme)."""
    comps = (spec.get("components") or {}).get("securitySchemes") or {}
    defs: dict[str, Any] = {}
    for name, raw in comps.items():
        raw = _deref(spec, raw)
        kind = raw.get("type")
        if kind == "http" and raw.get("scheme", "").lower() == "bearer":
            defs[name] = {"scheme": "bearer", "in": "header"}
        elif kind == "http" and raw.get("scheme", "").lower() == "basic":
            defs[name] = {"scheme": "basic", "in": "header"}
        elif kind == "apiKey":
            defs[name] = {
                "scheme": "apikey",
                "in": raw.get("in", "header"),
                "name": raw.get("name", "Authorization"),
            }
        elif kind == "oauth2":
            flows = raw.get("flows") or {}
            if "authorizationCode" in flows:  # the user-consent flow
                f = flows["authorizationCode"]
                defs[name] = {
                    "scheme": "oauth2",
                    "flow": "code",
                    "authorization": f.get("authorizationUrl", ""),
                    "token": f.get("tokenUrl", ""),
                    "refresh": f.get("refreshUrl", ""),
                    "scopes": list((f.get("scopes") or {}).keys()),
                }
            else:
                f = flows.get("clientCredentials") or flows.get("password") or {}
                defs[name] = {
                    "scheme": "oauth2",
                    "flow": "client_credentials" if "clientCredentials" in flows else "password",
                    "token": f.get("tokenUrl", ""),
                    "scopes": list((f.get("scopes") or {}).keys()),
                }
    groups = [g for g in spec.get("security", []) if g]
    # A requirement object lists every scheme that must be satisfied together
    # (AND), so keep all of its keys, not just the first. Fall back to every
    # defined scheme when no global requirement is declared.
    active = list(groups[0].keys()) if groups else list(defs.keys())
    # Keep only active schemes we understand.
    active = [a for a in active if a in defs]
    return defs, active


def _op_security(op: dict, defs: dict) -> list[str] | None:
    """Map an operation's own ``security`` (if any) to TD form-level scheme
    names. Returns None when the operation inherits the Thing-level security,
    and [] when the operation explicitly requires no auth."""
    if "security" not in op:
        return None
    groups = [g for g in (op.get("security") or []) if g]
    if not groups:
        return []
    return [a for a in groups[0].keys() if a in defs]


def from_openapi(
    spec: dict,
    *,
    base_url: str | None = None,
    id: str | None = None,
    title: str | None = None,
    security: dict | None = None,
    include: Callable[[str, str, str], bool] | list[str] | None = None,
    overrides: dict[str, dict] | None = None,
    headers: dict[str, str] | None = None,
    outputs: bool = False,
) -> dict:
    """Compile an OpenAPI 3.x ``spec`` (a dict) into a WoT TD 1.1 dict.

    base_url   override the server URL (else ``servers[0].url`` from the spec).
    id         TD id (else ``urn:thingctx:<title-slug>``).
    title      Thing title (else ``info.title``).
    security   override security as ``{"definitions": {...}, "active": [...]}``;
               by default the spec's own security schemes are mapped.
    include    keep an operation if the predicate ``(name, method, path)`` is
               true, or if its operationId/name is in the given list. Default:
               keep every operation.
    overrides  per-operation curation, keyed by ``"METHOD /path"`` (e.g.
               ``"POST /charges"``). Each value may set ``name``, ``description``,
               and ``body_fields`` (the request-body properties to keep). Keying
               by operation, not by generated tool name, so the caller need not
               reproduce the naming rules.
    headers    fixed header values applied to every operation's form as declared
               ``htv:headers``. A credential-shaped header name is refused, since
               a secret must not enter a TD.
    outputs    when True, emit an ``output`` schema from each operation's success
               response. Experimental: it changes what a model sees per call, so
               it is off by default until measured.
    """
    overrides = overrides or {}
    fixed_headers = {k: v for k, v in (headers or {}).items() if not _is_credential_header(k)}
    info = spec.get("info") or {}
    title = title or info.get("title") or "OpenAPI Thing"
    base = (base_url or _server_url(spec)).rstrip("/")
    thing_id = id or f"urn:thingctx:{_slugify(title)}"

    if isinstance(include, list):
        wanted = set(include)
        keep = lambda n, m, p: n in wanted  # noqa: E731
    elif callable(include):
        keep = include
    else:
        keep = lambda n, m, p: True  # noqa: E731

    if security is not None:
        defs, active = dict(security.get("definitions", {})), list(security.get("active", []))
    else:
        defs, active = _security_from_spec(spec)

    actions: dict[str, Any] = {}
    needs_nosec = False
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in _HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            name = _action_name(op, method, path)
            if not keep(name, method, path):
                continue
            ov = overrides.get(f"{method.upper()} {path}", {})
            if ov.get("name"):
                name = ov["name"]
            inp, body_ct = _input_schema(spec, op, body_fields=ov.get("body_fields"))
            form: dict[str, Any] = {
                "href": base + path,
                "htv:methodName": method.upper(),
                # Content type follows the request body's media key; default to
                # JSON only when the operation declares no body.
                "contentType": body_ct or "application/json",
            }
            hdrs = _required_headers(spec, op)
            declared = {h: None for h in hdrs}
            declared.update(fixed_headers)
            if declared:
                form["htv:headers"] = [
                    {"htv:fieldName": fn, **({"htv:fieldValue": fv} if fv is not None else {})}
                    for fn, fv in declared.items()
                ]
            # An operation may override the Thing-level security; carry that
            # onto the form so the generated TD authenticates per-operation.
            op_sec = _op_security(op, defs)
            if op_sec is not None and op_sec != active:
                if op_sec:
                    form["security"] = op_sec
                else:
                    form["security"] = ["nosec_sc"]
                    needs_nosec = True
            action: dict[str, Any] = {
                "title": name,
                "description": ov.get("description")
                or op.get("summary")
                or op.get("description")
                or name,
                "safe": _safe(method),
                "idempotent": _safe(method) or method in ("put", "delete"),
                "forms": [form],
            }
            if inp:
                action["input"] = inp
            if outputs:
                out_schema = _output_schema(spec, op)
                if out_schema:
                    action["output"] = out_schema
            # De-dup operationId collisions across paths.
            key = name if name not in actions else f"{method}_{name}"
            actions[key] = action

    if not defs:
        defs, active = {"nosec_sc": {"scheme": "nosec"}}, ["nosec_sc"]
    if needs_nosec and "nosec_sc" not in defs:
        defs["nosec_sc"] = {"scheme": "nosec"}

    return {
        "@context": [TD_CONTEXT, {"htv": HTV}],
        "@type": "Thing",
        "id": thing_id,
        "title": title,
        "description": info.get("description", title),
        "securityDefinitions": defs,
        "security": active,
        "actions": actions,
    }


def _server_url(spec: dict) -> str:
    servers = spec.get("servers") or []
    if servers and isinstance(servers[0], dict):
        return servers[0].get("url", "")
    return ""


def load_spec(source: str) -> dict:
    """Load an OpenAPI spec from a file path or http(s) URL. JSON is parsed
    natively; YAML needs ``pyyaml`` (the ``openapi`` extra)."""
    if source.startswith(("http://", "https://")):
        import httpx

        text = httpx.get(source, follow_redirects=True, timeout=30.0).text
    else:
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
    return _parse_spec(text)


def _parse_spec(text: str) -> dict:
    try:
        return json.loads(text)
    except ValueError:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - guidance path
            raise ValueError(
                "spec is not JSON and PyYAML is not installed; "
                'install the YAML support with: pip install "thingctx[openapi]"'
            ) from exc
        return yaml.safe_load(text)
