# thingctx

Drive any agent against any W3C Web of Things Thing, over any transport.
The integration is a JSON Thing Description, not a server you run.

## Install

    pip install thingctx[all]
    # or pick extras: thingctx[llm] [http] [mqtt] [validate] [mcp]

## Use

```python
import thingctx

# Out-of-the-box agent loop
host = await thingctx.from_url("https://api.example.com/.well-known/wot")
print(await host.chat("what's the forecast for Cairo?"))
```

Own the loop? Get tool specs and route calls yourself:

```python
client = thingctx.ThingClient.from_registry(thingctx.from_arg("./registry/"))
specs, invoke = client.as_tools()
await invoke("pump.set_speed", {"rpm": 1500})
```

Closed agent (Claude Desktop, Copilot)? Bridge a registry of descriptions to MCP:

    thingctx-mcp ./registry/

## Docs and source

Full README, examples, and design notes: https://github.com/thingctx/thingctx

Apache-2.0
