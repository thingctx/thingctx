# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Outbound: authenticate the agent as an Entra identity to any target.

``EntraAuth`` is a thingctx :class:`~thingctx.CredentialProvider`. It claims an
``oauth2`` security scheme flagged as Entra, mints an access token via
``azure-identity``, and returns a :class:`~thingctx.BearerToken`.

The whole Entra credential chain is reachable through one scheme:
``DefaultAzureCredential`` by default (client secret, certificate, managed
identity, workload identity federation, Azure CLI login), or an explicit
``ClientSecretCredential`` / ``CertificateCredential`` when the TD / runtime
secret names one.

The Entra gotcha this provider hides: v2 tokens are minted against a **resource
scope** of the form ``<resource>/.default``. A caller who writes a bare
resource URI, an app id uri, or (mistakenly) Graph-style delegated scopes gets
normalized to the one ``.default`` scope Entra's client-credentials flow wants.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from thingctx.auth.context import AuthContext
from thingctx.auth.credentials import BearerToken, Credential, Secret

__all__ = ["EntraAuth", "is_entra_scheme", "make_provider", "normalize_default_scope"]

# Entra's public cloud login host. (Sovereign clouds use a different host; the
# provider claims those too when the raw marker says so, see is_entra_scheme.)
_ENTRA_HOSTS = (
    "login.microsoftonline.com",
    "login.microsoftonline.us",  # US Gov
    "login.chinacloudapi.cn",  # China (21Vianet)
    "login.microsoftonline.de",  # legacy Germany
    "sts.windows.net",
)

_DEFAULT = "/.default"


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_entra_scheme(scheme: Any) -> bool:
    """True if ``scheme`` is an oauth2 scheme that should be driven by Entra.

    Two explicit signals, either is enough:

    * the token endpoint host is a Microsoft identity host
      (``login.microsoftonline.com`` and its sovereign siblings), or
    * a raw / extension marker on the security definition:
      ``{"x-thingctx-auth": "entra"}`` or a bare ``tenant`` / ``tenantId`` /
      ``authority`` field.

    Lenient about *how* Entra is declared, strict that it must be declared: a
    generic oauth2 scheme pointing at some other IdP is left to the built-in
    OAuth2 providers.
    """
    if getattr(scheme, "scheme", None) != "oauth2":
        return False
    raw = getattr(scheme, "raw", {}) or {}
    marker = str(raw.get("x-thingctx-auth", "")).lower()
    if marker in ("entra", "azuread", "aad", "microsoft"):
        return True
    if raw.get("tenant") or raw.get("tenantId") or raw.get("tenant_id"):
        return True
    # An authority URL pointing at a Microsoft login host.
    if _host_of(str(raw.get("authority", ""))) in _ENTRA_HOSTS:
        return True
    # The token endpoint host (the most common signal).
    token = getattr(scheme, "token", "") or raw.get("token", "")
    return _host_of(str(token)) in _ENTRA_HOSTS


def normalize_default_scope(scope: str) -> str:
    """Return the ``<resource>/.default`` scope Entra's app-only flow expects.

    Handles the four shapes a caller writes:

    * ``https://graph.microsoft.com/.default`` -> unchanged (already correct);
    * ``https://graph.microsoft.com`` (a resource URI) -> append ``/.default``;
    * ``api://<app-id>`` (an app id uri) -> append ``/.default``;
    * ``https://graph.microsoft.com/User.Read`` (a delegated scope, a misconfig
      for client credentials) -> resolve the resource and use its ``.default``.

    A bare resource identifier (``https://graph.microsoft.com`` with no path)
    and an app id uri both just gain ``/.default``. A URI that already carries a
    delegated permission path is reduced to its resource and re-defaulted, which
    is the correct behavior for an app-only token.
    """
    s = (scope or "").strip()
    if not s:
        raise ValueError("an Entra scope must be a non-empty resource, got empty")
    if s.endswith(_DEFAULT):
        return s

    parsed = urlparse(s)
    if parsed.scheme and parsed.netloc:
        # A real URL (https://... or api://host/...): the resource is
        # scheme://host; any path is a delegated permission we drop for .default.
        resource = f"{parsed.scheme}://{parsed.netloc}"
        return resource + _DEFAULT
    if parsed.scheme and not parsed.netloc:
        # api://<app-guid> parses with an empty netloc (the guid lands in path);
        # keep the whole thing as the resource, minus a trailing slash.
        return s.rstrip("/") + _DEFAULT
    # A bare token with no scheme (e.g. a client-id GUID): treat it as the
    # resource identifier and default it.
    return s.rstrip("/") + _DEFAULT


class EntraAuth:
    """thingctx credential provider that mints Entra ID access tokens.

    Construction is zero-arg for the entry point (:func:`make_provider`). A
    consumer who wants a fixed credential (rather than the default chain) can
    pass one in ``credential=``; otherwise the credential is built per owner
    from the runtime secret, falling back to ``DefaultAzureCredential``.
    """

    name = "entra"

    def __init__(self, credential: Any | None = None) -> None:
        # An explicit azure-identity credential instance to reuse for every
        # owner (e.g. a single managed identity). When None, resolve() builds
        # the credential from the runtime secret / environment.
        self._credential = credential
        # Cache DefaultAzureCredential: it probes IMDS/env once and is safe to
        # share. Keyed by tenant so a multi-tenant client keeps them distinct.
        self._default_by_tenant: dict[str, Any] = {}

    # -- CredentialProvider protocol ------------------------------------- #

    def matches(self, scheme: Any, credential: Any) -> bool:
        """Claim an oauth2 scheme that is Entra-flagged; reject everything else.

        Deliberately independent of the runtime secret: an Entra scheme is
        Entra whether the secret is a client secret, a cert path, or absent
        (managed identity / CLI login supply no explicit secret)."""
        return is_entra_scheme(scheme)

    async def resolve(self, ctx: AuthContext) -> Credential | None:
        """Mint a bearer token for the owner's Entra scope, or ``None``."""
        scheme = ctx.scheme
        raw = getattr(scheme, "raw", {}) or {}
        cred = ctx.credential

        scope = self._scope_for(scheme, raw, cred)
        if scope is None:
            return None
        resource_scope = normalize_default_scope(scope)

        tenant = self._tenant_for(raw, cred)

        # Optionally short-circuit on thingctx's binding-scoped cache. This sits
        # on TOP of azure-identity's own in-memory token cache; both are fine.
        cache_key = ("entra", ctx.owner_id or getattr(scheme, "name", ""), resource_scope)
        hit = ctx.cache.get(cache_key)
        if hit is not None:
            import time

            token, exp = hit
            if exp - 60 > time.time():  # 60s safety margin, like the built-ins
                return BearerToken(token=Secret(token))

        credential = self._build_credential(tenant, cred)
        # azure-identity is sync; get_token blocks. Run it off the event loop so
        # an IMDS / network round trip does not stall the loop.
        import asyncio

        result = await asyncio.to_thread(credential.get_token, resource_scope)
        token = getattr(result, "token", None)
        if not token:
            return None

        expires_on = getattr(result, "expires_on", None)
        if expires_on:
            ctx.cache[cache_key] = (token, float(expires_on))
        return BearerToken(token=Secret(token))

    # -- scope / tenant / credential construction ------------------------ #

    @staticmethod
    def _scope_for(scheme: Any, raw: dict, cred: Any) -> str | None:
        """The resource scope to mint against, from (in order): the runtime
        secret, the scheme's declared scopes, a raw ``resource`` field."""
        if isinstance(cred, dict):
            for k in ("scope", "resource", "audience"):
                if cred.get(k):
                    return str(cred[k])
        scopes = tuple(getattr(scheme, "scopes", ()) or ())
        if scopes:
            # Entra's app-only flow takes exactly one .default scope; if several
            # are declared, take the first and default it (the rest are
            # delegated permissions that .default already covers).
            return str(scopes[0])
        for k in ("resource", "scope"):
            if raw.get(k):
                return str(raw[k])
        return None

    @staticmethod
    def _tenant_for(raw: dict, cred: Any) -> str | None:
        if isinstance(cred, dict):
            for k in ("tenant", "tenant_id", "tenantId"):
                if cred.get(k):
                    return str(cred[k])
        for k in ("tenant", "tenant_id", "tenantId"):
            if raw.get(k):
                return str(raw[k])
        authority = raw.get("authority")
        if authority:
            # https://login.microsoftonline.com/<tenant>/...
            path = urlparse(str(authority)).path.strip("/").split("/")
            if path and path[0]:
                return path[0]
        return None

    def _build_credential(self, tenant: str | None, cred: Any) -> Any:
        """Return the azure-identity credential to mint with.

        Priority: an instance the provider was constructed with; an explicit
        ClientSecretCredential / CertificateCredential when the secret names
        one; otherwise DefaultAzureCredential (the full chain)."""
        if self._credential is not None:
            return self._credential

        if isinstance(cred, dict):
            client_id = cred.get("client_id") or cred.get("clientId")
            client_secret = cred.get("client_secret") or cred.get("clientSecret")
            cert_path = cred.get("certificate_path") or cred.get("certificatePath")
            t = tenant or cred.get("tenant") or cred.get("tenant_id") or cred.get("tenantId")
            from azure.identity import (
                CertificateCredential,
                ClientSecretCredential,
            )

            if t and client_id and client_secret:
                return ClientSecretCredential(
                    tenant_id=str(t), client_id=str(client_id), client_secret=str(client_secret)
                )
            if t and client_id and cert_path:
                return CertificateCredential(
                    tenant_id=str(t), client_id=str(client_id), certificate_path=str(cert_path)
                )

        # The default chain: covers env client secret, managed identity (IMDS),
        # workload identity federation, and the developer's Azure CLI login.
        from azure.identity import DefaultAzureCredential

        key = tenant or "*"
        got = self._default_by_tenant.get(key)
        if got is None:
            got = (
                DefaultAzureCredential(additionally_allowed_tenants=["*"])
                if tenant is None
                else DefaultAzureCredential()
            )
            self._default_by_tenant[key] = got
        return got


def make_provider() -> EntraAuth:
    """Zero-arg factory for the ``thingctx.auth`` entry point."""
    return EntraAuth()
