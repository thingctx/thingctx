"""Trust + grounding.

Two safety primitives a consumer needs before it lets an agent drive a Thing:

- **Approval gating** , a human (or policy) must say yes before a risky action
  runs. Risk is read from the TD (``tc:requiresApproval`` / ``@type
  tc:Destructive``) and/or from a policy ("any non-idempotent write", "all").
- **Grounding** , ``verify()`` checks a TD against the *live* Thing (its
  readable properties actually read, and match their declared types) so you do
  not trust a description that no longer matches reality.

Both are opt-in and have no LLM dependency.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from thingctx.thing import WoTAction

# When to ask the approver before an action runs:
#   declared    , only actions the TD marks risky (tc:requiresApproval/Destructive)
#   destructive , the above plus any non-idempotent (non-safe) action
#   all         , every action (and every property write)
#   never       , gating off
ApprovePolicy = Literal["declared", "destructive", "all", "never"]


@dataclass
class ApprovalRequest:
    """What the approver is asked to allow. Returned truthy = proceed."""

    tool_name: str
    arguments: dict[str, Any]
    thing_id: str
    action_name: str
    reason: str
    description: str = ""


# An approver allows or denies a gated call. Sync or async; truthy = allow.
Approver = Callable[[ApprovalRequest], "bool | Awaitable[bool]"]


def _action_reason(action: WoTAction, policy: ApprovePolicy) -> str | None:
    """Reason this action needs approval under ``policy``, or None to proceed."""
    if policy == "never":
        return None
    if policy == "all":
        return "policy=all"
    if action.requires_approval():
        return "TD-declared (tc:requiresApproval / tc:Destructive)"
    if policy == "destructive" and action.is_destructive():
        return "non-idempotent action"
    return None


async def _ask(approve: Approver | None, req: ApprovalRequest) -> dict[str, Any] | None:
    """Run the approver. Returns an error envelope to block, or None to allow.

    No approver wired but approval is required = deny (the safe default): a gate
    with nobody to open it stays shut."""
    if approve is None:
        return {
            "error": "approval required but no approver configured",
            "tool": req.tool_name,
            "reason": req.reason,
            "hint": "pass approve=<callable> to ThingClient, or approve_when='never' to disable",
        }
    verdict = approve(req)
    if inspect.isawaitable(verdict):
        verdict = await verdict
    if not verdict:
        return {"error": "approval denied", "tool": req.tool_name, "reason": req.reason}
    return None


async def gate_action(
    action: WoTAction,
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    approve: Approver | None,
    policy: ApprovePolicy,
) -> dict[str, Any] | None:
    """Error envelope if this action call is blocked, else None to proceed."""
    reason = _action_reason(action, policy)
    if reason is None:
        return None
    return await _ask(
        approve,
        ApprovalRequest(
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            thing_id=action.thing_id,
            action_name=action.name,
            reason=reason,
            description=action.description,
        ),
    )


async def gate_write(
    thing_id: str,
    name: str,
    value: Any,
    *,
    approve: Approver | None,
    policy: ApprovePolicy,
) -> dict[str, Any] | None:
    """A property write is a state mutation; gate it under destructive/all."""
    if policy not in ("destructive", "all"):
        return None
    return await _ask(
        approve,
        ApprovalRequest(
            tool_name=name,
            arguments={"value": value},
            thing_id=thing_id,
            action_name=f"write {name}",
            reason="property write (state mutation)",
        ),
    )


# --- grounding: verify a TD against the live Thing -------------------------

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "integer": (int,),
    "number": (int, float),
    "string": (str,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


@dataclass
class Check:
    """One grounding check and its outcome."""

    target: str
    ok: bool
    detail: str = ""


@dataclass
class VerifyReport:
    """Result of grounding a Thing's TD against its live endpoint."""

    thing_id: str
    ok: bool
    checks: list[Check] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "thing": self.thing_id,
            "ok": self.ok,
            "checks": [{"target": c.target, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def _type_ok(schema: dict[str, Any], value: Any) -> tuple[bool, str]:
    """Lenient type check: only fails on a clear scalar-type mismatch."""
    # Binary/media payloads (e.g. an image property) are not JSON-typed; a
    # successful read is the signal, so never flag bytes as a type mismatch.
    if isinstance(value, (bytes, bytearray)):
        return True, ""
    declared = schema.get("type") if isinstance(schema, dict) else None
    if not declared or declared not in _JSON_TYPES:
        return True, ""
    expected = _JSON_TYPES[declared]
    # bool is an int in Python; keep them distinct for declared types.
    if declared != "boolean" and isinstance(value, bool):
        return False, f"declared {declared}, got boolean"
    if isinstance(value, expected):
        return True, ""
    return False, f"declared {declared}, got {type(value).__name__}"


async def verify_thing(client: Any, thing: Any) -> VerifyReport:
    """Ground one Thing: read each readable property and check it answers and
    matches its declared scalar type. Actions are listed but never invoked
    (invoking has side effects), so grounding stays read-only and safe."""
    from thingctx.thing import _tool_name

    checks: list[Check] = []
    for prop in thing.properties.values():
        if not prop.readable:
            continue
        target = f"property:{prop.name}"
        name = _tool_name(thing.id, prop.name)
        try:
            value = await client.read_property(name)
        except Exception as exc:  # a live transport can raise; record, don't crash
            checks.append(Check(target, False, f"read raised {type(exc).__name__}: {exc}"))
            continue
        if isinstance(value, dict) and "error" in value:
            checks.append(Check(target, False, str(value["error"])))
            continue
        type_ok, detail = _type_ok(prop.schema, value)
        checks.append(Check(target, type_ok, detail or "read ok"))
    if not checks:
        checks.append(Check("properties", True, "no readable properties to ground"))
    return VerifyReport(thing.id, ok=all(c.ok for c in checks), checks=checks)
