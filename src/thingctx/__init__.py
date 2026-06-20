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
    register_auth,
    register_signer,
    resolve_credentials,
    sigv4_sign,
)
from thingctx.client import from_file, from_td, from_url

# LLMHost: optional tool-calling loop, in thingctx.contrib.
from thingctx.contrib.llm import LLMHost
from thingctx.invokers import (
    HttpInvoker,
    Invoker,
    LocalInvoker,
    MqttInvoker,
)

# Compile a non-WoT description (OpenAPI) into a TD.
from thingctx.openapi import from_openapi, load_spec
from thingctx.registry import (
    FileRegistry,
    Registry,
    TDDRegistry,
    from_arg,
    from_args,
)
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
    "Invoker",
    "HttpInvoker",
    "MqttInvoker",
    "LocalInvoker",
    "register_auth",
    "resolve_credentials",
    "AuthStrategy",
    "CredentialProvider",
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
]
