"""Transport-neutral authentication for thingctx.

The layer is split cleanly in two so auth never leaks into a transport:

* **Providers** (``providers``) resolve a security scheme + runtime secret
  into neutral :class:`Credential` material -- a bearer token, a username/
  password, an API key, AWS signing material, a client certificate, enhanced
  auth, or a custom request signer. Token-minting (OAuth2, JWT-bearer) lives
  here. Providers know nothing about HTTP/MQTT/etc.
* **Appliers** (``http``, ``mqtt``, ...) map that neutral material onto one
  protocol. Adding a transport is one more applier file; existing providers are
  reused unchanged. Adding an auth method is one more provider; existing
  appliers decide whether/how to apply it.

``resolve_credentials`` is the single primitive every invoker shares to turn an
owner's declared security into :class:`Credential` material. A scheme is only
*named*; the secret is supplied at runtime, keyed by owner id / slug / scheme
name, and never lives in the description document.

Custom auth: register a provider whose ``resolve`` returns a built-in
:class:`Credential` (works on every transport) or a :class:`RequestSigner`
(transport-specific signing), via :func:`register_auth` or
``HttpInvoker(extra_auth=[...])``.
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
from thingctx.auth.mqtt import MqttAuthPlan, apply_mqtt
from thingctx.auth.providers import (
    ApiKeyAuth,
    AuthStrategy,
    AwsSigV4Auth,
    BasicAuth,
    CredentialProvider,
    DirectCredentialAuth,
    NoSecAuth,
    OAuth2ClientCredentialsAuth,
    OAuth2JwtBearerAuth,
    StaticBearerAuth,
    _BaseAuth,
)
from thingctx.auth.registry import DEFAULT_AUTH, AuthRegistry, register_auth
from thingctx.auth.resolve import resolve_credentials
from thingctx.auth.sigv4 import _aws_region_service, sigv4_sign

__all__ = [
    # Context + registry
    "AuthContext",
    "AuthRegistry",
    "DEFAULT_AUTH",
    "register_auth",
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
    "AwsSigV4Auth",
    "_BaseAuth",
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
    # AWS primitive
    "sigv4_sign",
    "_aws_region_service",
]
