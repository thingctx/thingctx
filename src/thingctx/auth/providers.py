"""Credential providers: resolve a security scheme + runtime secret into
neutral :class:`~thingctx.auth.credentials.Credential` material.

A provider knows *one* kind of scheme. It never touches a request or a
connection -- it only produces material. Token-minting providers (OAuth2
client-credentials, JWT-bearer) call their IdP over HTTPS and return a
:class:`BearerToken`; the AWS provider returns signing material. How that
material is attached is the transport applier's job, not the provider's.

Providers are looked up in an :class:`AuthRegistry`; the first whose
``matches(scheme, credential)`` returns true wins, so a user-registered provider
can override a built-in or teach thingctx a brand-new scheme.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Protocol, runtime_checkable

from thingctx.auth.context import AuthContext
from thingctx.auth.credentials import (
    ApiKeyCredential,
    BasicCredential,
    BearerToken,
    Credential,
    RequestSigner,
    SignatureCredential,
)
from thingctx.auth.sigv4 import _AWS_SCHEMES, _aws_creds

__all__ = [
    "CredentialProvider",
    "AuthStrategy",
    "DirectCredentialAuth",
    "NoSecAuth",
    "StaticBearerAuth",
    "BasicAuth",
    "ApiKeyAuth",
    "OAuth2ClientCredentialsAuth",
    "OAuth2JwtBearerAuth",
    "AwsSigV4Auth",
    "RequestSigner",
]


@runtime_checkable
class CredentialProvider(Protocol):
    """Resolve one kind of security scheme into neutral credential material."""

    name: str

    def matches(self, scheme: Any, credential: Any) -> bool:
        """True if this provider handles ``scheme`` given ``credential``."""
        ...

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        """Return the credential material for this scheme, or ``None``."""
        ...


class _BaseAuth:
    """Default no-op provider so a concrete one implements only what it needs."""

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


class DirectCredentialAuth(_BaseAuth):
    """Pass through credential material the caller already built.

    If the runtime secret is itself a :class:`Credential` (a ``ClientCertificate``
    for mutual TLS, a pre-minted ``BearerToken``, ...), use it as-is for whatever
    scheme the owner declares. This is the seam for transport-level material that
    no security scheme names -- notably mTLS, which is reused across HTTPS, MQTT,
    OPC-UA and any other TLS transport."""

    name = "direct"

    def matches(self, scheme: Any, credential: Any) -> bool:
        return isinstance(credential, Credential)

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        return ctx.credential


class NoSecAuth(_BaseAuth):
    name = "nosec"

    def matches(self, scheme: Any, credential: Any) -> bool:
        return getattr(scheme, "scheme", None) == "nosec"


class StaticBearerAuth(_BaseAuth):
    name = "bearer"

    def matches(self, scheme: Any, credential: Any) -> bool:
        return getattr(scheme, "scheme", None) == "bearer"

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        cred = ctx.credential
        token = cred.get("access_token") if isinstance(cred, dict) else cred
        if not token:
            return None
        return BearerToken(token=str(token))


class BasicAuth(_BaseAuth):
    name = "basic"

    def matches(self, scheme: Any, credential: Any) -> bool:
        return getattr(scheme, "scheme", None) == "basic"

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        cred = ctx.credential
        if isinstance(cred, (tuple, list)) and len(cred) == 2:
            return BasicCredential(username=str(cred[0]), password=str(cred[1]))
        if isinstance(cred, dict):
            return BasicCredential(
                username=str(cred.get("username", "")),
                password=str(cred.get("password", "")),
            )
        raw = str(cred)
        user, _, pw = raw.partition(":")
        return BasicCredential(username=user, password=pw)


class ApiKeyAuth(_BaseAuth):
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
        return ApiKeyCredential(name=name, value=str(secret), location=location)


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


def _cache_get(cache: dict, key: tuple) -> str | None:
    hit = cache.get(key)
    if hit and hit[1] - 60 > time.monotonic():  # 60s safety margin
        return hit[0]
    return None


def _cache_put(cache: dict, key: tuple, token: str, expires_in: Any) -> None:
    try:
        ttl = float(expires_in)
    except (TypeError, ValueError):
        ttl = 3600.0
    cache[key] = (token, time.monotonic() + ttl)


class OAuth2ClientCredentialsAuth(_BaseAuth):
    """OAuth2 ``client_credentials`` with a shared secret.

    Sends the secret as HTTP Basic, falling back to a form field if the endpoint
    rejects it. A static ``{"access_token": ...}`` is used as-is. Returns a
    :class:`BearerToken`.
    """

    name = "oauth2-client-credentials"

    def matches(self, scheme: Any, credential: Any) -> bool:
        if getattr(scheme, "scheme", None) != "oauth2":
            return False
        if isinstance(credential, dict) and credential.get("private_key"):
            return False  # that is a JWT-bearer credential
        return True

    @staticmethod
    def _creds(cred: Any) -> tuple[str | None, str | None]:
        if isinstance(cred, dict):
            return cred.get("client_id"), cred.get("client_secret")
        if isinstance(cred, (tuple, list)) and len(cred) == 2:
            return cred[0], cred[1]
        if isinstance(cred, str) and ":" in cred:
            cid, sec = cred.split(":", 1)
            return cid, sec
        return (cred if isinstance(cred, str) else None), None

    @staticmethod
    def _token_request(method: str, cid, secret, grant, scopes):
        data = {"grant_type": grant}
        if scopes:
            data["scope"] = " ".join(scopes)
        headers: dict = {}
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
            return BearerToken(token=cred["access_token"])
        token_url = getattr(scheme, "token", "")
        if isinstance(cred, str) and not token_url:
            return BearerToken(token=cred)  # already-issued bearer

        cid, secret = self._creds(cred)
        if not token_url or cid is None:
            return None
        scopes = tuple(getattr(scheme, "scopes", ()) or ())
        key = ("cc", ctx.owner_id or scheme.name, token_url, scopes)
        cached = _cache_get(ctx.cache, key)
        if cached:
            return BearerToken(token=cached)

        _guard_tls(token_url, ctx.allow_insecure_oauth)
        grant = getattr(scheme, "flow", "") or "client_credentials"
        methods_key = ("cc-method", token_url)
        if secret is None:
            methods = ["post"]
        else:
            methods = ctx.cache.get(methods_key) or ["basic", "post"]

        import httpx

        tok = None
        async with httpx.AsyncClient(timeout=ctx.timeout) as client:
            for i, method in enumerate(methods):
                data, headers = self._token_request(method, cid, secret, grant, scopes)
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
        return BearerToken(token=access)


class OAuth2JwtBearerAuth(_BaseAuth):
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
        token_url = cred.get("token_uri") or getattr(scheme, "token", "") or (
            "https://oauth2.googleapis.com/token"
        )
        scopes = tuple(cred.get("scopes") or getattr(scheme, "scopes", ()) or ())
        iss = cred.get("client_email") or cred.get("iss") or cred.get("client_id")
        key = ("jwt", ctx.owner_id or scheme.name, token_url, scopes)
        cached = _cache_get(ctx.cache, key)
        if cached:
            return BearerToken(token=cached)

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
        return BearerToken(token=access)


# --------------------------------------------------------------------------- #
# AWS SigV4 (signing material; the signing itself is HTTP-specific)
# --------------------------------------------------------------------------- #


class AwsSigV4Auth(_BaseAuth):
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
            algorithm="aws-sigv4", key_id=ak, secret_key=sk, token=st, params=params
        )
