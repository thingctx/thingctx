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
_KEEP_KEYS = ("type", "description", "enum", "format", "default")


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


def _input_schema(spec: dict, op: dict) -> dict | None:
    """Build the action input JSON Schema from an operation's path/query
    parameters and (if present) its JSON or form-encoded request body. Returns
    None when the operation takes no input."""
    props: dict[str, Any] = {}
    required: list[str] = []

    for p in op.get("parameters", []):
        p = _deref(spec, p)
        if p.get("in") not in ("path", "query"):
            continue
        schema = _deref(spec, p.get("schema", {"type": "string"}))
        entry = {k: schema[k] for k in ("type", "enum", "format") if k in schema}
        if p.get("description"):
            entry["description"] = p["description"]
        props[p["name"]] = entry or {"type": "string"}
        if p.get("required") or p.get("in") == "path":
            required.append(p["name"])

    body = _deref(spec, op.get("requestBody")) if op.get("requestBody") else None
    if body:
        content = body.get("content", {})
        media = (
            content.get("application/json")
            or content.get("application/x-www-form-urlencoded")
            or {}
        )
        bschema = _deref(spec, media.get("schema", {}))
        for name, sub in (bschema.get("properties") or {}).items():
            props[name] = _slim(spec, sub)
        required += [r for r in bschema.get("required", []) if r not in required]

    if not props:
        return None
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


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
            flow = flows.get("clientCredentials") or flows.get("password") or {}
            defs[name] = {
                "scheme": "oauth2",
                "flow": "client_credentials" if "clientCredentials" in flows else "password",
                "token": flow.get("tokenUrl", ""),
                "scopes": list((flow.get("scopes") or {}).keys()),
            }
    active = [next(iter(g)) for g in spec.get("security", []) if g] or list(defs.keys())
    # Keep only active schemes we understand.
    active = [a for a in active if a in defs]
    return defs, active


def from_openapi(
    spec: dict,
    *,
    base_url: str | None = None,
    id: str | None = None,
    title: str | None = None,
    security: dict | None = None,
    include: Callable[[str, str, str], bool] | list[str] | None = None,
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
    """
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

    actions: dict[str, Any] = {}
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
            action: dict[str, Any] = {
                "title": name,
                "description": op.get("summary") or op.get("description") or name,
                "safe": _safe(method),
                "idempotent": _safe(method) or method in ("put", "delete"),
                "forms": [
                    {
                        "href": base + path,
                        "htv:methodName": method.upper(),
                        "contentType": "application/json",
                    }
                ],
            }
            inp = _input_schema(spec, op)
            if inp:
                action["input"] = inp
            # De-dup operationId collisions across paths.
            key = name if name not in actions else f"{method}_{name}"
            actions[key] = action

    if security is not None:
        defs, active = dict(security.get("definitions", {})), list(security.get("active", []))
    else:
        defs, active = _security_from_spec(spec)
    if not defs:
        defs, active = {"nosec_sc": {"scheme": "nosec"}}, ["nosec_sc"]

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
