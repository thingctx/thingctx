# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Validate a TD against the bundled W3C WoT TD 1.1 schema
(1.1-12-March-2025, official W3C validation schema). Needs the [validate]
extra (jsonschema).

    problems = validate_td(my_td)        # [] if conformant
    thingctx.from_td(td, validate=True)  # raises TDValidationError
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).parent / "data" / "td-schema-1.1.json"
_schema_cache: dict | None = None


class TDValidationError(ValueError):
    """A Thing Description failed W3C TD 1.1 schema validation."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(f"Thing Description is not valid WoT TD 1.1:\n  - {joined}")


def _load_schema() -> dict:
    global _schema_cache  # noqa: PLW0603  module-level schema cache; the global is the cache seam
    if _schema_cache is None:
        _schema_cache = json.loads(_SCHEMA_PATH.read_text())
    return _schema_cache


def validate_td(td: dict[str, Any]) -> list[str]:
    """Return a list of validation problems for ``td`` ([] if valid)
    against the bundled W3C WoT TD 1.1 schema.

    Each problem is ``"<location>: <message>"``. Raises ImportError with
    a hint if ``jsonschema`` isn't installed.
    """
    try:
        # optional dep, kept local so the core imports without the extra
        from jsonschema import Draft7Validator  # noqa: PLC0415
    except ImportError:
        raise ImportError(
            "validate_td needs jsonschema, `pip install thingctx[validate]`"
        ) from None

    validator = Draft7Validator(_load_schema())
    problems: list[str] = []
    for err in sorted(validator.iter_errors(td), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "(root)"
        problems.append(f"{loc}: {err.message}")
    return problems


def assert_valid_td(td: dict[str, Any]) -> None:
    """Raise :class:`TDValidationError` if ``td`` isn't conformant."""
    problems = validate_td(td)
    if problems:
        raise TDValidationError(problems)


# Legal form operations per affordance kind (WoT TD 1.1 op vocabulary).
_PROPERTY_OPS = {"readproperty", "writeproperty", "observeproperty", "unobserveproperty"}
_ACTION_OPS = {"invokeaction", "queryaction", "cancelaction"}
_EVENT_OPS = {"subscribeevent", "unsubscribeevent"}
_THING_OPS = {
    "readallproperties",
    "writeallproperties",
    "readmultipleproperties",
    "writemultipleproperties",
    "observeallproperties",
    "unobserveallproperties",
    "subscribeallevents",
    "unsubscribeallevents",
    "queryallactions",
}
_VAR = re.compile(r"\{([^}]+)\}")


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list | tuple) else [value]


def validate_semantics(td: dict[str, Any]) -> list[str]:
    """Return semantic problems a JSON Schema cannot catch ([] if clean):

    - every ``{var}`` in a form ``href`` resolves to a declared uriVariable
      (affordance-level or Thing-level);
    - every referenced security name (Thing-level, form-level, and a combo's
      ``oneOf`` / ``allOf``) is defined in ``securityDefinitions``;
    - a form's ``scopes`` reference an oauth2 scheme and stay within its
      declared scopes;
    - each form ``op`` is legal for the affordance it belongs to.
    """
    problems: list[str] = []
    defs = td.get("securityDefinitions") or {}
    defined = set(defs)
    thing_vars = set(td.get("uriVariables") or {})

    def _check_security(names: Any, where: str) -> None:
        for name in _as_list(names):
            if name not in defined:
                problems.append(f"{where}: security {name!r} is not in securityDefinitions")
                continue
            scheme = (defs.get(name) or {}).get("scheme")
            if scheme == "combo":
                for key in ("oneOf", "allOf"):
                    problems.extend(
                        f"{where}: combo {name!r} {key} references undefined {ref!r}"
                        for ref in _as_list((defs.get(name) or {}).get(key))
                        if ref not in defined
                    )

    _check_security(td.get("security"), "(root)")
    for name, sdef in defs.items():
        if (sdef or {}).get("scheme") == "combo":
            _check_security(name, f"securityDefinitions/{name}")

    def _check_form(form: dict, local_vars: set, legal: set, where: str) -> None:
        for var in _VAR.findall(form.get("href", "")):
            # A leading ``+`` is RFC 6570 reserved expansion ({+var}); the
            # variable name it declares is ``var``, matching WoTForm.fill().
            name = var.removeprefix("+")
            if name not in local_vars and name not in thing_vars:
                problems.append(f"{where}: href var {{{var}}} has no matching uriVariable")
        ops = _as_list(form.get("op"))
        problems.extend(
            f"{where}: op {op!r} is not legal for this affordance" for op in ops if op not in legal
        )
        if form.get("security") is not None:
            _check_security(form.get("security"), where)
        scopes = _as_list(form.get("scopes"))
        if scopes:
            sec = _as_list(form.get("security")) or _as_list(td.get("security"))
            oauth = [n for n in sec if (defs.get(n) or {}).get("scheme") == "oauth2"]
            if not oauth:
                problems.append(f"{where}: scopes declared but no oauth2 security in scope")
            for n in oauth:
                declared = set(_as_list((defs.get(n) or {}).get("scopes")))
                if declared and not set(scopes) <= declared:
                    problems.append(
                        f"{where}: scopes {scopes} exceed {n!r} declared {sorted(declared)}"
                    )

    for form in _as_list(td.get("forms")):
        _check_form(form, set(), _THING_OPS, "forms")
    kinds = (("properties", _PROPERTY_OPS), ("actions", _ACTION_OPS), ("events", _EVENT_OPS))
    for kind, legal in kinds:
        for aff_name, aff in (td.get(kind) or {}).items():
            local = set((aff or {}).get("uriVariables") or {})
            for i, form in enumerate(_as_list((aff or {}).get("forms"))):
                _check_form(form, local, legal, f"{kind}/{aff_name}/forms/{i}")
    return problems


def assert_semantics(td: dict[str, Any]) -> None:
    """Raise :class:`TDValidationError` if ``td`` has semantic problems."""
    problems = validate_semantics(td)
    if problems:
        raise TDValidationError(problems)


# Security scheme types thingctx resolves into applied credential material today.
# (digest, psk, and combo are modeled by the parser but not yet applied.)
_APPLIED_SECURITY = {"nosec", "basic", "bearer", "apikey", "oauth2", "auto"}
# Subprotocols the runtime drives for push today. longpoll / websub are not.
_SUPPORTED_SUBPROTOCOLS = {"sse"}


def _active_security_names(td: dict[str, Any]) -> set[str]:
    """The security scheme names a TD would actually apply: Thing-level plus
    every form-level override (only these get attached to a request)."""
    names = set(_as_list(td.get("security")))

    def _from_forms(container: dict[str, Any]) -> None:
        for form in _as_list(container.get("forms")):
            names.update(_as_list(form.get("security")))

    _from_forms(td)
    for kind in ("properties", "actions", "events"):
        for aff in (td.get(kind) or {}).values():
            _from_forms(aff or {})
    return names


def validate_support(td: dict[str, Any]) -> list[str]:
    """Return the constructs a TD needs that thingctx does not yet drive ([] if
    fully supported): an active security scheme with no applier (digest, psk,
    combo) or a form subprotocol with no push binding (longpoll, websub). The
    runtime refuses such a TD under ``validate="strict"`` so a gap surfaces at
    load, not as a silent partial result at call time.
    """
    problems: list[str] = []
    defs = td.get("securityDefinitions") or {}
    for name in sorted(_active_security_names(td)):
        scheme = (defs.get(name) or {}).get("scheme")
        if scheme and scheme not in _APPLIED_SECURITY:
            problems.append(f"security {name!r}: scheme {scheme!r} is modeled but not applied yet")
    seen: set[str] = set()
    for kind in ("properties", "actions", "events"):
        for aff in (td.get(kind) or {}).values():
            for form in _as_list((aff or {}).get("forms")):
                sub = form.get("subprotocol")
                if sub and sub not in _SUPPORTED_SUBPROTOCOLS and sub not in seen:
                    seen.add(sub)
                    problems.append(f"subprotocol {sub!r} is not driven yet")
    return problems
