# thingctx

thingctx is a Python library that turns a W3C Web of Things Thing Description
into tools an AI agent can call. The description is a JSON document naming a
system's actions and a transport for each; thingctx runs every call over that
transport. No server per integration.

You can add a policy gate that decides each call before any transport runs:
allow reads, deny writes, or ask a human first. The binding holds the
credential; the model never sees it.

## First run

    pip install thingctx

The base install pulls no dependencies. This runs with no keys and no network:

```python
import asyncio

import thingctx
from thingctx.contrib.time import make_time_handler

td = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:clock",
    "title": "Clock",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {
        "getCurrentTime": {
            "input": {
                "type": "object",
                "properties": {"timezone": {"type": "string"}},
            },
            "forms": [{"href": "local://getCurrentTime"}],
        }
    },
}


async def main():
    client = thingctx.ThingClient(
        tds=[td], bindings=[thingctx.LocalBinding(make_time_handler())]
    )
    tools, invoke = client.as_tools()
    print("tools:", [t["function"]["name"] for t in tools])
    print(await invoke("clock__getCurrentTime", {"timezone": "UTC"}))


asyncio.run(main())
```

## Agent loop

Install `'thingctx[llm,http]'`, set your provider key, import thingctx, and
inside an async function:

```python
host = await thingctx.from_url("https://example.com/device.td.json")
print(await host.chat("summarize the device's status"))
```

`from_url` accepts any URL that serves a Thing Description.

Closed agent (Claude Desktop, VS Code)? Install `'thingctx[mcp]'` and bridge a
folder of descriptions:

    thingctx-mcp ./registry/

## Docs and source

Full README, examples, and design notes: https://github.com/thingctx/thingctx

Apache-2.0
