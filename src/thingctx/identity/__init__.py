"""WoT-derived authorization for thingctx: the dependency-free seam.

This subpackage is the enforcement seam, and nothing more. It derives an
authorization vocabulary from a Thing Description, decides ``permit`` / ``deny``
against it, and enforces the decision at the runtime dispatch layer, all on top
of the installed thingctx with no extra runtime dependency.

Three roles, one module each:

* :mod:`~thingctx.identity.vocabulary` builds the TD-derived grant vocabulary:
  the closed set of ``(thing_id, affordance, op)`` tuples a Thing actually
  declares, applying the WoT default-op rule. A grant is valid only if it is in
  this set.
* :mod:`~thingctx.identity.pdp` is the Policy Decision Point: a pluggable
  :class:`GrantSource` maps a validated claim to the grants an identity holds,
  and the PDP decides ``permit`` / ``deny`` for a requested tuple against the
  vocabulary.
* :mod:`~thingctx.identity.pep` is the Policy Enforcement Point: a proxy around
  :class:`thingctx.ThingClient` that intercepts ``invoke`` / ``read_property`` /
  ``write_property`` and calls the PDP BEFORE the real client touches any
  binding, so every transport of a multi-transport Thing hits one check.

:mod:`~thingctx.identity.authzen` maps the same decision boundary to the
OpenID AuthZEN standard, so the PDP can be an external conformant service. Its
HTTP dependency is imported lazily, so importing this package stays dep-free.

The :class:`GrantSource` / :class:`PolicyDecisionPoint` protocols and the
:class:`AccessRequest` / :class:`Decision` values are the contract a separate
provider package implements and consumes; a token guard that yields a claims
dict composes with the PEP by passing that dict as the identity.
"""

from __future__ import annotations

from thingctx.identity.authzen import (
    AUTHZEN_EVALUATION_PATH,
    AuthZenPDP,
    from_authzen_response,
    to_authzen_request,
)
from thingctx.identity.pdp import (
    AccessRequest,
    Decision,
    GrantSet,
    GrantSource,
    LocalPolicyGrantSource,
    PolicyDecisionPoint,
)
from thingctx.identity.pep import (
    AuthorizationDenied,
    AuthorizedClient,
    guard_client,
)
from thingctx.identity.vocabulary import (
    DEFAULT_ACTION_OPS,
    DEFAULT_EVENT_OPS,
    DEFAULT_PROPERTY_OPS,
    GrantTuple,
    build_vocabulary,
)

__all__ = [
    # vocabulary
    "GrantTuple",
    "build_vocabulary",
    "DEFAULT_PROPERTY_OPS",
    "DEFAULT_ACTION_OPS",
    "DEFAULT_EVENT_OPS",
    # pdp
    "AccessRequest",
    "Decision",
    "GrantSet",
    "GrantSource",
    "LocalPolicyGrantSource",
    "PolicyDecisionPoint",
    # pep
    "AuthorizedClient",
    "guard_client",
    "AuthorizationDenied",
    # authzen
    "AuthZenPDP",
    "to_authzen_request",
    "from_authzen_response",
    "AUTHZEN_EVALUATION_PATH",
]
