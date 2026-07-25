# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Cloudflare Access provider for the pluggable gateway guard.

:class:`CloudflareAccessGuard` subclasses
:class:`~thingctx.identity.jwt_guard.JwtGatewayGuard`. The base does the real work
(JWKS fetch, RS256, iss/aud/exp, non-leaking errors); this class supplies the
issuer (``https://<team>.cloudflareaccess.com``), the certs URL, and the grant.

Token facts (RS256 JWTs): ``aud`` is an array that must contain the Access app's
AUD tag; a user token carries ``email`` / ``sub``; a service token (the agent
equivalent) carries ``common_name`` and an empty ``sub``.

Cloudflare authorizes at its edge, so the floor is "a valid token for this AUD
means the policy let you in", coarser than an Entra app role. Two ways to get a
per-action grant into the decision, both through the generic grant machinery:
``service_token_permissions=`` maps each ``common_name`` to its permissions (this
lives in gateway config, because Cloudflare will not put a permission list in a
service-token JWT); or ``permission_claim=`` reads a custom claim an upstream IdP
stamps, like Entra's ``roles``. With neither, the guard is authentication-only.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from thingctx.identity.jwt_guard import AuthorizationError, Grant, JwtGatewayGuard

__all__ = ["AuthorizationError", "CloudflareAccessGuard", "make_cloudflare_guard"]


class CloudflareAccessGuard(JwtGatewayGuard):
    """Validate and authorize an inbound Cloudflare Access application token.

    Construct with the team domain and the Access app's AUD tag; optionally attach
    a per-action grant (see the module docstring). :meth:`validate` returns the
    verified claims or raises.

    Args:
        team_domain: the Zero Trust team name (``"myteam"``), which fixes the
            issuer and certs URL. A full ``https://...`` host is also accepted.
        audience: the Access app's AUD tag. It lives in the token's ``aud`` array;
            a token for another app is rejected.
        required_permissions: if set, the caller's effective permissions must
            contain all of these (or any, with ``require_any``). Effective
            permissions come from ``permission_claim``, else from
            ``service_token_permissions`` keyed by ``common_name``.
        permission_claim: the token claim carrying the caller's permissions (a
            custom claim an upstream IdP stamps). Read like Entra's ``roles``.
        service_token_permissions: ``{common_name: [permissions]}``, used when
            ``permission_claim`` is not set, because Cloudflare will not embed a
            permission list in the token.
        require_any: accept the token with any of ``required_permissions`` rather
            than all (default: all).
        jwks: an explicit JWKS to verify against instead of fetching. For
            offline / test setups.
        allowed_algorithms: accepted signing algorithms. Locked to RS256 so a
            ``none``/HS downgrade is impossible.
        leeway: clock-skew allowance in seconds for exp/nbf (default 60).
    """

    provider = "cloudflare"

    def __init__(
        self,
        *,
        team_domain: str,
        audience: str,
        required_permissions: list[str] | tuple[str, ...] | None = None,
        permission_claim: str | None = None,
        service_token_permissions: dict[str, list[str]] | None = None,
        require_any: bool = False,
        jwks: dict | None = None,
        allowed_algorithms: tuple[str, ...] = ("RS256",),
        leeway: float = 60.0,
    ) -> None:
        if not team_domain:
            raise ValueError("team_domain is required")
        if not audience:
            raise ValueError("audience is required")
        self.team = self._team_name(team_domain)
        self.required_permissions = tuple(required_permissions or ())
        self.permission_claim = permission_claim
        self.service_token_permissions = dict(service_token_permissions or {})
        self.require_any = bool(require_any)

        if self.required_permissions and not (
            self.permission_claim or self.service_token_permissions
        ):
            raise ValueError(
                "required_permissions needs a source: set permission_claim "
                "(a custom claim the token carries) or service_token_permissions "
                "(a common_name -> permissions map). Cloudflare Access does not "
                "put app-role-style permissions in the token by itself."
            )

        issuer = f"https://{self.team}.cloudflareaccess.com"
        jwks_url = f"{issuer}/cdn-cgi/access/certs"

        grants: list[Grant] = []
        if self.required_permissions and self.permission_claim:
            # A custom claim the upstream IdP stamps: read it directly, exactly
            # like Entra's roles. Cloudflare puts a scalar or a list here.
            grants.append(
                Grant(
                    claim=self.permission_claim,
                    values=self.required_permissions,
                    space_delimited=False,
                    require_any=self.require_any,
                    kind="permission",
                )
            )

        super().__init__(
            issuers=(issuer,),
            audience=audience,
            grants=tuple(grants),
            jwks_url=jwks_url,
            jwks=jwks,
            allowed_algorithms=allowed_algorithms,
            leeway=leeway,
            # The issuer already encodes the team; pin it belt-and-braces so a
            # token from another team that shares a key can never pass.
            issuer_must_contain=f"{self.team}.cloudflareaccess.com",
        )

    @staticmethod
    def _team_name(team_domain: str) -> str:
        """Reduce ``"myteam"`` or ``"https://myteam.cloudflareaccess.com"`` to
        the bare team name."""
        t = str(team_domain).strip()
        if "://" in t:
            host = urlparse(t).hostname or ""
            t = host
        suffix = ".cloudflareaccess.com"
        t = t.removesuffix(suffix)
        return t.strip("/")

    def _authorize(self, claims: dict[str, Any]) -> None:
        """Enforce the per-action grant.

        For the service-token mapping (no custom claim), the caller's permissions
        are derived from ``common_name`` against the configured map, then checked.
        For a custom claim, the base's grant check (set up in __init__) handles it.
        """
        if self.required_permissions and not self.permission_claim:
            common_name = str(claims.get("common_name", "") or "")
            have = set(self.service_token_permissions.get(common_name, ()))
            need = set(self.required_permissions)
            ok = bool(have & need) if self.require_any else need.issubset(have)
            if not ok:
                mode = "any of" if self.require_any else "all of"
                raise AuthorizationError(
                    f"token is missing a required permission: needs {mode} "
                    f"{sorted(need)}, has {sorted(have)}"
                )
        # Any configured Grant objects (the custom-claim path) still run.
        super()._authorize(claims)


def make_cloudflare_guard() -> type[CloudflareAccessGuard]:
    """Zero-arg factory for the ``thingctx.guards`` entry point."""
    return CloudflareAccessGuard
