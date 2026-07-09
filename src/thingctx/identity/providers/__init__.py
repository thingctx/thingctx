# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Reference identity providers for the pluggable gateway guard.

Each provider is a thin subclass of
:class:`~thingctx.identity.jwt_guard.JwtGatewayGuard` that supplies only the three
provider-specific things (issuers, JWKS URL, authorization grant shape) and a
zero-argument factory for the ``thingctx.guards`` entry point. A third-party IdP
(Cognito, Google, Auth0, ...) is added the same way in its own package: subclass
the base, expose a factory, advertise it under ``thingctx.guards``. No core
change is needed to add one.
"""

from __future__ import annotations

from thingctx.identity.providers.cloudflare import (
    CloudflareAccessGuard,
    make_cloudflare_guard,
)
from thingctx.identity.providers.entra import EntraGatewayGuard, make_entra_guard

__all__ = [
    "EntraGatewayGuard",
    "make_entra_guard",
    "CloudflareAccessGuard",
    "make_cloudflare_guard",
]
