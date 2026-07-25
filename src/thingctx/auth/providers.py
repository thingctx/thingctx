# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Credential providers: resolve a security scheme + runtime secret into
neutral :class:`~thingctx.auth.credentials.Credential` material.

A provider knows *one* kind of scheme. It never touches a request or a
connection; it only produces material. Attaching that material is the transport
applier's job.

Providers are looked up in an :class:`AuthRegistry`; the first whose
``matches(scheme, credential)`` returns true wins, so a user-registered provider
can override a built-in.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Protocol, cast, runtime_checkable

from thingctx.auth.context import AuthContext
from thingctx.auth.credentials import (
    ApiKeyCredential,
    BasicCredential,
    BearerToken,
    Credential,
    RequestSigner,
    Secret,
    SignatureCredential,
)
from thingctx.auth.sigv4 import _AWS_SCHEMES, _aws_creds
from thingctx.auth.store import TokenStore, default_token_store, token_key

__all__ = [
    "ApiKeyAuth",
    "AuthStrategy",
    "AwsSigV4Auth",
    "BaseAuth",
    "BasicAuth",
    "CredentialProvider",
    "DirectCredentialAuth",
    "NoSecAuth",
    "OAuth2AuthorizationCodeAuth",
    "OAuth2ClientCredentialsAuth",
    "OAuth2JwtBearerAuth",
    "RequestSigner",
    "StaticBearerAuth",
]


@runtime_checkable
class CredentialProvider(Protocol):
    """Resolve one kind of security scheme into neutral credential material."""

    name: str

    def matches(self, scheme: Any, credential: Any) -> bool:
        """True if this provider handles ``scheme`` given ``credential``."""

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        """Return the credential material for this scheme, or ``None``."""


class BaseAuth:
    """Optional base for a custom credential provider: supplies no-op
    ``matches``/``resolve`` defaults so a concrete provider implements only what it
    needs. The contract is the :class:`CredentialProvider` protocol; inheriting
    this is convenience, not a requirement."""

    name = "base"

    def matches(self, scheme: Any, credential: Any) -> bool:  # pragma: no cover
        return False

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        return None


# Back-compat alias.
AuthStrategy = CredentialProvider


# --------------------------------------------------------------------------- #
# Static / direct providers
# --------------------------------------------------------------------------- #


class DirectCredentialAuth(BaseAuth):
    """Pass through credential material the caller already built.

    If the runtime secret is itself a :class:`Credential` (a ``ClientCertificate``
    for mutual TLS, a pre-minted ``BearerToken``, ...), use it as-is for whatever
    scheme the owner declares. This is the path for transport-level material that
    no security scheme names, notably mTLS, which is reused across HTTPS, MQTT,
    OPC-UA and any other TLS transport."""

    name = "direct"

    def matches(self, scheme: Any, credential: Any) -> bool:
        return isinstance(credential, Credential)

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        # matches() admits this provider only when the runtime secret is itself
        # a Credential, so ctx.credential (Any) is one here.
        return cast("Credential", ctx.credential)


class NoSecAuth(BaseAuth):
    name = "nosec"

    def matches(self, scheme: Any, credential: Any) -> bool:
        return getattr(scheme, "scheme", None) == "nosec"


class StaticBearerAuth(BaseAuth):
    name = "bearer"

    def matches(self, scheme: Any, credential: Any) -> bool:
        return getattr(scheme, "scheme", None) == "bearer"

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        cred = ctx.credential
        token = cred.get("access_token") if isinstance(cred, dict) else cred
        if not token:
            return None
        return BearerToken(token=Secret(str(token)))


class BasicAuth(BaseAuth):
    name = "basic"

    def matches(self, scheme: Any, credential: Any) -> bool:
        return getattr(scheme, "scheme", None) == "basic"

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        cred = ctx.credential
        if not cred:  # no secret supplied means no credential (never a "None" login)
            return None
        if isinstance(cred, tuple | list) and len(cred) == 2:
            return BasicCredential(username=Secret(str(cred[0])), password=Secret(str(cred[1])))
        if isinstance(cred, dict):
            return BasicCredential(
                username=Secret(str(cred.get("username", ""))),
                password=Secret(str(cred.get("password", ""))),
            )
        raw = str(cred)
        user, _, pw = raw.partition(":")
        return BasicCredential(username=Secret(user), password=Secret(pw))


class ApiKeyAuth(BaseAuth):
    name = "apikey"

    def matches(self, scheme: Any, credential: Any) -> bool:
        return getattr(scheme, "scheme", None) == "apikey"

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        scheme, secret = ctx.scheme, ctx.credential
        if isinstance(secret, dict):
            secret = secret.get("value") or secret.get("key") or ""
        if not secret:
            return None
        name = getattr(scheme, "key_name", "Authorization") or "Authorization"
        location = "query" if getattr(scheme, "in_", "header") == "query" else "header"
        return ApiKeyCredential(name=name, value=Secret(str(secret)), location=location)


# --------------------------------------------------------------------------- #
# OAuth2 token-minting providers
# --------------------------------------------------------------------------- #


def _guard_tls(url: str, allow_insecure: bool) -> None:
    """Refuse to send a secret to a non-https endpoint unless it is loopback
    or explicitly allowed."""
    from urllib.parse import urlparse

    u = urlparse(url)
    if u.scheme == "https" or allow_insecure:
        return
    if (u.hostname or "") in ("localhost", "127.0.0.1", "::1"):
        return
    raise ValueError(
        f"refusing to send a client secret to non-https token endpoint {url!r}; "
        f"use https, or pass allow_insecure_oauth=True to override"
    )


def _secret_fp(secret: Any) -> str:
    """A short, non-reversible fingerprint of a client secret, for a cache key.
    Keys the cached access token to the exact credential, so a rotated or revoked
    secret does not reuse a token minted under the old one. Empty when absent."""
    import hashlib

    if not secret:
        return ""
    raw = secret if isinstance(secret, bytes) else str(secret).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _cache_get(cache: dict, key: tuple) -> str | None:
    hit = cache.get(key)
    if hit and hit[1] - 60 > time.monotonic():  # 60s safety margin
        # _cache_put stores (token: str, expiry: float); hit[0] is the token.
        return cast("str", hit[0])
    return None


def _cache_put(cache: dict, key: tuple, token: str, expires_in: Any) -> None:
    try:
        ttl = float(expires_in)
    except (TypeError, ValueError):
        ttl = 3600.0
    cache[key] = (token, time.monotonic() + ttl)


async def _refresh_grant(
    token_url: str,
    client_id: str | None,
    client_secret: str | None,
    refresh_token: str,
    scopes: tuple[str, ...] = (),
    *,
    timeout: float = 30.0,
) -> dict:
    """Exchange a refresh token for a fresh access token (RFC 6749 section 6).
    Reusable beyond the authorization-code provider; the caller guards TLS and
    persists any rotated refresh token."""
    import httpx

    data: dict[str, str] = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if client_id:
        data["client_id"] = client_id
    if client_secret:
        data["client_secret"] = client_secret
    if scopes:
        data["scope"] = " ".join(scopes)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(token_url, data=data)
        resp.raise_for_status()
        return cast("dict", resp.json())


class OAuth2ClientCredentialsAuth(BaseAuth):
    """OAuth2 ``client_credentials`` and ``password`` grants with a shared secret.

    Sends the secret as HTTP Basic, falling back to a form field if the endpoint
    rejects it. For the ``password`` grant the resource-owner ``username`` and
    ``password`` from the credential are added to the token request. A static
    ``{"access_token": ...}`` is used as-is. Returns a :class:`BearerToken`.
    """

    name = "oauth2-client-credentials"

    def matches(self, scheme: Any, credential: Any) -> bool:
        if getattr(scheme, "scheme", None) != "oauth2":
            return False
        # A private_key routes to JWT-bearer, not client-credentials.
        return not (isinstance(credential, dict) and credential.get("private_key"))

    @staticmethod
    def _creds(cred: Any) -> tuple[str | None, str | None]:
        if isinstance(cred, dict):
            return cred.get("client_id"), cred.get("client_secret")
        if isinstance(cred, tuple | list) and len(cred) == 2:
            return cred[0], cred[1]
        if isinstance(cred, str) and ":" in cred:
            cid, sec = cred.split(":", 1)
            return cid, sec
        return (cred if isinstance(cred, str) else None), None

    @staticmethod
    def _token_request(
        method: str,
        cid: str | None,
        secret: str | None,
        grant: str,
        scopes: tuple[str, ...],
        owner: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        data: dict[str, Any] = {"grant_type": grant}
        if scopes:
            data["scope"] = " ".join(scopes)
        # The password grant carries the resource owner's credentials alongside
        # the client's.
        if grant == "password" and owner:
            if owner.get("username") is not None:
                data["username"] = owner["username"]
            if owner.get("password") is not None:
                data["password"] = owner["password"]
        headers: dict[str, str] = {}
        if method == "basic" and secret is not None:
            raw = f"{cid}:{secret}".encode()
            headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
            data["client_id"] = cid
        else:
            data["client_id"] = cid
            if secret is not None:
                data["client_secret"] = secret
        return data, headers

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        cred, scheme = ctx.credential, ctx.scheme
        if isinstance(cred, dict) and cred.get("access_token"):
            return BearerToken(token=Secret(cred["access_token"]))
        token_url = getattr(scheme, "token", "")
        if isinstance(cred, str) and not token_url:
            return BearerToken(token=Secret(cred))  # already-issued bearer

        cid, secret = self._creds(cred)
        if not token_url or cid is None:
            return None
        scopes = tuple(sorted(getattr(scheme, "scopes", ()) or ()))
        key = ("cc", ctx.owner_id or scheme.name, token_url, scopes, cid, _secret_fp(secret))
        cached = _cache_get(ctx.cache, key)
        if cached:
            return BearerToken(token=Secret(cached))

        _guard_tls(token_url, ctx.allow_insecure_oauth)
        grant = getattr(scheme, "flow", "") or "client_credentials"
        owner = None
        if grant == "password" and isinstance(cred, dict):
            owner = {"username": cred.get("username"), "password": cred.get("password")}
        methods_key = ("cc-method", token_url)
        methods = ["post"] if secret is None else ctx.cache.get(methods_key) or ["basic", "post"]

        import httpx

        tok = None
        async with httpx.AsyncClient(timeout=ctx.timeout) as client:
            for i, method in enumerate(methods):
                data, headers = self._token_request(method, cid, secret, grant, scopes, owner)
                resp = await client.post(token_url, data=data, headers=headers)
                if resp.status_code in (400, 401) and i < len(methods) - 1:
                    continue
                resp.raise_for_status()
                tok = resp.json()
                ctx.cache[methods_key] = [method]
                break

        access = (tok or {}).get("access_token")
        if not access:
            return None
        _cache_put(ctx.cache, key, access, (tok or {}).get("expires_in", 3600))
        return BearerToken(token=Secret(access))


class OAuth2JwtBearerAuth(BaseAuth):
    """OAuth2 JWT-bearer assertion grant (RFC 7523).

    The client proves itself by signing a short-lived JWT with its private key
    (RS256) and exchanging it for an access token. The credential is a
    service-account-style dict (``client_email`` + ``private_key`` +
    ``token_uri``). Returns a :class:`BearerToken`. Needs ``pyjwt[crypto]`` (the
    ``cloud`` extra).
    """

    name = "oauth2-jwt-bearer"
    _GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

    def matches(self, scheme: Any, credential: Any) -> bool:
        return (
            getattr(scheme, "scheme", None) == "oauth2"
            and isinstance(credential, dict)
            and bool(credential.get("private_key"))
        )

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        cred, scheme = ctx.credential, ctx.scheme
        token_url = (
            cred.get("token_uri")
            or getattr(scheme, "token", "")
            or ("https://oauth2.googleapis.com/token")
        )
        scopes = tuple(sorted(cred.get("scopes") or getattr(scheme, "scopes", ()) or ()))
        iss = cred.get("client_email") or cred.get("iss") or cred.get("client_id")
        fp = _secret_fp(cred.get("private_key"))
        key = ("jwt", ctx.owner_id or scheme.name, token_url, scopes, iss, fp)
        cached = _cache_get(ctx.cache, key)
        if cached:
            return BearerToken(token=Secret(cached))

        _guard_tls(token_url, ctx.allow_insecure_oauth)
        try:
            import jwt  # PyJWT
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "OAuth2 JWT-bearer needs PyJWT with crypto: pip install 'thingctx[cloud]'"
            ) from e

        now = int(time.time())
        claims = {
            "iss": iss,
            "aud": cred.get("audience") or token_url,
            "iat": now,
            "exp": now + 3600,
        }
        if scopes:
            claims["scope"] = " ".join(scopes)
        if cred.get("subject"):  # domain-wide delegation
            claims["sub"] = cred["subject"]
        headers = {}
        if cred.get("private_key_id"):
            headers["kid"] = cred["private_key_id"]
        assertion = jwt.encode(
            claims, cred["private_key"], algorithm="RS256", headers=headers or None
        )

        import httpx

        async with httpx.AsyncClient(timeout=ctx.timeout) as client:
            resp = await client.post(
                token_url,
                data={"grant_type": self._GRANT, "assertion": assertion},
            )
            resp.raise_for_status()
            tok = resp.json()
        access = (tok or {}).get("access_token")
        if not access:
            return None
        _cache_put(ctx.cache, key, access, (tok or {}).get("expires_in", 3600))
        return BearerToken(token=Secret(access))


class OAuth2AuthorizationCodeAuth(BaseAuth):
    """OAuth2 authorization-code grant for *user*-authorized APIs: the access
    token is minted on behalf of a user, not the client itself.

    This provider only refreshes silently. The one-time browser consent that
    issues the first refresh token is an explicit, out-of-band step
    (``thingctx auth login`` or :func:`thingctx.auth.authorize_code_flow`) and is
    never reached from ``resolve`` -- a token call, which an agent can trigger,
    must never open a browser. With no stored refresh token ``resolve`` raises,
    pointing the operator at consent.

    The refresh token (a long-lived secret) is read from a :class:`TokenStore`
    so it survives restarts; the short-lived access token is cached in
    ``ctx.cache``. Matches an ``oauth2`` scheme whose ``flow`` is ``code``.
    """

    name = "oauth2-authorization-code"

    def __init__(self, *, store: TokenStore | None = None) -> None:
        self._store = store

    @property
    def store(self) -> TokenStore:
        if self._store is None:  # the shared default file store
            self._store = default_token_store()
        return self._store

    def matches(self, scheme: Any, credential: Any) -> bool:
        return getattr(scheme, "scheme", None) == "oauth2" and getattr(scheme, "flow", "") in (
            "code",
            "authorization_code",
        )

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        cred, scheme = ctx.credential, ctx.scheme
        if isinstance(cred, dict) and cred.get("access_token"):
            return BearerToken(token=Secret(cred["access_token"]))  # caller supplied one
        token_url = getattr(scheme, "token", "") or (
            cred.get("token_url") if isinstance(cred, dict) else ""
        )
        if not token_url:
            return None
        scopes = tuple(sorted(getattr(scheme, "scopes", ()) or ()))
        _cid, _sec = OAuth2ClientCredentialsAuth._creds(cred)
        _rt = cred.get("refresh_token") if isinstance(cred, dict) else None
        # Key the cached access token to the client and to a runtime-supplied
        # refresh token, so a changed credential does not reuse a stale token.
        key = ("ac", ctx.owner_id or scheme.name, token_url, scopes, _cid, _secret_fp(_rt or _sec))
        cached = _cache_get(ctx.cache, key)
        if cached:
            return BearerToken(token=Secret(cached))

        cid, secret = OAuth2ClientCredentialsAuth._creds(cred)
        skey = token_key(ctx.owner_id, token_url, scopes)
        record = self.store.get(skey) or {}
        # An explicit refresh token in the credential wins; else the one consent
        # persisted under this owner/endpoint/scope.
        refresh = (cred.get("refresh_token") if isinstance(cred, dict) else None) or record.get(
            "refresh_token"
        )
        if not refresh:
            raise RuntimeError(
                f"no stored refresh token for {skey!r}; run a one-time consent first "
                "(thingctx auth login), then this resolves silently"
            )
        # A runtime credential wins; else fall back to what consent persisted, so
        # a confidential client refreshes with no runtime credential config.
        if cid is None:
            cid = record.get("client_id")
        if secret is None:
            secret = record.get("client_secret")

        _guard_tls(token_url, ctx.allow_insecure_oauth)
        tok = await _refresh_grant(token_url, cid, secret, refresh, scopes, timeout=ctx.timeout)
        access = (tok or {}).get("access_token")
        if not access:
            return None
        _cache_put(ctx.cache, key, access, (tok or {}).get("expires_in", 3600))
        rotated = (tok or {}).get("refresh_token")
        if rotated and rotated != refresh:  # some IdPs rotate the refresh token
            self.store.set(skey, {**record, "refresh_token": rotated, "client_id": cid})
        return BearerToken(token=Secret(access))


# --------------------------------------------------------------------------- #
# AWS SigV4 (signing material; the signing itself is HTTP-specific)
# --------------------------------------------------------------------------- #


class AwsSigV4Auth(BaseAuth):
    """Recognize the AWS SigV4 scheme and produce neutral signing material.

    Returns a :class:`SignatureCredential` with ``algorithm="aws-sigv4"``; the
    HTTP applier turns it into a request signer (signing only means anything for
    an HTTP-style request). SigV4 is not a standard security scheme, so it is
    declared conformantly as ``{"scheme": "auto", "x-thingctx-auth": "aws-sigv4",
    ...}`` (a bare ``{"scheme": "aws-sigv4"}`` also matches, but won't validate).
    """

    name = "aws-sigv4"

    def matches(self, scheme: Any, credential: Any) -> bool:
        s = getattr(scheme, "scheme", None)
        if s in _AWS_SCHEMES:
            return True
        raw = getattr(scheme, "raw", {}) or {}
        return raw.get("scheme") in _AWS_SCHEMES or raw.get("x-thingctx-auth") == "aws-sigv4"

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        ak, sk, st = _aws_creds(ctx.credential)
        if not ak or not sk:
            return None
        raw = getattr(ctx.scheme, "raw", {}) or {}
        cred = ctx.credential if isinstance(ctx.credential, dict) else {}
        params = {}
        region = cred.get("region") or raw.get("region")
        service = cred.get("service") or raw.get("service")
        if region:
            params["region"] = region
        if service:
            params["service"] = service
        return SignatureCredential(
            algorithm="aws-sigv4",
            key_id=Secret(ak),
            secret_key=Secret(sk),
            token=Secret(st) if st else None,
            params=params,
        )
