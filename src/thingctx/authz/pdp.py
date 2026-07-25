# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The Policy Decision Point and its pluggable grant source.

The split:

* The **PDP** (:class:`PolicyDecisionPoint`) is a pure decision. Given a
  validated claim, a requested ``(thing, affordance, op)`` tuple, and the
  TD-derived vocabulary, it returns ``permit`` or ``deny(reason)``. No I/O, no
  transport knowledge.
* The **grant source** (:class:`GrantSource`) is the pluggable PDP-provider
  seam: it answers "what tuples does this identity hold?" and is duck-typed so a
  role-claim source, a scope source, or an OPA-over-HTTP source could replace it
  with zero PDP change. One reference implementation ships here:
  :class:`LocalPolicyGrantSource`, a role/scope to grant map with wildcard
  support.

Wildcards (``*`` at the affordance or op position) are convenience OVER the
closed universe, never an escape from it. A wildcard grant expands only to
tuples the vocabulary already contains, so ``(pump, *, writeproperty)`` grants
write on exactly the writable properties the TD declares, and nothing else.
"""

from __future__ import annotations

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
    ``readproperty``, etc.), so the PEP never guesses it. ``form_scheme`` is
    carried for audit and for the multi-transport point: the SAME request is
    decided identically whether the form that would route it is http, mqtt, or
    local, because the decision does not read the scheme.
    """

    thing_id: str
    affordance: str
    op: str
    form_scheme: str | None = None


@dataclass(frozen=True)
class Decision:
    """A PDP decision. A value, not an exception, so the enforcement point owns
    the failure shape (it raises or returns an envelope as it sees fit)."""

    permit: bool
    reason: str = ""


class AuthorizationDenied(Exception):  # noqa: N818 (public API name; the Error suffix would break consumers)
    """Raised when the PDP denies a call, before any device touch.

    Deliberately distinct from thingctx's own error ENVELOPES (dict returns like
    ``{"error": ...}``) and from an authentication error (a failure to validate
    the inbound token before its claims ever reach enforcement). This one means:
    the identity was authenticated, but is not authorized for this
    ``(thing, affordance, op)``.

    Lives here (next to the PDP) so :class:`thingctx.ThingClient` can enforce
    authorization inline without importing the enforcement module, which would be
    a cycle. The PEP re-exports it for backward compatibility.
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

    The identity is the validated claims dict the guard returned; a JWT carries
    ``exp`` (seconds since the epoch), a wall-clock deadline compared against now.
    A missing ``exp`` is treated as expired (fail-closed): the guard requires exp
    on inbound tokens, so its absence here means an untrusted identity."""
    import time as _time

    if not isinstance(identity, dict):
        return True  # no claims -> cannot prove validity -> treat as expired
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
    """Wrap a device stream so each delivered value is re-authorized, and STOP the
    stream the moment authorization lapses.

    Two lapse conditions:
    1. TOKEN EXPIRY: the identity's ``exp`` deadline passes while the stream lives,
       checked against the wall clock on each delivery, so a stream cannot outlive
       the token that authorized it.
    2. REVOCATION: an optional ``revocation_check(identity, request) -> bool`` that
       returns True to revoke (a role pulled, a policy change). The default re-asks
       the PDP, catching a policy/grant change.

    On lapse we cut the stream FORWARD (stop yielding); we do not claw back values
    already delivered, the correct semantics for a live-feed revocation.

    The token-expiry check applies only when the identity carries an ``exp`` (a
    time-bounded token). A ``None`` identity (the no-code preset) and a local
    opt-in identity (a named principal) are both token-less: absence of ``exp``
    means "not a time-bounded token," not "expired," so it must not stop the
    stream. The PDP decision always gates the stream regardless."""
    check_expiry = isinstance(identity, dict) and "exp" in identity
    async for value in stream:
        if check_expiry and _token_expired(identity):
            return  # token expired: stop the stream
        decision = await pdp.decide(identity, request)
        if not decision.permit:
            return  # grant/policy lapsed: stop the stream
        if revocation_check is not None and await revocation_check(identity, request):
            return
        yield value


@runtime_checkable
class GrantSource(Protocol):
    """Answers: which ``(thing, affordance, op)`` tuples does this identity hold?

    Provider-neutral over WHERE the grant lives. A concrete source reads the
    claim (Entra ``roles``, a scope string, a local principal) and returns the
    grant set; wildcards are allowed and expanded by the PDP. Async so a source
    backed by a network policy engine (OPA) fits the same contract.
    """

    async def grant_for(self, identity: Any) -> GrantSet:
        pass


class LocalPolicyGrantSource:
    """A reference :class:`GrantSource`: a static role/scope to grant map.

    The identity is expected to be a claims dict (what a token guard's
    ``validate`` returns). This source reads one claim (default ``roles``) and
    unions the grant set every named role holds. A role's grants may use ``*`` at
    the affordance or op position; the PDP expands them against the TD vocabulary
    at decision time.

    This is the named-grant-sets binding: the issuer emits a coarse ``roles``
    claim, and the fine ``(thing, affordance, op)`` expansion lives here, next to
    the TD, as thingctx policy. Swap this class for an OPA or role-service source
    and the PDP is unchanged.

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
        """Pull the role list out of the claims. Accepts a dict of claims (the
        guard's output), or a bare list/str for convenience in tests."""
        if identity is None:
            return []
        raw = identity.get(self._claim) if isinstance(identity, dict) else identity
        if raw is None:
            return []
        if isinstance(raw, str):
            return raw.split()
        return list(raw)


# The named policy presets a single local user can pick with no identity and no policy
# file. Each names the wildcard ops it grants; the affordance is the ``*`` wildcard (any
# affordance), which the PDP expands against the TD vocabulary. The thing_id is NOT
# wildcardable in the PDP, so the grant is generated per Thing (see StaticGrantSource).
#
# "read-only" grants reads/observe/subscribe AND, per the WoT ``safe`` flag, invokeaction
# on SAFE actions only (an action with ``safe: true`` causes no state change, so it is
# read-like: e.g. readFile, listDir, search). It denies property writes and UNSAFE actions
# (writeFile, sendMessage, delete, a PTZ move). This is "look but don't touch": read
# everything, run no-op queries, change nothing.
# "full" grants every op.
POLICY_PRESETS: dict[str, tuple[str, ...]] = {
    "full": ("readproperty", "writeproperty", "observeproperty", "invokeaction", "subscribeevent"),
    "read-only": ("readproperty", "observeproperty", "subscribeevent"),
}

# Presets that additionally grant invokeaction on SAFE actions only, generated per
# affordance from the ``safe`` flag. read-only is safe-permitting: it reads the WoT
# ``safe`` flag for its defined meaning (no state change), NOT a new axis.
_SAFE_ACTION_PRESETS = frozenset({"read-only"})


class StaticGrantSource:
    """A :class:`GrantSource` that returns a fixed grant set, ignoring identity.

    For the single local user who wants a coarse posture without an identity provider
    or a policy file: pick a named preset (``full`` / ``read-only``, see
    ``POLICY_PRESETS``) and every request is decided against that fixed grant. The
    grant is ``(thing_id, "*", op)`` for each Thing id and each preset op.

    ``read-only``'s safe-action ``invokeaction`` grant is per-affordance, so it
    needs the parsed Things: pass ``things=`` (preferred). ``thing_ids=`` still
    works for the wildcard ops; without ``things`` a ``read-only`` grant omits the
    safe-action allowance (the stricter behavior).
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
            # invokeaction on safe actions only: reads the WoT safe flag, no new axis.
            for t in things:
                for name, action in getattr(t, "actions", {}).items():
                    if getattr(action, "safe", False):
                        grants.add((t.id, name, "invokeaction"))
        self._grants: GrantSet = grants

    @property
    def grants(self) -> GrantSet:
        """The fixed grant set this preset resolved to (identity-free). Lets a caller
        bind the same preset grant to a named role without re-deriving it."""
        return set(self._grants)

    async def grant_for(self, identity: Any) -> GrantSet:
        return set(self._grants)


@dataclass
class PolicyDecisionPoint:
    """Decide ``permit`` / ``deny`` for a request, against a TD vocabulary.

    Pure once constructed: it holds the closed vocabulary (from
    :func:`build_vocabulary`) and a :class:`GrantSource`. For each request it
    asks the source for the identity's grant set, expands any wildcards against
    the vocabulary, and permits iff the requested tuple is in BOTH the expanded
    grant and the vocabulary. The double check is deliberate: the vocabulary is
    the TD-closed universe, so a grant that names an operation the TD never
    declared can never permit, even a wildcard one.

    Args:
        vocabulary: the closed set of grantable tuples for the Things in scope.
        grant_source: the pluggable claim -> grant seam.
    """

    vocabulary: set[GrantTuple]
    grant_source: GrantSource

    async def decide(self, identity: Any, request: AccessRequest) -> Decision:
        target: GrantTuple = (request.thing_id, request.affordance, request.op)

        # Capability/vocabulary gate first: an op the TD does not declare is not
        # grantable at all. This is what closes the vocabulary.
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

        A grant tuple matches the target if each of its affordance/op positions
        is either ``*`` or equal to the target's. The thing_id must match
        exactly (a wildcard Thing is not supported). ``target`` is already known
        to be in the vocabulary, so a wildcard cannot widen beyond what the TD
        declares.
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
