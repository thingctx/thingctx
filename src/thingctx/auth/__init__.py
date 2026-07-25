# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Transport-neutral authentication for thingctx.

The layer is split in two so auth never leaks into a transport:

* **Providers** (``providers``) resolve a security scheme + runtime secret into
  neutral :class:`Credential` material and know nothing about HTTP/MQTT/etc.
* **Appliers** (``http``, ``mqtt``, ...) map that neutral material onto one
  protocol. A new transport is one more applier; a new auth method is one more
  provider.

``resolve_credentials`` is the single primitive every binding shares to turn an
owner's declared security into :class:`Credential` material. A scheme is only
*named*; the secret is supplied at runtime, keyed by owner id / slug / scheme
name, and never lives in the description document.

Custom auth: write a provider (subclass :class:`BaseAuth` for no-op defaults)
whose ``resolve`` returns a built-in :class:`Credential` (works on every
transport) or a :class:`RequestSigner` (transport-specific signing), and register
it via :func:`register_auth` or ``HttpBinding(extra_auth=[...])``.
"""

from __future__ import annotations

from thingctx.auth.context import AuthContext
from thingctx.auth.credentials import (
    ApiKeyCredential,
    BasicCredential,
    BearerToken,
    ClientCertificate,
    Credential,
    EnhancedAuth,
    RequestSigner,
    Secret,
    SignatureCredential,
)
from thingctx.auth.http import HttpAuthPlan, apply_http, register_signer
from thingctx.auth.media import (
    MediaAuthPlan,
    apply_media,
    av_auth_options,
    redact_url,
    ytdlp_auth_options,
)
from thingctx.auth.mqtt import MqttAuthPlan, apply_mqtt
from thingctx.auth.oauth_consent import authorize_code_flow, login
from thingctx.auth.providers import (
    ApiKeyAuth,
    AuthStrategy,
    AwsSigV4Auth,
    BaseAuth,
    BasicAuth,
    CredentialProvider,
    DirectCredentialAuth,
    NoSecAuth,
    OAuth2AuthorizationCodeAuth,
    OAuth2ClientCredentialsAuth,
    OAuth2JwtBearerAuth,
    StaticBearerAuth,
)
from thingctx.auth.registry import DEFAULT_AUTH, AuthRegistry, discover_auth, register_auth
from thingctx.auth.resolve import resolve_credentials
from thingctx.auth.sigv4 import _aws_region_service, sigv4_sign
from thingctx.auth.store import (
    FileTokenStore,
    MemoryTokenStore,
    TokenStore,
    default_token_store,
    token_key,
)

__all__ = [
    # Context + registry
    "AuthContext",
    "AuthRegistry",
    "DEFAULT_AUTH",
    "register_auth",
    "discover_auth",
    "resolve_credentials",
    # Providers
    "CredentialProvider",
    "AuthStrategy",  # back-compat alias of CredentialProvider
    "DirectCredentialAuth",
    "NoSecAuth",
    "StaticBearerAuth",
    "BasicAuth",
    "ApiKeyAuth",
    "OAuth2ClientCredentialsAuth",
    "OAuth2JwtBearerAuth",
    "OAuth2AuthorizationCodeAuth",
    "AwsSigV4Auth",
    "BaseAuth",
    # User-consent OAuth (authorization-code + refresh)
    "authorize_code_flow",
    "login",
    "TokenStore",
    "MemoryTokenStore",
    "FileTokenStore",
    "token_key",
    "default_token_store",
    # Neutral credential material
    "Credential",
    "Secret",
    "BearerToken",
    "BasicCredential",
    "ApiKeyCredential",
    "SignatureCredential",
    "ClientCertificate",
    "EnhancedAuth",
    "RequestSigner",
    # Transport appliers
    "apply_http",
    "HttpAuthPlan",
    "register_signer",
    "apply_mqtt",
    "MqttAuthPlan",
    "apply_media",
    "MediaAuthPlan",
    "av_auth_options",
    "ytdlp_auth_options",
    "redact_url",
    # AWS primitive
    "sigv4_sign",
    "_aws_region_service",
]
