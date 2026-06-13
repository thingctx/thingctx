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
from thingctx.client import from_file, from_td, from_url

# LLMHost: optional tool-calling loop, in thingctx.contrib.
from thingctx.contrib.llm import LLMHost
from thingctx.invokers import (
    HttpInvoker,
    Invoker,
    LocalInvoker,
    MqttInvoker,
)
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
from thingctx.validate import TDValidationError, validate_td

__version__ = "0.1.2"

__all__ = [
    "from_url",
    "from_file",
    "from_td",
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
]
