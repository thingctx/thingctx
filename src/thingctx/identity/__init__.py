# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Inbound and outbound identity for thingctx.

* **Outbound** (:mod:`thingctx.identity.provider`): a
  :class:`~thingctx.CredentialProvider` that authenticates the agent *as* an
  Entra identity to any Entra-aware target.
* **Inbound** (:mod:`thingctx.identity.jwt_guard` + :mod:`thingctx.identity.providers`):
  a provider-neutral gateway guard that validates an incoming access token, so
  thingctx can sit in front of devices that do not speak the IdP. Entra and
  Cloudflare Access ship; more register through the ``thingctx.guards``
  entry-point group (:mod:`thingctx.identity.registry`).

Every name is served lazily, so the guard needs ``pyjwt[crypto]`` + ``httpx``
(the ``authz`` extra) and the Entra provider needs ``azure-identity`` (the
``entra`` extra), neither loaded until the name is accessed.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Static visibility for the lazily served names; never imported at runtime.
    from thingctx.identity.jwt_guard import AuthorizationError, Grant, JwtGatewayGuard
    from thingctx.identity.provider import EntraAuth, make_provider
    from thingctx.identity.providers.cloudflare import CloudflareAccessGuard
    from thingctx.identity.providers.entra import EntraGatewayGuard
    from thingctx.identity.registry import (
        DEFAULT_GUARDS,
        GuardRegistry,
        discover_guards,
        register_guard,
    )

__all__ = [
    "DEFAULT_GUARDS",
    "AuthorizationError",
    "CloudflareAccessGuard",
    # Outbound Entra credential provider (needs azure-identity, the ``entra`` extra).
    "EntraAuth",
    "EntraGatewayGuard",
    "Grant",
    # Pluggability, mirroring discover_bindings / discover_auth.
    "GuardRegistry",
    # Inbound gateway guard (needs pyjwt[crypto] + httpx, the ``authz`` extra).
    "JwtGatewayGuard",
    "discover_guards",
    "make_provider",
    "register_guard",
]

_LAZY = {
    "EntraAuth": "thingctx.identity.provider",
    "make_provider": "thingctx.identity.provider",
    "JwtGatewayGuard": "thingctx.identity.jwt_guard",
    "Grant": "thingctx.identity.jwt_guard",
    "AuthorizationError": "thingctx.identity.jwt_guard",
    "EntraGatewayGuard": "thingctx.identity.providers.entra",
    "CloudflareAccessGuard": "thingctx.identity.providers.cloudflare",
    "GuardRegistry": "thingctx.identity.registry",
    "DEFAULT_GUARDS": "thingctx.identity.registry",
    "register_guard": "thingctx.identity.registry",
    "discover_guards": "thingctx.identity.registry",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is not None:
        return getattr(importlib.import_module(module), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
