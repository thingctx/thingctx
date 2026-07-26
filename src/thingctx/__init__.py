# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Consume a WoT Thing Description and drive the Thing over any transport.

Parse a TD, present its actions as tools, and invoke each over the
transport its form names. Depends on stdlib; litellm, httpx, paho-mqtt
are optional extras.

    import thingctx
    host = await thingctx.from_url("http://device.local/.well-known/wot")
    print(await host.chat("turn on the pump and report its status"))

    host = thingctx.from_file("pump.td.json")
    host = thingctx.from_td(td_dict)

For the pure client without an LLM, build a ThingClient directly.
"""

# ThingClient: TD -> tools + invoke/read/write/observe/subscribe. No LLM.
# Transport-neutral auth: providers resolve a scheme+secret into neutral
# Credential material; per-transport appliers map it onto HTTP/MQTT/etc.
from thingctx.auth import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthContext,
    AuthRegistry,
    AuthStrategy,
    AwsSigV4Auth,
    BaseAuth,
    BasicAuth,
    BasicCredential,
    BearerToken,
    ClientCertificate,
    Credential,
    CredentialProvider,
    EnhancedAuth,
    FileTokenStore,
    HttpAuthPlan,
    MemoryTokenStore,
    MqttAuthPlan,
    OAuth2AuthorizationCodeAuth,
    OAuth2ClientCredentialsAuth,
    OAuth2JwtBearerAuth,
    RequestSigner,
    Secret,
    SignatureCredential,
    StaticBearerAuth,
    TokenStore,
    apply_http,
    apply_mqtt,
    authorize_code_flow,
    discover_auth,
    login,
    register_auth,
    register_signer,
    resolve_credentials,
    sigv4_sign,
)

# Binding contract: built-in transports (http, mqtt, media, local, exec) implement it;
# an adopter registers their own binding to replace one or add a new protocol.
from thingctx.bindings import (
    BUILTIN_BINDINGS,
    CONTRACT_VERSION,
    AsyncAction,
    AuthMixin,
    BindingRegistry,
    BulkProperties,
    Closeable,
    ContentRouted,
    ExecBinding,
    Frame,
    HttpBinding,
    LocalBinding,
    MediaBackend,
    MediaBinding,
    MediaConsumer,
    MediaPublisher,
    MqttBinding,
    ProtocolBinding,
    Readable,
    SecurityAware,
    Subscribable,
    Writable,
    binding_schemes,
    build_builtin,
    default_bindings,
    discover_bindings,
    select_binding,
)
from thingctx.chain import ChainError, run_chain
from thingctx.client import from_file, from_td, from_url
from thingctx.contracts import implements

# LLMHost: optional tool-calling loop, in thingctx.contrib.
from thingctx.contrib.llm import LLMHost
from thingctx.gateway import GatewayProjection

# Compile a non-WoT description (OpenAPI) into a TD.
from thingctx.lint import LintFinding, lint_td
from thingctx.openapi import from_openapi, load_spec
from thingctx.quality import (
    QUALITY_KEY,
    Quality,
    Verdict,
    is_suspect,
    make_quality,
    quality_of,
)
from thingctx.registry import (
    FileRegistry,
    Registry,
    TDDRegistry,
    from_arg,
    from_args,
)
from thingctx.reliability import RetryPolicy, TransportError
from thingctx.runtime import ThingClient
from thingctx.thing import (
    WoTAction,
    WoTEvent,
    WoTProperty,
    WoTSecurityScheme,
    WoTThing,
    actions_to_tools,
    parse_thing,
)
from thingctx.trust import (
    ApprovalRequest,
    Check,
    VerifyReport,
)
from thingctx.validate import TDValidationError, validate_td

__version__ = "0.2.2"

__all__ = [
    "BUILTIN_BINDINGS",
    "CONTRACT_VERSION",
    "QUALITY_KEY",
    "ApiKeyAuth",
    "ApiKeyCredential",
    "ApprovalRequest",
    "AsyncAction",
    "AuthContext",
    "AuthMixin",
    "AuthRegistry",
    "AuthStrategy",
    "AwsSigV4Auth",
    "BaseAuth",
    "BasicAuth",
    "BasicCredential",
    "BearerToken",
    "BindingRegistry",
    "BulkProperties",
    "ChainError",
    "Check",
    "ClientCertificate",
    "Closeable",
    "ContentRouted",
    "Credential",
    "CredentialProvider",
    "EnhancedAuth",
    "ExecBinding",
    "FileRegistry",
    "FileTokenStore",
    "Frame",
    "GatewayProjection",
    "HttpAuthPlan",
    "HttpBinding",
    "LLMHost",
    "LintFinding",
    "LocalBinding",
    "MediaBackend",
    "MediaBinding",
    "MediaConsumer",
    "MediaPublisher",
    "MemoryTokenStore",
    "MqttAuthPlan",
    "MqttBinding",
    "OAuth2AuthorizationCodeAuth",
    "OAuth2ClientCredentialsAuth",
    "OAuth2JwtBearerAuth",
    "ProtocolBinding",
    "Quality",
    "Readable",
    "Registry",
    "RequestSigner",
    "RetryPolicy",
    "Secret",
    "SecurityAware",
    "SignatureCredential",
    "StaticBearerAuth",
    "Subscribable",
    "TDDRegistry",
    "TDValidationError",
    "ThingClient",
    "TokenStore",
    "TransportError",
    "Verdict",
    "VerifyReport",
    "WoTAction",
    "WoTEvent",
    "WoTProperty",
    "WoTSecurityScheme",
    "WoTThing",
    "Writable",
    "actions_to_tools",
    "apply_http",
    "apply_mqtt",
    "authorize_code_flow",
    "binding_schemes",
    "build_builtin",
    "default_bindings",
    "discover_auth",
    "discover_bindings",
    "from_arg",
    "from_args",
    "from_file",
    "from_openapi",
    "from_td",
    "from_url",
    "implements",
    "is_suspect",
    "lint_td",
    "load_spec",
    "login",
    "make_quality",
    "parse_thing",
    "quality_of",
    "register_auth",
    "register_signer",
    "resolve_credentials",
    "run_chain",
    "select_binding",
    "sigv4_sign",
    "validate_td",
]
