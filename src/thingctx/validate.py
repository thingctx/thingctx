"""Validate a TD against the bundled W3C WoT TD 1.1 schema
(1.1-12-March-2025, official W3C validation schema). Needs the [validate]
extra (jsonschema).

    problems = validate_td(my_td)        # [] if conformant
    thingctx.from_td(td, validate=True)  # raises TDValidationError
"""

from __future__ import annotations

import json
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
    global _schema_cache
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
        from jsonschema import Draft7Validator
    except ImportError as e:  # noqa: F841
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
