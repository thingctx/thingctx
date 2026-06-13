# thingctx

**One description, and your model reads from and acts on a real device,
sensor, tool, or service, directly. No per-integration server.**

[**thingctx.com**](https://thingctx.com): browse real services (GitHub, Stripe,
Slack, and more) as ready-to-use Thing Descriptions.

thingctx uses the [W3C Web of Things](https://www.w3.org/WoT/) standard as a
uniform interface between an AI app and the systems it reaches. Point it at a
Thing Description and it drives the actual Thing the description names, over
that Thing's own transport. The integration is a document, not a server you
run.

A "Thing" is anything with a callable interface, not just hardware: a sensor
or a robot, but equally a REST API, a database, a SaaS product, an internal
service. A Thing Description (TD) is plain JSON that names that system's
`actions` (things to do), `properties` (state to read or write), and `events`
(things to subscribe to), plus the transport for each (HTTP, MQTT, local, and
more). thingctx reads it, hands the actions to your model as tools, and calls
each against the real system. The system's own endpoints are the server; you
write nothing server-side.

A whole TD can be this small (a weather API, no hardware in sight):

```json
{
  "@context": "https://www.w3.org/2022/wot/td/v1.1",
  "id": "urn:example:weather:v1",
  "title": "Weather",
  "securityDefinitions": { "bearer_sc": { "scheme": "bearer" } },
  "security": ["bearer_sc"],
  "properties": {
    "temperature": { "type": "number", "readOnly": true,
      "forms": [{ "href": "https://api.example.com/temp" }] }
  },
  "actions": {
    "forecast": {
      "input": { "type": "object", "properties": { "city": { "type": "string" } } },
      "forms": [{ "href": "https://api.example.com/forecast", "htv:methodName": "POST" }]
    }
  }
}
```

Point an agent at it:

```python
import thingctx

host = await thingctx.from_url("https://api.example.com/.well-known/wot")
print(await host.chat("what's the forecast for Cairo, and the current temperature?"))
```

The model picks the actions; thingctx routes each to its transport.

## Install

```bash
pip install thingctx[all]      # litellm + httpx + paho-mqtt + jsonschema + mcp
# or pick extras: thingctx[llm] [http] [mqtt] [validate] [mcp]
```

## Drive it directly

Own the agent loop? Read a description, get the tool specs to hand your model,
and route each call back to the Thing. Nothing in between.

```python
import thingctx

client = thingctx.ThingClient.from_registry(
    thingctx.from_arg("http://device.local/.well-known/wot"))   # a URL, folder, or TDD
specs, invoke = client.as_tools()        # specs for your model; invoke(name, args) runs a call

await invoke("pump.set_speed", {"rpm": 1500})
await client.read_property("pump.rpm")
```

Add a Thing by pointing at one more description.

## Reach a closed agent: the MCP bridge

Some agents are closed: you can't hand their model tools directly, only
through MCP (Claude Desktop, the Claude CLI, Copilot). For those, thingctx
ships one generic MCP server that turns a registry of descriptions (a folder,
a URL, or a W3C Thing Description Directory) into MCP tools, with no
per-integration server.

```bash
pip install "thingctx[mcp,http]"
thingctx-mcp ./examples/registry/        # a folder, a URL, or a TD Directory
```

```json
{ "mcpServers": { "things": { "command": "thingctx-mcp",
  "args": ["./examples/registry/"] } } }
```

MCP is just one way to deliver the description, for agents where direct tool
calling isn't available.

## Why not MCP

To expose a system over MCP you write a server, deploy it, and keep it
running, one per integration. N systems means N processes to operate. A Thing
Description is a static file: write it (or generate it), check it into git, or
serve it from a URL. There is nothing to run, nothing to keep alive.
Integration becomes data, not a service, and data scales to a fleet for free.

A messy device (binary protocol, a session dance) gets one thin connector that
exposes a clean WoT face; the TD describes *that*. The connector is consumed
the same way by an LLM, an MCP client, or anything else.

See [`examples/01_mcp_baseline.py`](examples/01_mcp_baseline.py) (a server per
integration) and
[`examples/02_thingctx_baseline.py`](examples/02_thingctx_baseline.py) (no
server). Both drive the same pump; every result is asserted equal to calling
the system directly.

## Reference

### ThingClient: the core

Stdlib only, with no dependency on any agent framework. `ThingClient` has no
LLM and no opinion on what chose the action. It reads properties, writes them,
and streams events, and routes each call to the transport the TD's form names,
so one client can read over HTTP and subscribe over MQTT without you wiring
either:

```python
await client.read_property("pump.rpm")          # e.g. an HTTP GET
await client.write_property("pump.target_rpm", 1500)
async for evt in await client.subscribe("pump.overheat"):   # e.g. an MQTT topic
    ...                              # evt is the payload, e.g. {"temp": 98}
```

(`thingctx.from_url(...)` returns a ready `LLMHost` if you just want a loop out
of the box.)

### Registry

Where descriptions come from: a folder of files, a URL, or a **Thing
Description Directory** (TDD). `ThingClient`, the MCP bridge, and the LLM loop
all build from the same registry.

```python
client = thingctx.ThingClient.from_registry(thingctx.from_arg("./examples/registry/"))
```

The hosted registry at [thingctx.com](https://thingctx.com) is one such source.

The TDD is the [W3C WoT Discovery](https://www.w3.org/TR/wot-discovery/)
standard (a final Recommendation): a service that serves a whole fleet of
Things from a `/things` endpoint, with optional search. thingctx reads from any
compliant TDD. Point `from_arg` at its URL.

### Authentication

The TD declares the scheme (`bearer`, `basic`, `apikey`); the secret is
supplied to the invoker at runtime, never in the TD, so a TD is safe to commit
and share. Secrets are keyed by Thing id, then slug, then scheme name, so one
client can carry a different secret per Thing.

```python
thingctx.HttpInvoker(credentials={"weather": "secret"})  # by Thing id/slug, or scheme name
```

## License

Apache-2.0. Copyright 2026 The thingctx Authors.
