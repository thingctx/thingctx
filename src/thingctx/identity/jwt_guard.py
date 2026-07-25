# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral inbound JWT validation for a thingctx gateway.

:class:`JwtGatewayGuard` validates an *incoming* bearer JWT and authorizes the
caller, so thingctx can sit in front of devices that do not speak the provider's
protocol. It is only about inbound identity; the device is invoked with the
existing outbound stack (its own bearer / basic / apikey), a different credential.

A concrete provider (Entra, Cloudflare Access, ...) supplies three things to the
constructor: ``issuers`` (accepted ``iss`` values), the signing keys (a live
``jwks_url`` to fetch and cache, or a static ``jwks`` set), and the authorization
grants (:class:`Grant` objects naming the claim and required values).

The validation runs for every provider:

* fetch the provider's signing keys (JWKS) and cache them;
* select the key by the token header ``kid``;
* verify the RS256 signature against that key (never disabled);
* verify ``iss``, ``aud``, and ``exp`` / ``nbf``;
* enforce the configured grants.

Any failure raises :class:`AuthorizationError` with a reason that names the
failure for the gateway's logs without leaking it to the caller unless the
gateway chooses to surface it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

__all__ = ["JwtGatewayGuard", "Grant", "AuthorizationError"]

_JWKS_TTL = 3600.0  # cache the provider's signing keys for an hour
_JWKS_FORCE_COOLDOWN = 30.0  # min seconds between forced refetches (rotation), a DoS guard


class AuthorizationError(Exception):
    """Raised when an inbound token fails validation or authorization.

    Deliberately a single flat error type: a caller gets the same 401/403-shaped
    answer whether the signature was wrong, the token expired, or a grant was
    missing, and the human-readable ``reason`` says which without leaking it to
    an attacker unless the gateway chooses to."""


@dataclass(frozen=True)
class Grant:
    """One authorization requirement checked against a claim.

    A provider maps its own permission model onto this: Entra delegated scopes
    live in the space-delimited ``scp`` string, Entra app roles live in the
    ``roles`` list, a Cloudflare custom claim or service-token ``common_name``
    lives wherever the Access policy puts it. The guard does not care which; it
    reads ``claim`` and requires the configured ``values``.

    Args:
        claim: the claim name that carries the grant (e.g. ``"scp"``,
            ``"roles"``, ``"common_name"``, a custom ``"permissions"`` claim).
        values: the values that must be present in the claim.
        space_delimited: if True, a string claim value is split on whitespace
            before the membership test (the shape of Entra's ``scp``). If False,
            the claim is either a list, or a single scalar treated as a
            one-element set (the shape of Cloudflare's ``common_name``).
        require_any: accept the token if the claim has ANY of ``values`` rather
            than ALL of them (default: require all).
        kind: a human label used only in the rejection message (e.g. "scope",
            "role", "permission").
    """

    claim: str
    values: tuple[str, ...]
    space_delimited: bool = False
    require_any: bool = False
    kind: str = "grant"

    def check(self, claims: dict[str, Any]) -> None:
        """Raise :class:`AuthorizationError` if ``claims`` does not satisfy this."""
        raw = claims.get(self.claim)
        if isinstance(raw, str):
            have = set(raw.split()) if self.space_delimited else {raw}
        elif raw is None:
            have = set()
        else:
            have = set(raw)
        need = set(self.values)
        ok = bool(have & need) if self.require_any else need.issubset(have)
        if not ok:
            mode = "any of" if self.require_any else "all of"
            raise AuthorizationError(
                f"token is missing a required {self.kind}: needs {mode} "
                f"{sorted(need)}, has {sorted(have)}"
            )


class JwtGatewayGuard:
    """Validate and authorize an inbound provider JWT (provider-neutral base).

    Concrete providers subclass this and supply the three provider-specific
    inputs; the subclass constructor is where "a tenant/team plus an audience"
    turns into concrete issuers and a JWKS URL. Everything below the constructor
    is shared and must stay provider-agnostic.

    Args:
        issuers: the accepted ``iss`` values. A token whose ``iss`` is not one
            of these is rejected.
        audience: the value the token's ``aud`` must contain. A token minted for
            a different audience is rejected.
        grants: authorization requirements (:class:`Grant`), all of which must
            pass. An empty tuple means "authentication only, no grant check".
        jwks_url: where to fetch the provider's signing keys, when ``jwks`` is
            not supplied. Fetched lazily and cached for an hour.
        jwks: an explicit JWKS dict ``{"keys": [...]}`` to verify against instead
            of fetching. For offline / test setups; when set, no network call is
            made.
        allowed_algorithms: the signing algorithms accepted. Every provider here
            signs with RS256; the default locks to that so a ``none`` / HS
            downgrade is impossible.
        leeway: clock-skew allowance in seconds for exp/nbf (default 60).
        issuer_must_contain: an extra substring every accepted issuer must
            contain (belt-and-braces: a tenant/team id), confirmed against the
            token's ``iss`` after decode. ``None`` disables the extra check.
    """

    def __init__(
        self,
        *,
        issuers: tuple[str, ...] | list[str],
        audience: str,
        grants: tuple[Grant, ...] | list[Grant] = (),
        jwks_url: str | None = None,
        jwks: dict | None = None,
        allowed_algorithms: tuple[str, ...] = ("RS256",),
        leeway: float = 60.0,
        issuer_must_contain: str | None = None,
    ) -> None:
        issuers = tuple(issuers)
        if not issuers:
            raise ValueError("at least one accepted issuer is required")
        if not audience:
            raise ValueError("audience is required")
        if jwks is None and not jwks_url:
            raise ValueError("either jwks_url or a static jwks is required")
        self._issuers = issuers
        self.audience = str(audience)
        self.grants = tuple(grants)
        self._jwks_url = jwks_url
        self.allowed_algorithms = tuple(allowed_algorithms)
        self.leeway = float(leeway)
        self._issuer_must_contain = issuer_must_contain

        # Static JWKS (test/offline) vs fetched-and-cached.
        self._static_jwks = jwks
        self._jwks_cache: dict | None = None
        self._jwks_fetched_at = 0.0
        self._jwks_forced_at = 0.0  # last forced refetch, for the DoS cooldown

    # -- JWKS ------------------------------------------------------------ #

    async def _get_jwks(self, *, force: bool = False) -> dict:
        """Return the provider's JWKS, from the static set or the cached fetch.

        A fetch failure raises AuthorizationError (fail-closed): the guard's
        contract is that any failure denies, never falls through. Never leaks the
        underlying httpx error type to a naive caller that might treat a raised
        httpx error differently from a denial."""
        if self._static_jwks is not None:
            return self._static_jwks
        fresh = (
            self._jwks_cache is not None
            and not force
            and (time.time() - self._jwks_fetched_at) < _JWKS_TTL
        )
        if fresh:
            return self._jwks_cache  # type: ignore[return-value]
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(self._jwks_url)
                resp.raise_for_status()
                jwks = resp.json()
        except Exception as exc:  # noqa: BLE001 - any fetch failure is a denial
            raise AuthorizationError(
                "could not fetch the signing keys to verify the token"
            ) from exc
        self._jwks_cache = jwks
        self._jwks_fetched_at = time.time()
        return self._jwks_cache  # type: ignore[return-value]

    def _signing_key(self, jwks: dict, kid: str | None):
        """Build a public key for the token's ``kid`` from the JWKS, or raise."""
        import jwt

        keys = (jwks or {}).get("keys") or []
        if kid is not None:
            for jwk in keys:
                if jwk.get("kid") == kid:
                    return jwt.PyJWK.from_dict(jwk).key
        # No kid match. Fall back to a lone key ONLY when the token carries no
        # kid, matching common single-key practice. A token that DOES name a kid
        # absent from the set is a real miss: raise, so the caller refetches the
        # JWKS and picks up a rotated key, rather than silently trusting the one
        # stale cached key (which would lock out every new-kid token for the
        # cache TTL after rotation).
        if kid is None and len(keys) == 1:
            return jwt.PyJWK.from_dict(keys[0]).key
        raise AuthorizationError(f"no signing key in the JWKS matches the token kid {kid!r}")

    # -- validation ------------------------------------------------------ #

    async def validate(self, token: str) -> dict[str, Any]:
        """Verify and authorize ``token``; return its claims or raise.

        Every check is enforced: RS256 signature against the provider JWKS key
        for the token's ``kid``, issuer, audience, expiry/not-before, and the
        configured grants. Signature verification is never skipped.
        """
        import jwt

        if not token or not isinstance(token, str):
            raise AuthorizationError("no token presented")

        # Read the header WITHOUT trusting it, only to pick the key + reject a
        # forbidden algorithm before any crypto runs.
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise AuthorizationError(f"malformed token: {exc}") from exc
        alg = header.get("alg")
        if alg not in self.allowed_algorithms:
            # Blocks the classic 'alg: none' and HS256-with-public-key downgrades.
            raise AuthorizationError(
                f"token signing algorithm {alg!r} is not allowed "
                f"(expected one of {self.allowed_algorithms})"
            )

        jwks = await self._get_jwks()
        try:
            key = self._signing_key(jwks, header.get("kid"))
        except AuthorizationError:
            # A kid miss can mean the provider rotated keys since we cached them.
            # Refetch once and retry before giving up (only when not static).
            #
            # DoS guard: an attacker can send RS256 tokens with random kids to
            # force a refetch per request. Rate-limit the forced refetch so a
            # flood of unknown kids cannot amplify into unbounded outbound calls;
            # a genuine rotation still refreshes within the cooldown window.
            now = time.time()
            if self._static_jwks is None and (now - self._jwks_forced_at) >= _JWKS_FORCE_COOLDOWN:
                self._jwks_forced_at = now
                jwks = await self._get_jwks(force=True)
                key = self._signing_key(jwks, header.get("kid"))
            else:
                raise

        # jwt.decode does the heavy lifting: signature (mandatory), aud, exp,
        # nbf, iat. It raises a specific InvalidTokenError subclass on any
        # failure. verify_signature is left ON (the default); we never disable it.
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self.allowed_algorithms),
                audience=self.audience,
                issuer=list(self._issuers),
                leeway=self.leeway,
                options={
                    "verify_signature": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "require": ["exp", "iss", "aud"],
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthorizationError("token has expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise AuthorizationError("token is not yet valid (nbf)") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthorizationError(f"token audience does not match {self.audience!r}") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthorizationError("token issuer is not accepted") from exc
        except jwt.InvalidSignatureError as exc:
            raise AuthorizationError("token signature is invalid") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthorizationError(f"token rejected: {exc}") from exc

        # Belt and braces: pyjwt with a list issuer accepts a token whose iss is
        # any member; confirm the pinned substring (tenant/team id) is actually
        # in the issuer, so a token from a different tenant/team that somehow
        # shares a key can never pass.
        if self._issuer_must_contain is not None:
            iss = str(claims.get("iss", ""))
            if self._issuer_must_contain not in iss:
                raise AuthorizationError("token issuer does not match")

        self._authorize(claims)
        return claims

    def _authorize(self, claims: dict[str, Any]) -> None:
        """Enforce every configured grant. All must pass."""
        for grant in self.grants:
            grant.check(claims)

    # -- the caller-to-device bridge ------------------------------------ #

    async def authorize_and_invoke(
        self,
        token: str,
        client: Any,
        tool: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Validate the inbound ``token``, then, only if authorized, drive the
        device via ``client.invoke(tool, arguments)``.

        The device is invoked with its own native auth (whatever ``client``'s TD
        declares), never the caller's. The device is never touched if validation
        fails: :meth:`validate` raises first, so ``invoke`` never runs.
        """
        claims = await self.validate(token)  # raises before any device call
        result = await client.invoke(tool, arguments or {})
        return claims, result
