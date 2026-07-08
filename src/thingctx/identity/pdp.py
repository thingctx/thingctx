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

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from thingctx.identity.vocabulary import GrantTuple

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
    """A PDP decision. A value, not an exception, so the PEP owns the failure
    shape (it raises or returns an envelope as it sees fit)."""

    permit: bool
    reason: str = ""


@runtime_checkable
class GrantSource(Protocol):
    """Answers: which ``(thing, affordance, op)`` tuples does this identity hold?

    Provider-neutral over WHERE the grant lives. A concrete source reads the
    claim (Entra ``roles``, a scope string, a local principal) and returns the
    grant set; wildcards are allowed and expanded by the PDP. Async so a source
    backed by a network policy engine (OPA) fits the same contract.
    """

    async def grant_for(self, identity: Any) -> GrantSet: ...


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
        if isinstance(identity, dict):
            raw = identity.get(self._claim)
        else:
            raw = identity
        if raw is None:
            return []
        if isinstance(raw, str):
            return raw.split()
        return list(raw)


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
