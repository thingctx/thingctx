# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Inbound identity for thingctx: authenticate a caller, then let thingctx.authz decide.

Two directions, one package, kept distinct from the dependency-free authorization
seam in :mod:`thingctx.authz`:

* **Outbound** (:mod:`thingctx.identity.provider`): a
  :class:`~thingctx.CredentialProvider` that authenticates the agent *as* an
  Entra identity to any Entra-aware target. It wraps ``azure-identity`` so the
  whole Entra credential chain (client secret, certificate, managed identity,
  workload identity federation, developer CLI login) is available behind one
  scheme. Discovered by thingctx through the ``thingctx.auth`` entry point.

* **Inbound** (:mod:`thingctx.identity.jwt_guard` + :mod:`thingctx.identity.providers`):
  a provider-neutral gateway guard that validates an incoming access token (real
  JWT signature + issuer + audience + expiry + claim-based authorization against
  the provider's live JWKS), so thingctx can sit in front of devices that do not
  speak the IdP. Two reference providers ship (Entra, Cloudflare Access); more are
  added through the ``thingctx.guards`` entry-point group
  (:mod:`thingctx.identity.registry`).

Every name here is served lazily. Importing :mod:`thingctx.identity` pulls no
heavy dependency: the guard needs ``pyjwt[crypto]`` + ``httpx`` (the ``authz``
extra) and the Entra provider needs ``azure-identity`` (the ``entra`` extra), and
neither is loaded until the corresponding name is actually accessed. The
dependency-free base install is never affected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Static visibility for the lazily served names below. This block never runs
    # (TYPE_CHECKING is False at runtime), so it imports no heavy dependency; it
    # only lets type checkers and static analysis resolve the __all__ exports.
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
    # Outbound Entra credential provider (needs azure-identity, the ``entra`` extra).
    "EntraAuth",
    "make_provider",
    # Inbound gateway guard (needs pyjwt[crypto] + httpx, the ``authz`` extra).
    "JwtGatewayGuard",
    "Grant",
    "AuthorizationError",
    "EntraGatewayGuard",
    "CloudflareAccessGuard",
    # Pluggability, mirroring discover_bindings / discover_auth.
    "GuardRegistry",
    "DEFAULT_GUARDS",
    "register_guard",
    "discover_guards",
]

# Every name maps to the module that defines it. Serving these lazily keeps a bare
# ``import thingctx.identity`` free of pyjwt, httpx, and azure-identity; a heavy
# dependency loads only when the consumer accesses the name that needs it.
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


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is not None:
        import importlib

        return getattr(importlib.import_module(module), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
