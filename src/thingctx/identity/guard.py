# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Back-compat re-export shim.

:class:`EntraGatewayGuard`, :class:`AuthorizationError`, and the base
:class:`JwtGatewayGuard` / :class:`Grant` moved but keep importing from here. See
:mod:`thingctx.identity.providers.entra` and :mod:`thingctx.identity.jwt_guard`.
"""

from __future__ import annotations

from thingctx.identity.jwt_guard import (
    AuthorizationError,
    Grant,
    JwtGatewayGuard,
)
from thingctx.identity.providers.entra import EntraGatewayGuard

__all__ = [
    "AuthorizationError",
    "EntraGatewayGuard",
    "Grant",
    "JwtGatewayGuard",
]
