# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""SOSA records from the operations the runtime reports.

A read becomes an Observation, a command an Actuation, a refused call a Refusal. A
long running action is one Actuation: its start writes the record without an
outcome, and the query or cancel that ends it writes the same record (same id) with
the outcome. Every record model offers these two functions with these signatures.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from thingctx.models.sosa._generated.records import (
    Actuation,
    ActuationOutcome,
    Observation,
    Refusal,
)
from thingctx.operations import Operation

# ActionStatus names to GoalStatus outcomes.
_OUTCOME = {"completed": "succeeded", "failed": "aborted", "cancelled": "canceled"}


def records_for(
    operation: Operation,
    *,
    actor: str | None = None,
    inflight: dict[str, tuple[Any, Any]] | None = None,
) -> list[Any]:
    """The records one operation adds. ``actor`` names the requester when the
    identity has no ``sub`` claim. ``inflight`` keeps a started action's arguments
    and start time by status href until a query or cancel reports it finished."""
    who = actor or _subject(operation.identity)
    if not operation.permitted:
        return [
            Refusal(
                operation=operation.affordance,
                feature_of_interest=operation.thing_id,
                actor=who,
                reason=operation.reason,
                described_by=operation.thing_id,
                description_version=operation.thing_version,
                refused_time=operation.ended,
            )
        ]
    status = operation.result if hasattr(operation.result, "terminal") else None
    if operation.op == "readproperty":
        if operation.error is not None or _failed(operation.result):
            return []
        return [
            Observation(
                result=operation.result,
                observed_property=operation.affordance,
                feature_of_interest=operation.thing_id,
                procedure=operation.href,
                phenomenon_time=operation.ended,
                result_time=operation.ended,
            )
        ]
    if operation.op in ("invokeaction", "writeproperty"):
        if status is not None and not status.terminal:
            if inflight is not None and status.href:
                inflight[status.href] = (operation.arguments, operation.started)
            return [
                _actuation(
                    operation,
                    who=who,
                    arguments=operation.arguments,
                    started=operation.started,
                    outcome=None,
                    status=status,
                )
            ]
        outcome = _outcome(operation, status)
        return [
            _actuation(
                operation,
                who=who,
                arguments=operation.arguments,
                started=operation.started,
                outcome=outcome,
                status=status,
            )
        ]
    if operation.op in ("queryaction", "cancelaction"):
        if status is None or not status.terminal:
            return []
        key = status.href or ""
        if inflight is None:
            arguments, started = None, operation.started
        elif key in inflight:
            arguments, started = inflight.pop(key)
        else:
            return []
        return [
            _actuation(
                operation,
                who=who,
                arguments=arguments,
                started=started,
                outcome=_outcome(operation, status),
                status=status,
            )
        ]
    return []


def recorder(
    write: Callable[[Any], Any], *, actor: str | None = None
) -> Callable[[Operation], None]:
    """A ``record`` callable for ``ThingClient``: each record an operation adds goes
    to ``write``. Keeps the in flight actions so a goal's end lands on its record."""
    inflight: dict[str, tuple[Any, Any]] = {}

    def record(operation: Operation) -> None:
        for item in records_for(operation, actor=actor, inflight=inflight):
            write(item)

    return record


def _actuation(
    operation: Operation,
    *,
    who: str | None,
    arguments: Any,
    started: Any,
    outcome: str | None,
    status: Any,
) -> Actuation:
    return Actuation(
        result=arguments,
        acts_on_property=operation.affordance,
        feature_of_interest=operation.thing_id,
        procedure=operation.href,
        actor=who,
        outcome=ActuationOutcome(outcome) if outcome else None,
        reason=operation.reason,
        described_by=operation.thing_id,
        description_version=operation.thing_version,
        phenomenon_time=started,
        result_time=operation.ended,
        evidence=getattr(status, "href", None),
    )


def _outcome(operation: Operation, status: Any) -> str:
    if status is not None:
        return _OUTCOME.get(status.status, "aborted")
    if operation.error is not None or _failed(operation.result):
        return "aborted"
    return "succeeded"


def _failed(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


def _subject(identity: Any) -> str | None:
    if isinstance(identity, dict):
        sub = identity.get("sub")
        return str(sub) if sub is not None else None
    return identity if isinstance(identity, str) else None
