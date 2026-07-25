"""WoT-derived authorization for thingctx: the dependency-free seam.

This subpackage is the enforcement seam, and nothing more. It derives an
authorization vocabulary from a Thing Description, decides ``permit`` / ``deny``
against it, and enforces the decision at the runtime dispatch layer, all on top
of the installed thingctx with no extra runtime dependency.

Three roles, one module each:

* :mod:`~thingctx.authz.vocabulary` builds the TD-derived grant vocabulary:
  the closed set of ``(thing_id, affordance, op)`` tuples a Thing actually
  declares, applying the WoT default-op rule. A grant is valid only if it is in
  this set.
* :mod:`~thingctx.authz.pdp` is the Policy Decision Point: a pluggable
  :class:`GrantSource` maps a validated claim to the grants an identity holds,
  and the PDP decides ``permit`` / ``deny`` for a requested tuple against the
  vocabulary.
* The Policy Enforcement Point is now :class:`thingctx.ThingClient` itself: each
  device-reaching dispatch method (``invoke`` / ``read_property`` /
  ``write_property`` / ``subscribe`` / ``frames`` / ``publish``) authorizes the
  resolved ``(thing_id, affordance, op)`` against the PDP BEFORE it selects a
  binding, so every transport of a multi-transport Thing hits one check and
  there is no wrapper to bypass. :mod:`~thingctx.authz.pep` keeps the
  :func:`guard_client` factory (which builds that native guarded client) and
  re-exports :class:`AuthorizationDenied`.

:mod:`~thingctx.authz.authzen` maps the same decision boundary to the
OpenID AuthZEN standard, so the PDP can be an external conformant service. Its
HTTP dependency is imported lazily, so importing this package stays dep-free.

The :class:`GrantSource` / :class:`PolicyDecisionPoint` protocols and the
:class:`AccessRequest` / :class:`Decision` values are the contract a separate
provider package implements and consumes; a token guard that yields a claims
dict composes with enforcement by passing that dict as the identity.
"""

from __future__ import annotations

from thingctx.authz.authzen import (
    AUTHZEN_EVALUATION_PATH,
    AuthZenPDP,
    from_authzen_response,
    to_authzen_request,
)
from thingctx.authz.pdp import (
    AccessRequest,
    Decision,
    GrantSet,
    GrantSource,
    LocalPolicyGrantSource,
    PolicyDecisionPoint,
)
from thingctx.authz.pep import (
    AuthorizationDenied,
    guard_client,
)
from thingctx.authz.vocabulary import (
    DEFAULT_ACTION_OPS,
    DEFAULT_EVENT_OPS,
    DEFAULT_PROPERTY_OPS,
    GrantTuple,
    build_vocabulary,
)

__all__ = [
    "AUTHZEN_EVALUATION_PATH",
    "DEFAULT_ACTION_OPS",
    "DEFAULT_EVENT_OPS",
    "DEFAULT_PROPERTY_OPS",
    # pdp
    "AccessRequest",
    # authzen
    "AuthZenPDP",
    "AuthorizationDenied",
    "Decision",
    "GrantSet",
    "GrantSource",
    # vocabulary
    "GrantTuple",
    "LocalPolicyGrantSource",
    "PolicyDecisionPoint",
    "build_vocabulary",
    "from_authzen_response",
    # pep
    "guard_client",
    "to_authzen_request",
]
