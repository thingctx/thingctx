# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral inbound JWT validation for a thingctx gateway.

:class:`JwtGatewayGuard` validates an incoming bearer JWT and authorizes the
caller, so thingctx can sit in front of devices that do not speak the provider's
protocol. Inbound identity only: the device is still invoked with its own outbound
credential (bearer / basic / apikey), not the caller's.

A concrete provider (Entra, Cloudflare Access) supplies the accepted ``issuers``,
the signing keys (a ``jwks_url`` to fetch and cache, or a static ``jwks``), and
the authorization ``grants``. Validation then fetches the JWKS, picks the key by
the token's ``kid``, verifies the RS256 signature (never disabled) plus ``iss`` /
``aud`` / ``exp`` / ``iat`` / ``nbf``, and enforces the grants. ``iat`` is
required and rejected when it is in the future beyond the clock-skew leeway; a
token minted "in the future" is not honored early.

Any failure raises :class:`AuthorizationError` with a reason for the gateway's
logs. The reason is not returned to the caller unless the gateway chooses to.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

__all__ = ["AuthorizationError", "Grant", "JwtGatewayGuard"]

_JWKS_TTL = 3600.0  # cache the provider's signing keys for an hour
_JWKS_FORCE_COOLDOWN = 30.0  # min seconds between forced refetches (rotation), a DoS guard


class AuthorizationError(Exception):
    """Raised when an inbound token fails validation or authorization.

    One flat type on purpose: the caller gets the same answer whether the
    signature, the expiry, or a grant failed. The ``reason`` says which, for the
    log, not the attacker (unless the gateway surfaces it)."""


@dataclass(frozen=True)
class Grant:
    """One authorization requirement checked against a claim.

    A provider maps its permission model onto this: Entra scopes in the
    space-delimited ``scp`` string, Entra app roles in the ``roles`` list, a
    Cloudflare ``common_name`` wherever the Access policy puts it. The guard reads
    ``claim`` and requires the configured ``values``.

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

    Concrete providers subclass this; the subclass constructor turns "a tenant
    plus an audience" into concrete issuers and a JWKS URL. Everything below the
    constructor is shared and stays provider-agnostic.

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

        A fetch failure raises AuthorizationError (fail-closed), never falls
        through, and never leaks the underlying httpx error type."""
        if self._static_jwks is not None:
            return self._static_jwks
        fresh = (
            self._jwks_cache is not None
            and not force
            and (time.time() - self._jwks_fetched_at) < _JWKS_TTL
        )
        if fresh:
            return self._jwks_cache  # type: ignore[return-value]
        # optional dep, kept local so the core imports without the extra
        import httpx  # noqa: PLC0415

        # Reached only when _static_jwks is None; the constructor then requires
        # jwks_url, so it is set here. Guard fail-closed rather than trust it.
        url = self._jwks_url
        if url is None:  # pragma: no cover - constructor invariant
            raise AuthorizationError("no signing-key source configured")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                jwks = resp.json()
        except Exception as exc:
            raise AuthorizationError(
                "could not fetch the signing keys to verify the token"
            ) from exc
        # A 2xx says nothing about the shape. Check before caching: a bad shape
        # wedges the cache for the whole TTL, and every later lookup then raises
        # from inside _signing_key, past this guard's fail-closed boundary, which
        # surfaces as a 500 rather than the 401 it promises. Both levels matter,
        # because a str "keys" iterates into characters before it fails.
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise AuthorizationError(
                "the signing key endpoint did not return a JWKS object with a key list"
            )
        self._jwks_cache = jwks
        self._jwks_fetched_at = time.time()
        return jwks

    def _signing_key(self, jwks: dict[str, Any], kid: str | None) -> Any:
        """Build a public key for the token's ``kid`` from the JWKS, or raise."""
        # optional dep, kept local so the core imports without the extra
        import jwt  # noqa: PLC0415

        keys = (jwks or {}).get("keys") or []
        if kid is not None:
            for jwk in keys:
                if jwk.get("kid") == kid:
                    return jwt.PyJWK.from_dict(jwk).key
        # Fall back to a lone key only when the token carries no kid. A token that
        # names a kid absent from the set is a real miss: raise so the caller
        # refetches and picks up a rotated key, instead of trusting the stale
        # cached one (which would lock out every new-kid token for the cache TTL).
        if kid is None and len(keys) == 1:
            return jwt.PyJWK.from_dict(keys[0]).key
        raise AuthorizationError(f"no signing key in the JWKS matches the token kid {kid!r}")

    # -- validation ------------------------------------------------------ #

    async def validate(self, token: str) -> dict[str, Any]:
        """Verify and authorize ``token``; return its claims or raise.

        Enforces the RS256 signature (never skipped), issuer, audience,
        expiry/not-before, and the configured grants.
        """
        # optional dep, kept local so the core imports without the extra
        import jwt  # noqa: PLC0415

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
            # A kid miss can mean the provider rotated keys since we cached them,
            # so refetch once and retry (when not static). Rate-limit the forced
            # refetch: without the cooldown, a flood of tokens with random kids
            # amplifies into unbounded outbound calls. A real rotation still
            # refreshes within the window.
            now = time.time()
            if self._static_jwks is None and (now - self._jwks_forced_at) >= _JWKS_FORCE_COOLDOWN:
                self._jwks_forced_at = now
                jwks = await self._get_jwks(force=True)
                key = self._signing_key(jwks, header.get("kid"))
            else:
                raise

        # jwt.decode verifies signature (mandatory), aud, exp, nbf, iat and raises
        # a specific InvalidTokenError subclass on failure. verify_signature stays
        # on; we never disable it. iat is required and verified: a token whose iat
        # is in the future (beyond the leeway) is rejected, so a token minted "in
        # the future" is not honored early, and a token carrying no iat is refused.
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
                    "verify_iat": True,
                    "require": ["exp", "iss", "aud", "iat"],
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthorizationError("token has expired") from exc
        except jwt.ImmatureSignatureError as exc:
            # pyjwt raises this for both a future nbf and a future iat; the message
            # carries "(iat)" only for the latter, so the reason stays honest.
            claim = "iat" if "iat" in str(exc) else "nbf"
            raise AuthorizationError(f"token is not yet valid ({claim})") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthorizationError(f"token audience does not match {self.audience!r}") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthorizationError("token issuer is not accepted") from exc
        except jwt.InvalidSignatureError as exc:
            raise AuthorizationError("token signature is invalid") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthorizationError(f"token rejected: {exc}") from exc

        # pyjwt with a list issuer accepts a token whose iss is any member. Confirm
        # the pinned substring (tenant id) is in the issuer, so a token from a
        # different tenant that somehow shares a key cannot pass.
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

        The device is invoked with its own native auth, never the caller's, and is
        never touched if validation fails: :meth:`validate` raises first.
        """
        claims = await self.validate(token)  # raises before any device call
        result = await client.invoke(tool, arguments or {})
        return claims, result
