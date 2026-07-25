# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The Policy Decision Point and its pluggable grant source.

The PDP (:class:`PolicyDecisionPoint`) is a pure decision: given a validated
claim, a ``(thing, affordance, op)`` tuple, and the TD-derived vocabulary, it
returns permit or deny. No I/O, no transport knowledge. The grant source
(:class:`GrantSource`) answers "what tuples does this identity hold?" and is
duck-typed, so a role source, a scope source, or an OPA-over-HTTP source swaps in
with no PDP change. :class:`LocalPolicyGrantSource` is the reference one.

Wildcards (``*`` at the affordance or op position) expand only to tuples the
vocabulary already contains. So ``(pump, *, writeproperty)`` grants write on the
writable properties the TD declares, and nothing more: a wildcard cannot escape
the closed universe.
"""

from __future__ import annotations

import time as _time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from thingctx.authz.vocabulary import GrantTuple

# A grant is a set of (thing, affordance, op) tuples, where affordance and op may
# be the "*" wildcard; the PDP expands wildcards against the vocabulary.
GrantSet = set[GrantTuple]

WILDCARD = "*"


@dataclass(frozen=True)
class AccessRequest:
    """The tuple a PEP asks the PDP about, plus audit context.

    ``op`` is fixed per dispatch method (``read_property`` always asks
    ``readproperty``), so the PEP never guesses it. ``form_scheme`` is audit-only:
    the decision never reads it, so the same request is decided identically
    whether it would route over http, mqtt, or local.
    """

    thing_id: str
    affordance: str
    op: str
    form_scheme: str | None = None


@dataclass(frozen=True)
class Decision:
    """A PDP decision. A value, not an exception, so the enforcement point owns
    the failure shape (raise or return an envelope, its choice)."""

    permit: bool
    reason: str = ""


class AuthorizationDenied(Exception):  # noqa: N818 (public API name; the Error suffix would break consumers)
    """Raised when the PDP denies a call, before any device touch.

    Means: authenticated, but not authorized for this ``(thing, affordance,
    op)``. Distinct from an error envelope (``{"error": ...}`` return) and from an
    authentication failure (a bad token, before claims reach enforcement).

    Lives here, next to the PDP, so :class:`thingctx.ThingClient` can enforce
    inline without importing the enforcement module (a cycle). The PEP re-exports
    it.
    """

    def __init__(self, request: AccessRequest, reason: str) -> None:
        self.request = request
        self.reason = reason
        super().__init__(
            f"authorization denied: {request.op} on "
            f"{request.thing_id}/{request.affordance} ({reason})"
        )


def _token_expired(identity: Any, *, now: float | None = None) -> bool:
    """True if the identity's token has expired.

    ``identity`` is the validated claims dict; ``exp`` is a wall-clock deadline in
    epoch seconds. A missing ``exp`` counts as expired (fail-closed): the guard
    requires exp on inbound tokens, so its absence means an untrusted identity."""

    if not isinstance(identity, dict):
        return True  # no claims, cannot prove validity, treat as expired
    exp = identity.get("exp")
    if not isinstance(exp, int | float):
        return True
    return (now if now is not None else _time.time()) >= float(exp)


async def _authorized_stream(
    stream: AsyncIterator[Any],
    pdp: Any,
    identity: Any,
    request: AccessRequest,
    *,
    revocation_check: Callable[[Any, AccessRequest], Awaitable[bool]] | None = None,
) -> AsyncIterator[Any]:
    """Re-authorize each delivered value and stop the stream when authorization
    lapses, so a stream cannot outlive its authorization.

    Two lapse conditions: the identity's ``exp`` passes (checked against the wall
    clock on each delivery), or ``revocation_check(identity, request)`` returns
    True (a role pulled, a policy change; the default re-asks the PDP).

    A lapse cuts the stream forward. Values already delivered are not clawed back,
    the right semantics for a live feed.

    The expiry check runs only when the identity carries ``exp``. A ``None``
    identity and a local named principal are token-less: no ``exp`` means "not
    time-bounded," not "expired," so it must not stop the stream. The PDP decision
    gates the stream either way."""
    check_expiry = isinstance(identity, dict) and "exp" in identity
    async for value in stream:
        if check_expiry and _token_expired(identity):
            return  # token expired
        decision = await pdp.decide(identity, request)
        if not decision.permit:
            return  # grant or policy lapsed
        if revocation_check is not None and await revocation_check(identity, request):
            return
        yield value


@runtime_checkable
class GrantSource(Protocol):
    """Which ``(thing, affordance, op)`` tuples does this identity hold?

    Neutral over where the grant lives. A concrete source reads the claim (Entra
    ``roles``, a scope string, a local principal) and returns the grant set;
    wildcards are expanded by the PDP. Async so an OPA-backed source fits the same
    contract.
    """

    async def grant_for(self, identity: Any) -> GrantSet:
        pass


class LocalPolicyGrantSource:
    """A reference :class:`GrantSource`: a static role/scope to grant map.

    ``identity`` is a claims dict (what a token guard's ``validate`` returns). This
    reads one claim (default ``roles``) and unions the grant set every named role
    holds. A role's grants may use ``*`` at the affordance or op position; the PDP
    expands them against the TD vocabulary.

    The issuer emits a coarse ``roles`` claim; the fine ``(thing, affordance,
    op)`` expansion lives here, next to the TD, as thingctx policy. Swap this for
    an OPA or role-service source and the PDP is unchanged.

    Args:
        policy: ``{role_name: {(thing, affordance, op), ...}}``. affordance/op
            may be ``"*"``.
        claim: the claim that names the caller's roles (default ``"roles"``).
    """

    def __init__(
        self,
        policy: dict[str, GrantSet],
        *,
        claim: str = "roles",
    ) -> None:
        self._policy = {role: set(grants) for role, grants in policy.items()}
        self._claim = claim

    async def grant_for(self, identity: Any) -> GrantSet:
        roles = self._roles(identity)
        grants: GrantSet = set()
        for role in roles:
            grants |= self._policy.get(role, set())
        return grants

    def _roles(self, identity: Any) -> list[str]:
        """Pull the role list out of the claims. Also accepts a bare list/str for
        convenience in tests."""
        if identity is None:
            return []
        raw = identity.get(self._claim) if isinstance(identity, dict) else identity
        if raw is None:
            return []
        if isinstance(raw, str):
            return raw.split()
        return list(raw)


# Presets a single local user can pick with no identity and no policy file. Each
# lists the wildcard ops it grants; the affordance is ``*`` (PDP-expanded). thing_id
# is not wildcardable, so the grant is generated per Thing (see StaticGrantSource).
#
# read-only grants reads/observe/subscribe plus invokeaction on WoT-``safe`` actions
# only (safe: true means no state change, so it is read-like: readFile, listDir).
# It denies property writes and unsafe actions (writeFile, delete, a PTZ move).
POLICY_PRESETS: dict[str, tuple[str, ...]] = {
    "full": ("readproperty", "writeproperty", "observeproperty", "invokeaction", "subscribeevent"),
    "read-only": ("readproperty", "observeproperty", "subscribeevent"),
}

# Presets that also grant invokeaction on safe actions, generated per affordance
# from the WoT ``safe`` flag.
_SAFE_ACTION_PRESETS = frozenset({"read-only"})


class StaticGrantSource:
    """A :class:`GrantSource` that returns a fixed grant set, ignoring identity.

    For the single local user who wants a coarse posture with no identity provider
    or policy file: pick a preset (``full`` / ``read-only``) and every request is
    decided against ``(thing_id, "*", op)`` for each Thing id and preset op.

    ``read-only``'s safe-action grant is per-affordance, so it needs the parsed
    Things: pass ``things=``. ``thing_ids=`` still covers the wildcard ops; without
    ``things`` a ``read-only`` grant drops the safe-action allowance (stricter).
    """

    def __init__(self, preset: str, thing_ids: Any = (), *, things: Any = None) -> None:
        if preset not in POLICY_PRESETS:
            raise ValueError(
                f"unknown policy preset {preset!r}; choose one of {sorted(POLICY_PRESETS)}"
            )
        things = list(things) if things is not None else []
        ids = [t.id for t in things] if things else list(thing_ids)
        ops = POLICY_PRESETS[preset]
        grants: GrantSet = {(tid, "*", op) for tid in ids for op in ops}
        if preset in _SAFE_ACTION_PRESETS and things:
            for t in things:
                for name, action in getattr(t, "actions", {}).items():
                    if getattr(action, "safe", False):
                        grants.add((t.id, name, "invokeaction"))
        self._grants: GrantSet = grants

    @property
    def grants(self) -> GrantSet:
        """The fixed grant set this preset resolved to, so a caller can bind the
        same grant to a named role without re-deriving it."""
        return set(self._grants)

    async def grant_for(self, identity: Any) -> GrantSet:
        return set(self._grants)


@dataclass
class PolicyDecisionPoint:
    """Decide permit / deny for a request, against a TD vocabulary.

    Holds the closed vocabulary (from :func:`build_vocabulary`) and a
    :class:`GrantSource`. Permits iff the requested tuple is in both the grant and
    the vocabulary. The double check is the point: a grant that names an operation
    the TD never declared can never permit, wildcard or not.

    Args:
        vocabulary: the closed set of grantable tuples for the Things in scope.
        grant_source: the pluggable claim -> grant seam.
    """

    vocabulary: set[GrantTuple]
    grant_source: GrantSource

    async def decide(self, identity: Any, request: AccessRequest) -> Decision:
        target: GrantTuple = (request.thing_id, request.affordance, request.op)

        # Vocabulary gate first: an op the TD does not declare is not grantable.
        if target not in self.vocabulary:
            return Decision(
                permit=False,
                reason=(
                    f"{target} is not in the TD-derived vocabulary "
                    f"(no form of {request.affordance!r} declares op {request.op!r})"
                ),
            )

        grant = await self.grant_source.grant_for(identity)
        if self._granted(target, grant):
            return Decision(permit=True)

        return Decision(
            permit=False,
            reason=f"grant does not include {target}",
        )

    def _granted(self, target: GrantTuple, grant: GrantSet) -> bool:
        """Is ``target`` covered by ``grant``, honoring ``*`` wildcards?

        A grant tuple matches if each affordance/op position is ``*`` or equal.
        thing_id must match exactly (no wildcard Thing). ``target`` is already in
        the vocabulary, so a wildcard cannot widen beyond what the TD declares.
        """
        t_thing, t_aff, t_op = target
        for g_thing, g_aff, g_op in grant:
            if g_thing != t_thing:
                continue
            if g_aff not in (WILDCARD, t_aff):
                continue
            if g_op not in (WILDCARD, t_op):
                continue
            return True
        return False
