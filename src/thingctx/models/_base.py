# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Behaviour every generated record inherits.

A vocabulary describes what a field means. It does not say a timestamp needs a
timezone, or that the same fact written twice should be one row. That logic lives
here. ``Record`` works for any model because it reads the vocabulary from tables the
generator fills in, and never names a term itself.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, model_validator

__all__ = ["Record"]

# The UUID namespace every record id derives from. Fixed, so the same record built
# on two machines gets the same id. Name based UUIDs: RFC 9562 section 5.5.
_NAMESPACE = uuid.UUID("6d9d7a3e-6a2e-5b41-9c4a-2f6f1a2c7c58")

# Fields that say when the record was written. Defaulted to now when absent.
_OWN_TIMES = ("result_time", "refused_time")


def _utc(when: Any, what: str) -> datetime:
    """Return ``when`` in UTC, or raise if it has no timezone. These records get
    compared across devices, and a timestamp without a zone cannot be compared."""
    if not isinstance(when, datetime):
        # ValueError on purpose: pydantic reports a ValueError as a validation error
        # and lets a TypeError crash the caller.
        raise ValueError(f"{what} must be a datetime, got {type(when).__name__}")  # noqa: TRY004
    if when.tzinfo is None:
        raise ValueError(f"{what} needs a timezone; a naive timestamp cannot be correlated")
    return when.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    """Serialize ``value`` the same way every time, for hashing. Rejects anything
    that is not plain JSON."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"an observation result must be JSON serializable, got {type(value).__name__}: {exc}"
        ) from exc


class Record(BaseModel):
    """Base class of every generated record.

    Fills in defaults and the id before validation, and converts a record to a table
    row or a JSON-LD document. Frozen: a record states what happened, so nothing may
    change it afterwards.
    """

    # The vocabulary tables. Empty here; the generated module of each model fills them.
    _CONTEXT: ClassVar[dict[str, str]] = {}
    _TERMS: ClassVar[dict[str, dict[str, str]]] = {}
    _TYPES: ClassVar[dict[str, list[str]]] = {}
    _IDENTITY: ClassVar[dict[str, list[str]]] = {}
    _ID_NAMESPACE: ClassVar[dict[str, str]] = {}
    _NESTED: ClassVar[dict[str, dict[str, Any]]] = {}
    _STATUS_IRI: ClassVar[dict[str, str]] = {}

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        arbitrary_types_allowed=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _complete(cls, data: Any) -> Any:
        """Fill in the timestamps the caller left out, then derive the id from the other
        fields. Runs before validation because the record is frozen afterwards."""
        if not isinstance(data, dict):
            return data
        filled = dict(data)

        for name in _OWN_TIMES:
            if name in cls.model_fields:
                given = filled.get(name)
                filled[name] = _utc(given if given is not None else _now(), name)

        if "phenomenon_time" in cls.model_fields:
            given = filled.get("phenomenon_time")
            filled["phenomenon_time"] = (
                _utc(given, "phenomenon_time") if given is not None else filled["result_time"]
            )

        if "ages" in cls.model_fields:
            filled["ages"] = dict(filled.get("ages") or {})

        filled["id"] = cls._derive_id(filled)
        return filled

    @classmethod
    def _derive_id(cls, filled: dict[str, Any]) -> str:
        namespace = cls._ID_NAMESPACE.get(cls.__name__)
        root = uuid.uuid5(_NAMESPACE, namespace) if namespace else _NAMESPACE
        identity = [filled.get(field) for field in cls._IDENTITY[cls.__name__]]
        identity = [v.isoformat() if isinstance(v, datetime) else v for v in identity]
        return f"urn:uuid:{uuid.uuid5(root, _canonical(identity))}"

    def replace(self, **changes: Any) -> Any:
        """Return a copy with ``changes`` applied and the id derived again.
        pydantic's ``model_copy`` skips validation and would keep the old id."""
        fields = {name: getattr(self, name) for name in type(self).model_fields}
        del fields["id"]
        return type(self)(**(fields | changes))

    def as_row(self) -> dict[str, Any]:
        """Return the record as a flat dict for a table: plain JSON values, timestamps
        as ISO strings, keys are the field names."""
        row: dict[str, Any] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, list):
                value = list(value)
            elif isinstance(value, dict):
                value = dict(value)
            row[name] = value
        return row

    def as_jsonld(self) -> dict[str, Any]:
        """Return the record as a JSON-LD document, each field under its vocabulary
        term. Empty fields are left out. A field the schema marks as unpublished has
        no term and is left out too."""
        kind = type(self).__name__
        row = self.as_row()
        for field in ("outcome", "status"):
            if row.get(field) is not None:
                row[field] = self._STATUS_IRI[row[field]]

        types = self._TYPES[kind]
        doc: dict[str, Any] = {
            "@context": dict(self._CONTEXT),
            "@id": row["id"],
            "@type": types[0] if len(types) == 1 else list(types),
        }
        for name, term in self._TERMS[kind].items():
            value = row[name]
            if value is None or value == []:
                continue
            doc[term] = value

        node = self._nested(kind, row)
        if node is not None:
            doc[self._NESTED[kind]["predicate"]] = node
        return doc

    @classmethod
    def _nested(cls, kind: str, row: dict[str, Any]) -> dict[str, Any] | None:
        """Build the nested node for fields the schema groups together, for example
        the description a decision was judged against and its version."""
        shape = cls._NESTED.get(kind)
        if shape is None:
            return None
        node: dict[str, Any] = {"@type": shape["type"]}
        if (identifier := row.get(shape["id_from"])) is not None:
            node["@id"] = identifier
        for name, term in shape["terms"].items():
            if (value := row.get(name)) is not None:
                node[term] = value
        return node if len(node) > 1 else None


def _now() -> datetime:
    return datetime.now(timezone.utc)
