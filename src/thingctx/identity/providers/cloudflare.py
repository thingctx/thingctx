# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Cloudflare Access provider for the pluggable gateway guard.

:class:`CloudflareAccessGuard` is a thin subclass of
:class:`~thingctx.identity.jwt_guard.JwtGatewayGuard`. Everything real (JWKS fetch,
RS256 verification, iss/aud/exp checks, the non-leaking errors) lives in the
base; this class supplies only the three Cloudflare-specific things:

1. the issuer: ``https://<team_domain>.cloudflareaccess.com``;
2. the JWKS URL: ``https://<team_domain>.cloudflareaccess.com/cdn-cgi/access/certs``
   (Cloudflare publishes two keys there, current + previously rotated, so the
   base's kid match is what selects the right one);
3. the authorization grant. Cloudflare has NO equivalent of Entra app roles, so
   the honest mapping is spelled out below.

Cloudflare Access token facts (RS256 JWTs):
  * ``iss`` is ``https://<team>.cloudflareaccess.com``;
  * ``aud`` is an ARRAY containing the Access application's AUD tag (a hex
    string); the base's audience check requires the configured tag to be a
    member;
  * a USER token carries ``email`` / ``sub``;
  * a SERVICE TOKEN (the app-only / agent equivalent, a client-id/secret pair
    Cloudflare validates at its edge) carries ``common_name`` (the service
    token's name, e.g. ``"my-agent.access"``) and ``sub`` empty.

Authorization mapping (be honest, this is the load-bearing design decision):

Cloudflare Access does authorization at its EDGE via Access policies: if the
caller is allowed to reach the application, the token is minted at all. So the
FLOOR of authorization is "holding a valid token for this AUD means the policy
let you in" — coarser than Entra, where the app-role in the token names the
exact permission. To get a per-device / per-action grant (the
``Thing1.Write``-equivalent) INTO the token, a real deployment has two paths,
and this guard supports both through the generic grant machinery:

  * SERVICE-TOKEN MAPPING (``service_token_permissions=``): the guard is
    configured with a mapping from each service token's ``common_name`` to the
    permissions that token holder may exercise, and ``required_permissions=``
    names what this gateway requires. The guard synthesizes the caller's
    permissions from ``common_name`` and checks them. The mapping lives in the
    gateway config, NOT in the token, because Cloudflare will not put an
    arbitrary permission list in a service-token JWT. This is the honest
    difference from Entra: Entra's app-role IS in the token (Cloudflare's
    per-action grant is derived by the gateway from the token's identity).

  * CUSTOM-CLAIM MAPPING (``permission_claim=`` + ``required_permissions=``):
    if the deployment configures an Access policy / OIDC IdP to stamp a custom
    claim (e.g. ``"groups"`` or a bespoke ``"permissions"`` claim) into the
    token, the guard reads that claim directly, exactly like Entra's ``roles``.
    This is the closest Cloudflare gets to Entra app roles, and it requires the
    upstream IdP to emit the claim; Access's own service tokens do not.

If neither is configured, the guard is authentication-only: a valid token for
the AUD passes (the Access policy at the edge was the authorization). That is a
legitimate and common Cloudflare posture; it is just coarser than a per-action
role, and this docstring says so plainly rather than pretending otherwise.
"""

from __future__ import annotations

from typing import Any

from thingctx.identity.jwt_guard import AuthorizationError, Grant, JwtGatewayGuard

__all__ = ["CloudflareAccessGuard", "make_cloudflare_guard", "AuthorizationError"]


class CloudflareAccessGuard(JwtGatewayGuard):
    """Validate and authorize an inbound Cloudflare Access application token.

    Construct with the team domain and the Access application's AUD tag;
    optionally attach a per-action grant (see the module docstring for the two
    honest mappings). :meth:`validate` returns the verified claims or raises.

    Args:
        team_domain: the Cloudflare Zero Trust team name (``"myteam"``), which
            fixes the issuer ``https://myteam.cloudflareaccess.com`` and the
            certs URL. A full ``https://...`` host is also accepted and reduced
            to the team name.
        audience: the Access application's AUD tag (the hex string). Cloudflare
            puts it in the token's ``aud`` ARRAY; a token for another app is
            rejected.
        required_permissions: if set, the effective permissions of the caller
            must contain every one of these (or ANY, with ``require_any``). The
            effective permissions come from ``permission_claim`` if set, else
            from ``service_token_permissions`` keyed by ``common_name``.
        permission_claim: the token claim that carries the caller's permissions
            (a custom claim an upstream IdP / Access policy stamps, e.g.
            ``"groups"`` or ``"permissions"``). Read like Entra's ``roles``.
        service_token_permissions: a mapping ``{common_name: [permissions]}``
            the gateway is configured with, used when ``permission_claim`` is
            not set. The grant is derived from the service token's identity
            because Cloudflare will not embed an arbitrary permission list in the
            token.
        require_any: accept the token if it has ANY of ``required_permissions``
            rather than ALL (default: require all).
        jwks: an explicit JWKS dict ``{"keys": [...]}`` to verify against instead
            of fetching from Cloudflare. For offline / test setups; when set, no
            network call is made.
        allowed_algorithms: the signing algorithms accepted. Cloudflare Access
            tokens are RS256; the default locks to that so a ``none``/HS
            downgrade is impossible.
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
            from urllib.parse import urlparse

            host = urlparse(t).hostname or ""
            t = host
        # Strip the cloudflareaccess.com suffix if a full host was given.
        suffix = ".cloudflareaccess.com"
        if t.endswith(suffix):
            t = t[: -len(suffix)]
        return t.strip("/")

    def _authorize(self, claims: dict[str, Any]) -> None:
        """Enforce the per-action grant.

        When the permission source is a service-token mapping (no custom claim),
        the caller's effective permissions are DERIVED from the token's
        ``common_name`` against the gateway-configured map, then checked. This is
        the honest Cloudflare shape: the grant is not in the token, the gateway
        resolves it from the token's identity. When a custom claim is configured
        instead, the base's grant check (already set up in __init__) handles it.
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
    """Zero-arg factory for the ``thingctx.guards`` entry point.

    Like the Entra factory, this returns the guard CLASS (a guard needs
    per-deployment config: team domain, AUD tag), which a gateway instantiates.
    ``discover_guards`` keys it by ``provider`` (``"cloudflare"``)."""
    return CloudflareAccessGuard
