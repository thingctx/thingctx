# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Enforcement helpers and the ``guard_client`` factory.

Enforcement lives inside :class:`thingctx.ThingClient`: each device-reaching
dispatch method authorizes the resolved ``(thing_id, affordance, op)`` against the
client's PDP before it selects a binding. So the enforcement point is singular and
has no wrapper to bypass, and ``as_tools()`` hands out the client's own authorized
``invoke``.

This module is the compatibility seam. :class:`AuthorizationDenied`,
:func:`_authorized_stream`, and :func:`_token_expired` are re-exported from
:mod:`thingctx.authz.pdp` (they moved there so the client can enforce without a
``client -> pep`` cycle). :func:`guard_client` builds the native guarded client
via :meth:`ThingClient.guarded`, not a proxy, so an old ``guard_client(...)`` call
gets the same enforcement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# Live next to the PDP to avoid a client -> pep cycle; re-exported so existing
# imports from here keep working.
from thingctx.authz.pdp import (
    AuthorizationDenied,
    _authorized_stream,
    _token_expired,
)
from thingctx.runtime import ThingClient

if TYPE_CHECKING:
    from thingctx.authz.pdp import PolicyDecisionPoint

__all__ = [
    "AuthorizationDenied",
    "_authorized_stream",
    "_token_expired",
    "guard_client",
]


def guard_client(
    client: ThingClient,
    pdp: PolicyDecisionPoint,
    *,
    identity: Any = None,
    raise_on_deny: bool = True,
) -> ThingClient:
    """Return a client that authorizes every device-reaching call via ``pdp``.

    Sugar over :meth:`thingctx.ThingClient.guarded`. Not a proxy: it returns a
    ThingClient sharing this one's state with the authorization settings applied,
    so enforcement stays in the client's own dispatch methods.

    ``raise_on_deny=True`` (default) raises :class:`AuthorizationDenied`; ``False``
    returns a thingctx-style error envelope.
    """
    return client.guarded(pdp, identity=identity, authz_raise=raise_on_deny)
