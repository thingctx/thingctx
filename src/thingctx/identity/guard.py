# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Inbound: thingctx becomes the identity-aware gateway.

This module is the historical home of :class:`EntraGatewayGuard` and keeps
importing it (and :class:`AuthorizationError`) working from here. The guard is
now one thin provider on a provider-neutral base:

* :class:`~thingctx.identity.jwt_guard.JwtGatewayGuard` holds all the generic JWT
  validation (JWKS fetch + cache, RS256 signature verification against the
  ``kid``, iss/aud/exp/nbf checks, the reason-not-leaked errors, the
  caller-to-device ``authorize_and_invoke`` bridge);
* :class:`~thingctx.identity.providers.entra.EntraGatewayGuard` supplies only the
  three Entra-specific things (the issuers, the JWKS URL, the scp/roles
  claims);
* :class:`~thingctx.identity.providers.cloudflare.CloudflareAccessGuard` is a
  second provider on the same base;
* guards are discovered / registered exactly like thingctx bindings and auth
  providers, via :mod:`thingctx.identity.registry`.
"""

from __future__ import annotations

from thingctx.identity.jwt_guard import (
    AuthorizationError,
    Grant,
    JwtGatewayGuard,
)
from thingctx.identity.providers.entra import EntraGatewayGuard

__all__ = [
    "EntraGatewayGuard",
    "AuthorizationError",
    "JwtGatewayGuard",
    "Grant",
]
