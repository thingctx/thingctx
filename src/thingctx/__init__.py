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
    HttpAuthPlan,
    MqttAuthPlan,
    OAuth2ClientCredentialsAuth,
    OAuth2JwtBearerAuth,
    RequestSigner,
    Secret,
    SignatureCredential,
    StaticBearerAuth,
    apply_http,
    apply_mqtt,
    discover_auth,
    register_auth,
    register_signer,
    resolve_credentials,
    sigv4_sign,
)

# Binding contract: built-in transports (http, mqtt, media, local) implement it;
# an adopter registers their own binding to replace one or add a new protocol.
from thingctx.bindings import (
    BUILTIN_BINDINGS,
    CONTRACT_VERSION,
    AuthMixin,
    BindingRegistry,
    Closeable,
    ContentRouted,
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
from thingctx.client import from_file, from_td, from_url
from thingctx.contracts import implements

# LLMHost: optional tool-calling loop, in thingctx.contrib.
from thingctx.contrib.llm import LLMHost

# Compile a non-WoT description (OpenAPI) into a TD.
from thingctx.lint import LintFinding, lint_td
from thingctx.openapi import from_openapi, load_spec
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

__version__ = "0.1.3"

__all__ = [
    "from_url",
    "from_file",
    "from_td",
    "from_openapi",
    "load_spec",
    "lint_td",
    "LintFinding",
    "ThingClient",
    "LLMHost",
    "Registry",
    "FileRegistry",
    "TDDRegistry",
    "from_arg",
    "from_args",
    "WoTThing",
    "WoTAction",
    "WoTProperty",
    "WoTEvent",
    "WoTSecurityScheme",
    "validate_td",
    "TDValidationError",
    "parse_thing",
    "actions_to_tools",
    "ProtocolBinding",
    "implements",
    "BindingRegistry",
    "select_binding",
    "default_bindings",
    "discover_bindings",
    "build_builtin",
    "binding_schemes",
    "BUILTIN_BINDINGS",
    "CONTRACT_VERSION",
    "ContentRouted",
    "Readable",
    "Writable",
    "Subscribable",
    "MediaConsumer",
    "MediaPublisher",
    "SecurityAware",
    "Closeable",
    "AuthMixin",
    "HttpBinding",
    "MqttBinding",
    "LocalBinding",
    "MediaBinding",
    "MediaBackend",
    "Frame",
    "register_auth",
    "discover_auth",
    "resolve_credentials",
    "AuthStrategy",
    "CredentialProvider",
    "BaseAuth",
    "AuthRegistry",
    "AuthContext",
    "StaticBearerAuth",
    "BasicAuth",
    "ApiKeyAuth",
    "OAuth2ClientCredentialsAuth",
    "OAuth2JwtBearerAuth",
    "AwsSigV4Auth",
    "sigv4_sign",
    "Credential",
    "Secret",
    "BearerToken",
    "BasicCredential",
    "ApiKeyCredential",
    "SignatureCredential",
    "ClientCertificate",
    "EnhancedAuth",
    "RequestSigner",
    "apply_http",
    "HttpAuthPlan",
    "register_signer",
    "apply_mqtt",
    "MqttAuthPlan",
    "ApprovalRequest",
    "VerifyReport",
    "Check",
    "RetryPolicy",
    "TransportError",
]
