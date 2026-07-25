# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Microsoft Entra ID provider for the pluggable gateway guard.

:class:`EntraGatewayGuard` is a thin subclass of
:class:`~thingctx.identity.jwt_guard.JwtGatewayGuard` supplying the three
Entra-specific things:

1. the issuers: ``login.microsoftonline.com/{tenant}/v2.0`` (v2 access tokens)
   and ``sts.windows.net/{tenant}/`` (v1 access tokens);
2. the JWKS URL: ``login.microsoftonline.com/{tenant}/discovery/v2.0/keys``;
3. the authorization claims: delegated scopes in ``scp`` (space-delimited) and
   app roles in ``roles`` (a list).

Entra's app-only (agent) tokens carry app permissions in ``roles``, never
``scp``; a delegated (user) token carries ``scp``. This class lets a caller
require either or both, matching that split.
"""

from __future__ import annotations

from thingctx.identity.jwt_guard import AuthorizationError, Grant, JwtGatewayGuard

__all__ = ["AuthorizationError", "EntraGatewayGuard", "make_entra_guard"]


class EntraGatewayGuard(JwtGatewayGuard):
    """Validate and authorize an inbound Entra access token.

    Construct with the expected tenant and audience; optionally require scopes
    and/or app roles. :meth:`validate` returns the verified claims or raises.
    :meth:`authorize_and_invoke` chains validation with a thingctx invoke, so
    "validate the caller, then drive the device with its native auth" is one
    call.

    Args:
        tenant_id: the Entra tenant (GUID or verified domain). Fixes the
            accepted issuer and the JWKS source.
        audience: the API's identifier URI (``api://<app-id>``) or client id.
            A token minted for a different audience is rejected.
        required_scopes: if set, the token's ``scp`` (space-delimited delegated
            permissions) must contain every one of these.
        required_roles: if set, the token's ``roles`` (app roles / app-only
            permissions) must contain every one of these.
        require_any: with multiple required scopes/roles, accept the token if it
            has ANY of them rather than ALL (default: require all).
        jwks: an explicit JWKS dict ``{"keys": [...]}`` to verify against,
            instead of fetching from Entra. For offline / test setups; when set,
            no network call is made.
        allowed_algorithms: the signing algorithms accepted. Entra v2 access
            tokens are RS256; the default locks to that so a ``none``/HS
            downgrade is impossible.
        leeway: clock-skew allowance in seconds for exp/nbf (default 60).
    """

    provider = "entra"

    def __init__(
        self,
        *,
        tenant_id: str,
        audience: str,
        required_scopes: list[str] | tuple[str, ...] | None = None,
        required_roles: list[str] | tuple[str, ...] | None = None,
        require_any: bool = False,
        jwks: dict | None = None,
        allowed_algorithms: tuple[str, ...] = ("RS256",),
        leeway: float = 60.0,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if not audience:
            raise ValueError("audience is required")
        self.tenant_id = str(tenant_id)
        self.required_scopes = tuple(required_scopes or ())
        self.required_roles = tuple(required_roles or ())
        self.require_any = bool(require_any)

        # A v2 access token's issuer; v1 tokens use the sts.windows.net form.
        # We accept both so an app registered for either token version works.
        issuers = (
            f"https://login.microsoftonline.com/{self.tenant_id}/v2.0",
            f"https://sts.windows.net/{self.tenant_id}/",
        )
        jwks_url = f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"

        grants: list[Grant] = []
        if self.required_scopes:
            grants.append(
                Grant(
                    claim="scp",
                    values=self.required_scopes,
                    space_delimited=True,  # Entra scp is a space-delimited string
                    require_any=self.require_any,
                    kind="scope",
                )
            )
        if self.required_roles:
            grants.append(
                Grant(
                    claim="roles",
                    values=self.required_roles,
                    space_delimited=False,  # roles is a JSON list
                    require_any=self.require_any,
                    kind="role",
                )
            )

        super().__init__(
            issuers=issuers,
            audience=audience,
            grants=tuple(grants),
            jwks_url=jwks_url,
            jwks=jwks,
            allowed_algorithms=allowed_algorithms,
            leeway=leeway,
            # Confirm the tenant id is actually in the decoded issuer, so a token
            # from another tenant that somehow shares a key can never pass.
            issuer_must_contain=self.tenant_id,
        )


def make_entra_guard() -> type[EntraGatewayGuard]:
    """Zero-arg factory for the ``thingctx.guards`` entry point."""
    return EntraGatewayGuard
