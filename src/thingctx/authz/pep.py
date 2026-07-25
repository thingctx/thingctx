"""Enforcement helpers and the ``guard_client`` factory.

Authorization enforcement lives INSIDE :class:`thingctx.ThingClient` now: each
device-reaching dispatch method (``invoke`` / ``read_property`` /
``write_property`` / ``subscribe`` / ``frames`` / ``publish``) authorizes the
resolved ``(thing_id, affordance, op)`` against the client's PDP before it
selects a binding or touches any transport. That makes the enforcement point
singular and unbypassable: there is no wrapper around the client to drift from
its dispatch surface, and ``as_tools()`` hands out the client's own authorized
``invoke`` by construction.

This module is now just the compatibility seam:

* :class:`AuthorizationDenied`, :func:`_authorized_stream`, and
  :func:`_token_expired` are re-exported from :mod:`thingctx.authz.pdp`, which
  is where they moved so the client can raise/enforce without importing this
  module (that would be a cycle). Existing imports from here keep working.
* :func:`guard_client` builds the native guarded client (via
  :meth:`ThingClient.guarded`) rather than a proxy, so a call site that wrote
  ``guard_client(client, pdp, identity=...)`` gets the same enforcement with no
  second dispatch surface to bypass.

Native enforcement in the dispatch methods has no external proxy to drift from
the real dispatch surface, so there is no wrapper to bypass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from thingctx import ThingClient

# Re-exported from the PDP module (their new home): the client enforces inline,
# so these live next to the PDP to avoid a client -> pep import cycle. Keeping
# the names importable from here preserves existing imports.
from thingctx.authz.pdp import (
    AuthorizationDenied,
    _authorized_stream,
    _token_expired,
)

if TYPE_CHECKING:
    from thingctx.authz.pdp import PolicyDecisionPoint

__all__ = [
    "AuthorizationDenied",
    "guard_client",
    "_authorized_stream",
    "_token_expired",
]


def guard_client(
    client: ThingClient,
    pdp: PolicyDecisionPoint,
    *,
    identity: Any = None,
    raise_on_deny: bool = True,
) -> ThingClient:
    """Return a client that authorizes every device-reaching call via ``pdp``.

    Sugar over :meth:`thingctx.ThingClient.guarded`. It does NOT wrap the client
    in a proxy: it returns a ThingClient that shares this client's internal state
    with only the authorization settings set, so enforcement lives in the
    client's own dispatch methods and there is nothing to bypass. ``as_tools()``
    on the result hands back that client's authorized ``invoke``.

    ``raise_on_deny=True`` (default) raises :class:`AuthorizationDenied` on a
    denial; ``False`` returns a thingctx-style error envelope instead.
    """
    return client.guarded(pdp, identity=identity, authz_raise=raise_on_deny)
