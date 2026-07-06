# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Lint a Thing Description for whether an agent can use it.

``validate_td`` answers whether a TD is legal against the W3C TD 1.1 schema.
``lint_td`` answers a different question: once the TD is projected to tool
specs (see ``docs/MAPPING.md``, which is normative for what a model sees), will
a model be able to choose and call its affordances? A TD can be schema-valid and
still project a tool with no description, no argument names, or a name the model
providers reject.

The linter reads one document, returns findings, and touches no network. It never
rejects a valid TD; every finding is advice. Rule ids are stable ``snake_case``
strings so a caller can filter or suppress by id.

    from thingctx.lint import lint_td
    for f in lint_td(td):
        print(f.severity, f.target, f.rule, f.message)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# The tool-name charset the OpenAI/Anthropic function-calling APIs accept.
# ``_tool_name`` in thing.py keeps ``. _ -`` in the slug; a name outside this
# set is silently rejected by a provider at call time, so flag it here.
_TOOL_NAME_OK = re.compile(r"^[A-Za-z0-9_.-]+$")

# The shape ``from_openapi`` emits when an operation has no ``operationId``:
# a method and a path slug joined by an underscore (e.g. ``get_users_id``).
_GENERATED_NAME = re.compile(r"^(get|post|put|patch|delete|head|options)_[a-z0-9_]+$", re.I)

# A single word (no space) is almost never a usable description for a model.
_SINGLE_TOKEN = re.compile(r"^\S+$")

# Header names that carry a secret. A TD is meant to be committed and shared, so
# a credential in ``htv:headers`` is the one thing the security posture forbids.
_CREDENTIAL_HEADERS = {"authorization", "cookie", "proxy-authorization"}
_CREDENTIAL_HINT = re.compile(r"(api[-_]?key|token|secret|password|bearer)", re.I)

_MIN_DESCRIPTION = 8  # a description under this many characters carries no meaning


@dataclass
class LintFinding:
    """One lint result. ``severity`` is "error" | "warn" | "notice";
    ``target`` is a JSON-pointer-style path into the TD; ``rule`` is a stable
    id; ``message`` states the problem in the reader's terms."""

    severity: str
    target: str
    rule: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "target": self.target,
            "rule": self.rule,
            "message": self.message,
        }


def lint_td(td: dict[str, Any]) -> list[LintFinding]:
    """Return findings for one TD, ordered by where they occur. Never raises on
    a well-formed dict; a caller should still ``validate_td`` for legality."""
    out: list[LintFinding] = []

    _lint_id(td, out)
    _lint_thing_type(td, out)

    for kind in ("actions", "properties", "events"):
        for name, aff in (td.get(kind) or {}).items():
            if not isinstance(aff, dict):
                continue
            base = f"{kind}/{name}"
            _lint_affordance_name(kind, name, base, out)
            _lint_description(name, aff, base, out)
            _lint_affordance_type(kind, aff, base, out)
            _lint_headers(aff, base, out)
        # per-kind detail below

    for name, action in (td.get("actions") or {}).items():
        if isinstance(action, dict):
            _lint_action_risk(name, action, f"actions/{name}", out)
            _lint_empty_input(name, action, f"actions/{name}", out)

    for name, prop in (td.get("properties") or {}).items():
        if isinstance(prop, dict):
            _lint_property_units(name, prop, f"properties/{name}", out)

    return out


def _lint_id(td: dict[str, Any], out: list[LintFinding]) -> None:
    tid = td.get("id") or td.get("@id")
    if isinstance(tid, str) and tid.startswith(("http://", "https://")):
        # ``_tool_name`` derives the tool-name prefix from the id's last path
        # segment; a URL-shaped id can collapse or collide, mangling every tool.
        out.append(
            LintFinding(
                "warn",
                "id",
                "url_shaped_id",
                "id is a URL; the tool-name prefix is derived from its last path "
                "segment and may collide. Prefer a urn: id.",
            )
        )


def _lint_thing_type(td: dict[str, Any], out: list[LintFinding]) -> None:
    if not td.get("@type"):
        out.append(
            LintFinding(
                "notice",
                "@type",
                "missing_thing_type",
                "Thing has no @type; W3C WoT Discovery typed search cannot find it.",
            )
        )


def _lint_affordance_name(kind: str, name: str, base: str, out: list[LintFinding]) -> None:
    if not _TOOL_NAME_OK.match(name):
        out.append(
            LintFinding(
                "error",
                base,
                "invalid_tool_name",
                f"{kind[:-1]} name {name!r} projects to a tool name outside the "
                "accepted charset [A-Za-z0-9_.-]; the model provider will reject it.",
            )
        )
    elif _GENERATED_NAME.match(name):
        out.append(
            LintFinding(
                "warn",
                base,
                "generated_name",
                f"{name!r} looks machine-generated (method plus path); a readable "
                "name helps a model choose it.",
            )
        )


def _lint_description(name: str, aff: dict[str, Any], base: str, out: list[LintFinding]) -> None:
    # Projection precedence (thing.py): description, then title, then the bare
    # name. If both are absent the model sees only the name.
    desc = aff.get("description") or aff.get("title")
    if not desc:
        out.append(
            LintFinding(
                "warn",
                base,
                "thin_description",
                "no description or title; the projected tool description falls back "
                "to the bare name, which tells a model nothing about what it does.",
            )
        )
        return
    if desc == name:
        out.append(
            LintFinding(
                "notice",
                base,
                "thin_description",
                "description duplicates the name; it adds nothing a model can use.",
            )
        )
    elif len(desc) < _MIN_DESCRIPTION or _SINGLE_TOKEN.match(desc):
        out.append(
            LintFinding(
                "notice",
                base,
                "thin_description",
                f"description {desc!r} is too short to guide a model.",
            )
        )


def _lint_affordance_type(
    kind: str, aff: dict[str, Any], base: str, out: list[LintFinding]
) -> None:
    if not aff.get("@type"):
        out.append(
            LintFinding(
                "notice",
                base,
                "missing_affordance_type",
                "no @type; a class term makes the affordance groupable and findable "
                "in W3C WoT Discovery typed search.",
            )
        )


def _lint_action_risk(name: str, action: dict[str, Any], base: str, out: list[LintFinding]) -> None:
    # thing.py treats an action as idempotent when either ``safe`` or
    # ``idempotent`` is set; the trust gate must otherwise assume worst case.
    safe = action.get("safe")
    idem = action.get("idempotent")
    at_type = action.get("@type")
    marks = " ".join(at_type) if isinstance(at_type, list | tuple) else str(at_type or "")
    has_tc_mark = "tc:" in marks or "requiresApproval" in action
    if safe is None and idem is None and not has_tc_mark:
        out.append(
            LintFinding(
                "notice",
                base,
                "unmarked_risk",
                "no safe/idempotent flag and no tc: risk marking; the approval gate "
                "must assume this action is destructive on every call.",
            )
        )


def _lint_empty_input(name: str, action: dict[str, Any], base: str, out: list[LintFinding]) -> None:
    inp = action.get("input")
    if not isinstance(inp, dict):
        return
    if inp.get("type") == "object" and not inp.get("properties"):
        out.append(
            LintFinding(
                "warn",
                f"{base}/input",
                "empty_parameters",
                "input is an object with no properties; the model gets no argument "
                "names and cannot form a call.",
            )
        )


def _lint_property_units(
    name: str, prop: dict[str, Any], base: str, out: list[LintFinding]
) -> None:
    if prop.get("type") in ("number", "integer") and not prop.get("unit") and not prop.get("@type"):
        out.append(
            LintFinding(
                "notice",
                base,
                "missing_units",
                "numeric property has no unit and no @type; a model must guess what "
                "the number measures.",
            )
        )


def _lint_headers(aff: dict[str, Any], base: str, out: list[LintFinding]) -> None:
    for i, form in enumerate(aff.get("forms") or []):
        if not isinstance(form, dict):
            continue
        headers = form.get("htv:headers")
        if not isinstance(headers, list):
            continue
        for h in headers:
            if not isinstance(h, dict):
                continue
            field = str(h.get("htv:fieldName", ""))
            low = field.lower()
            if low in _CREDENTIAL_HEADERS or _CREDENTIAL_HINT.search(low):
                out.append(
                    LintFinding(
                        "error",
                        f"{base}/forms/{i}",
                        "credential_in_td",
                        f"form header {field!r} looks like a credential; a TD is meant "
                        "to be shared and must carry no secret. The invoker holds secrets.",
                    )
                )
