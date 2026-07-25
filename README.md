# thingctx

[![PyPI](https://img.shields.io/pypi/v/thingctx?style=flat-square&label=PyPI&color=3775A9)](https://pypi.org/project/thingctx/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square)](https://pypi.org/project/thingctx/)
[![License](https://img.shields.io/badge/License-Apache--2.0-4c9a2a?style=flat-square)](LICENSE)
&nbsp;
![W3C WoT](https://img.shields.io/badge/W3C_WoT-TD_1.1-005a9c?style=flat-square)
![WoT Discovery](https://img.shields.io/badge/W3C_WoT-Discovery_(TDD)-005a9c?style=flat-square)
![MCP](https://img.shields.io/badge/MCP-ready-6f42c1?style=flat-square)
![OpenAPI](https://img.shields.io/badge/OpenAPI-3.x_import-6ba539?style=flat-square)
&nbsp;
![Microsoft Entra](https://img.shields.io/badge/Microsoft_Entra-compatible-0078d4?style=flat-square)
![Cloudflare Access](https://img.shields.io/badge/Cloudflare_Access-compatible-f38020?style=flat-square)
![AWS SigV4](https://img.shields.io/badge/AWS_SigV4-signing-ff9900?style=flat-square)
![OAuth2](https://img.shields.io/badge/OAuth2-PKCE-eb5424?style=flat-square)
![AuthZEN](https://img.shields.io/badge/AuthZEN-authorization-005a9c?style=flat-square)

![How thingctx drives a gated tool call from a Thing Description over the system's own transport.](assets/hero.gif)

**thingctx drives an AI agent's tool calls against real systems from a
document that describes each system.** Point it at a description and your agent
calls that system over its own transport. Browse ready made descriptions at
[td.thingctx.com](https://td.thingctx.com): apps like Gmail, GitHub, Slack, and
Notion; developer tools like Git, GitLab, and Sentry; and devices like Home
Assistant, Hue, Nest, and MQTT and RTSP hardware. 32 today, growing.

Use it as a Python library in your own app; from a coding agent over the
command line; or as an MCP server for Claude Desktop, VS Code, or any MCP
client. Same descriptions, same policy gate, every path. No server per integration.

Jump to [Install](#install), [first run](#first-run-no-keys-no-network),
[Claude Desktop and VS Code](#reach-a-closed-agent-the-mcp-bridge), or
[authorization](#authorization-who-may-do-what).

The description is a document: a [W3C Web of Things](https://www.w3.org/WoT/)
Thing Description, a plain JSON file. A "Thing" is anything with a callable
interface:
a REST API, a database, an app, an internal service, a sensor, a robot.
The description names that system's `actions` (things to do), `properties`
(state to read or write), and `events` (things to subscribe to), plus the
transport for each (HTTP, MQTT, local, and more). One description can span
several transports at once: a media server that takes commands over HTTP and
streams over RTSP, a device controlled over HTTP whose events arrive over
WebSocket, one hardware Thing that reads over MQTT and acts over a local call.
That whole multi protocol system is one description and one tool set to the
agent. thingctx reads it, hands the actions to your model as tools, and calls
each against the real system. The system's own endpoints are the server; you
write nothing server side.

Every call passes a policy gate keyed on the operation and the caller. Grant
read on a resource and deny write on that same resource. The credential stays
in the binding, refreshed when it expires; the model never sees it.

## First run: no keys, no network

The repo ships a clock Thing: a TD over the in process time handler bundled
with thingctx. Paste this into a file and run it from the repo root (after
`pip install thingctx`; nothing else is needed):

```python
import asyncio
import json

import thingctx
from thingctx.contrib.time import make_time_handler


async def main():
    with open("examples/registry/time.td.json") as f:
        td = json.load(f)
    client = thingctx.ThingClient(
        tds=[td], bindings=[thingctx.LocalBinding(make_time_handler())]
    )
    tools, invoke = client.as_tools()  # specs for your model; invoke runs a call
    print("tools:", [t["function"]["name"] for t in tools])
    print(await invoke("time__getCurrentTime", {"timezone": "UTC"}))


asyncio.run(main())
```

It prints the two projected tools, then a real timestamp. That is the whole
model: a TD in, tools out, calls routed. Every other transport (HTTP, MQTT)
works the same way; only the form's `href` changes.

## The document

A whole TD can be this small. This one drives the live, no key
[Open-Meteo](https://open-meteo.com) forecast API, so it runs as is:

```json
{
  "@context": "https://www.w3.org/2022/wot/td/v1.1",
  "id": "urn:example:weather:v1",
  "title": "Weather",
  "securityDefinitions": { "nosec_sc": { "scheme": "nosec" } },
  "security": ["nosec_sc"],
  "actions": {
    "forecast": {
      "input": {
        "type": "object",
        "properties": {
          "latitude": { "type": "number" },
          "longitude": { "type": "number" },
          "current": { "type": "string" }
        },
        "required": ["latitude", "longitude"]
      },
      "forms": [{ "href": "https://api.open-meteo.com/v1/forecast", "htv:methodName": "GET" }]
    }
  }
}
```

A description also scales up. Each affordance names its own `href`, so one TD
can drive a whole multi protocol system: the `mediamtx` TD in the registry
controls a media server over HTTP and pulls its stream over RTSP; a
`home-assistant` TD calls actions over HTTP and receives events over WebSocket;
the `pump` example reads over MQTT and runs a local action, all in one file.
The agent sees one tool set; thingctx routes each call to the right transport.

Point an agent at it. The weather API needs no key (it is `nosec`); the LLM loop
needs the `[llm]` extra, your provider's API key in its usual env var (for
example `OPENAI_API_KEY`), and a model via `THINGCTX_MODEL` (a litellm
`provider/model` string; the default is `openai/gpt-4o-mini`):

```python
import asyncio

import thingctx


async def main():
    host = thingctx.from_file("weather.td.json")        # the TD above, saved to a file
    print(await host.chat("what's the forecast for Cairo? Use latitude 30.0, longitude 31.2."))


asyncio.run(main())
```

The model picks the actions; thingctx routes each to its transport. (The URL
here is a placeholder; substitute a real TD endpoint, a TD file, or a folder.)

## Install

```bash
pip install 'thingctx[llm,http,validate]'   # the recommended start: LLM loop + HTTP + TD validation
```

Quote the argument; unquoted brackets fail in zsh (macOS default) with
`no matches found`. The base `pip install thingctx` has no dependencies,
including the authorization seam (`thingctx.authz`). Everything else is an
optional extra; add only what you use:

- `llm`: the agent loop, any provider via litellm.
- `http`: the HTTP(S) transport (httpx).
- `mqtt`: the MQTT transport (paho-mqtt).
- `validate`: check TDs against the official W3C TD 1.1 schema (jsonschema).
- `mcp`: the MCP bridge for closed agents (Claude Desktop, Copilot).
- `mcp-http`: serve the MCP bridge over streamable HTTP (adds uvicorn).
- `openapi`: import OpenAPI specs as TDs (YAML support; JSON needs nothing).
- `cloud`: OAuth2 JWT bearer assertions for cloud APIs (pyjwt).
- `authz`: the inbound token guard, JWT to claims (pyjwt + httpx).
- `entra`: Microsoft Entra identity provider and guard (azure-identity).
- `media`: continuous audio/video streams (PyAV/FFmpeg, numpy, pillow; heavy).
- `filesystem`: the sandboxed local filesystem handler (stdlib; inert until
  `THINGCTX_FS_ROOT` is set).
- `all`: everything above, including the heavy media and Entra dependencies.
  Reach for it only when you actually want all of that installed.

## Drive it directly

Own the agent loop? Read a description, get the tool specs to hand your model,
and route each call back to the Thing. Nothing in between.

```python
import thingctx

client = thingctx.ThingClient.from_registry(
    thingctx.from_arg("http://device.local/.well-known/wot"))   # a URL, folder, or TDD
specs, invoke = client.as_tools()        # specs for your model; invoke(name, args) runs a call

await invoke("pump__set_speed", {"rpm": 1500})
await client.read_property("pump__rpm")
```

Add a Thing by pointing at one more description.

## Just a TD runtime: no agent, no LLM, no MCP

Under the agent surfaces, thingctx is a runtime that executes a Thing
Description. Any code can drive it. `ThingClient` is stdlib only, with no LLM
and no opinion on what chose the action. It reads properties, writes them, and
streams events, routing each call to the transport the TD's form names, so one
client reads over HTTP and subscribes over MQTT without you wiring either:

```python
import thingctx

client = thingctx.ThingClient.from_registry(thingctx.from_arg("./descriptions/"))
await client.read_property("pump__rpm")               # e.g. an HTTP GET
await client.write_property("pump__target_rpm", 1500) # gated like every call
async for evt in await client.subscribe("pump__overheat"):  # e.g. an MQTT topic
    ...                                # evt is the payload, e.g. {"temp": 98}
```

So thingctx is useful with no agent in sight: a declarative way to drive your
APIs and devices from a versioned file, with one policy gate on every call.

## You do not need to change thingctx to use it

Write a description for your system and point the runtime at it. That is the
whole integration. thingctx does not need to know your device exists, so you
never fork it and never wait for a pull request. A thousand descriptions cost no
more to run than one.

There is one exception: a transport thingctx cannot speak yet. Then you write a
binding, which is a single class. It names the scheme it handles and implements
the operations that scheme supports.

```python
class CoapBinding:
    scheme = "coap"
    schemes = ("coap", "coaps")   # optional, when one class serves several

    async def invoke(self, action, form, arguments): ...   # required
    async def read(self, prop, form): ...                  # add what it supports
```

The scheme and `invoke` are the contract. Add `read`, `write`, `subscribe` or
the media methods if your transport supports them, and leave out the ones it
does not. The runtime checks which methods exist before it calls them, so a
pub/sub transport with no way to do a read simply does not have one. See
[docs/BINDINGS.md](docs/BINDINGS.md).

Then you pick one of two things, and both are fully supported.

**Keep the binding.** Pass it to the client. It stays in your own repository, on
your release schedule, under your license, and thingctx never needs to know.

```python
client = thingctx.ThingClient(tds=[...], bindings=[CoapBinding()])
```

**Or contribute it.** If other people need the same transport, send a pull
request and it joins the built-ins, so nobody writes it twice.

Keeping a binding private is not a workaround for one we have not merged. It is
a normal way to use thingctx, and it uses the same seam a built-in does.

## Safe by default: approval + grounding

Two optional layers stand between an agent and a real system.

**Approval** gates risky calls behind a human or a policy. Risk is read from the
TD (`tc:requiresApproval`, or `@type tc:Destructive`) and from a policy you pick:

```python
def approve(req):                      # sync or async; return True to allow
    return input(f"run {req.tool_name}{req.arguments}? [y/N] ").lower() == "y"

client = thingctx.ThingClient(
    tds=[td], bindings=[...], approve=approve, approve_when="declared")

await client.invoke("pump__estop")     # asks approve() first; if denied, never runs
```

`approve_when` is `declared` (default, only TD marked risky actions),
`destructive` (the above plus any non-idempotent action and every property
write), `all`, or `never`. A gated call with no approver is **denied**: a gate
with nobody to open it stays shut. The check sits in `ThingClient.invoke`, so it
applies to the LLM loop and to direct callers alike.

**Grounding** checks a description against the *live* Thing before you trust it.
`verify()` reads every readable property and confirms it answers and matches its
declared type. It is read-only and safe; actions are never invoked.

```python
for report in await client.verify():
    assert report.ok, report.as_dict()
```

The gate is on `ThingClient.invoke`, so it holds for any caller: a hand loop,
the LLM host, or an MCP client (Claude/Copilot CLI; see
[Reach a closed agent](#reach-a-closed-agent-the-mcp-bridge) below).

Runnable: [`examples/04_trust.py`](examples/04_trust.py). Full model:
[`docs/USAGE.md`](docs/USAGE.md).

## Authorization: who may do what

Approval asks a human. Authorization decides from policy, per caller, per
operation, before the device is touched. thingctx separates two things that are
usually blurred together:

- **Authentication (authn)** proves *who* the caller is. It validates a bearer
  token into a claims dict. It needs crypto, so it lives in `thingctx.identity`
  behind the `authz` extra (or an upstream gateway does it for you).
- **Authorization (authz)** decides *what* that caller may do. It runs on the
  dependency free core (`thingctx.authz`): no crypto, no network. It takes an
  already validated identity and answers permit or deny for each
  `(thing, affordance, operation)`.

That split is why the core stays dependency free while still enforcing: it never
validates a token, it consumes an identity someone already validated.

Authorization is native to `ThingClient`. Pass a `pdp` and an `identity`, and
every device reaching call authorizes before it selects a transport:

```python
from thingctx import LocalBinding, ThingClient
from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary

vocab = build_vocabulary(ThingClient(tds=[td], bindings=[...]).things)
# the operator role may READ the setpoint, not WRITE it
grants = LocalPolicyGrantSource({"operator": {(thing_id, "target_rpm", "readproperty")}})
pdp = PolicyDecisionPoint(vocabulary=vocab, grant_source=grants)

client = ThingClient(tds=[td], bindings=[...], pdp=pdp, identity=claims)
await client.read_property("pump__target_rpm")    # ALLOWED
await client.write_property("pump__target_rpm", 3000)  # AuthorizationDenied, device untouched
```

The decision is closed to the TD: a grant is honored only if the TD's forms actually
declare that operation, so a wildcard grant can never permit an operation no form
exposes, and a read-only property can never be written. The check is at the
dispatch layer, below the transport, so a multi transport Thing cannot be reached
around it, and streams (observe, event, media) are authorized at subscribe time
*and* per delivery, so a stream stops the moment the token expires.

The PDP is pluggable. The lean local one ships by default; the same seam speaks
the OpenID [AuthZEN](https://openid.net/specs/authorization-api-1_0.html) 1.0
wire format, so you can point at your own OPA, Cedar, or enterprise PDP without
changing thingctx.

Runnable: [`examples/14_authz.py`](examples/14_authz.py) (authz on core alone)
and [`examples/15_authn_to_authz.py`](examples/15_authn_to_authz.py) (a real
token validated by the guard, then enforced). Full model:
[`docs/SECURITY.md`](docs/SECURITY.md).

**Trusting the description itself.** A TD carries no secrets, so it is safe to
commit and share, and thingctx fetches only over `http(s)` and validates a TD
against the W3C TD 1.1 schema before using it (install `thingctx[validate]`). But
a description you fetch is code your agent acts on, so treat its source the way you
treat any dependency: prefer a TD you wrote or a directory you control over an
arbitrary URL. Content pinning and signature verification (so a fetched TD must
match a known digest or a trusted signer) are a design in progress, not yet shipped;
until then, the trust boundary is the source you point thingctx at.

## Reach a closed agent: the MCP bridge

Some agents are closed: you can't hand their model tools directly, only
through MCP (Claude Desktop, the Claude CLI, Copilot). For those, thingctx
ships one generic MCP server that turns a registry of descriptions (a folder,
a URL, or a W3C Thing Description Directory) into MCP tools, with no
per integration server.

```bash
pip install "thingctx[mcp,http]"
thingctx-mcp ./examples/registry/        # a folder, a URL, or a TD Directory
```

For Claude Desktop, add this to the config file at
`~/Library/Application Support/Claude/claude_desktop_config.json`
(on Windows: `%APPDATA%\Claude\claude_desktop_config.json`), then restart
Claude Desktop:

```json
{ "mcpServers": { "things": {
  "command": "uvx",
  "args": ["--from", "thingctx[mcp]", "thingctx-mcp", "https://td.thingctx.com/index.json"] } } }
```

That runs with only [uv](https://docs.astral.sh/uv/) on the machine; no prior install,
no PATH setup. If you have already `pip install 'thingctx[mcp]'`, set `"command"` to
`thingctx-mcp` (no `uvx`, no `--from` args). The URL is the hosted catalog; point at
a folder of TD files or any TD Directory URL instead to serve your own. (`uvx`
pulls thingctx from PyPI, so catalog index URLs resolve once 0.2.0 is
published; a local folder works with any version.)

Risky tools are gated here too (see [Safe by default](#safe-by-default-approval--grounding)
above): the bridge sends MCP destructive hints and asks the client to confirm a
gated call (elicitation); declining, or a client that cannot ask, means denied.
Pick the policy with `THINGCTX_APPROVE_WHEN` (`declared` default, or
`destructive` / `all` / `never`):

```json
{ "mcpServers": { "things": {
  "command": "uvx",
  "args": ["--from", "thingctx[mcp]", "thingctx-mcp", "https://td.thingctx.com/index.json"],
  "env": { "THINGCTX_POLICY": "read-only", "THINGCTX_APPROVE_WHEN": "destructive" } } } }
```

`THINGCTX_POLICY` is `read-only` (reads and TD-declared safe actions only;
writes and state changing actions are denied) or `full`. Edit the same config
file as above and restart Claude Desktop after a change.

### Add your keys

A TD names its security scheme but never carries a secret. The bridge reads
per Thing secrets from the environment: `THINGCTX_TOKEN_<SLUG>` binds a secret
to the Thing whose slug is `<SLUG>` (lowercased, `_` maps to `-`, so
`THINGCTX_TOKEN_GOOGLE_MAPS` serves `google-maps`). The slug is the same one
used in tool names. To let the agent act on GitHub:

```json
{ "mcpServers": { "things": {
  "command": "uvx",
  "args": ["--from", "thingctx[mcp]", "thingctx-mcp", "https://td.thingctx.com/index.json"],
  "env": { "THINGCTX_TOKEN_GITHUB": "ghp_yourtoken" } } } }
```

The secret is applied per the Thing's declared scheme (bearer, basic, apikey)
and lives only in the process environment, never in a TD or on disk.

Two more environment knobs the bridge and CLI honor:

- `THINGCTX_REGISTRY`: default TD source(s) when no argument is given, path
  separator delimited (a folder, a TD file, or a directory URL).
- `THINGCTX_FS_ROOT`: enables the sandboxed filesystem Thing by naming the one
  directory it may touch; unset (the default) refuses every filesystem call.

## Serve a whole fleet: the gateway

The MCP bridge and the direct client both REACH devices. A gateway goes the other
way: it re-serves a fleet of Things onto a middleware, so any consumer on that bus
drives every device through one protocol, with no client per device. One process
fronts the fleet.

```python
from thingctx import ThingClient
from thingctx.gateways import Gateway
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding

client = ThingClient(tds=[...], bindings=[...])              # reach the devices
gateway = Gateway(client, MqttGatewayBinding("broker:1883"))  # serve them on the bus
await gateway.start()
```

A request arrives on a topic, the gateway resolves it against the native device,
and the reply goes back on the bus. The engine names only the five WoT operations;
the driver owns the wire. If the client carries the authorization seam
(`pdp`/`identity`), the same per operation check runs for a bus request, so the
gateway is not a bypass. MQTT and MCP ship as drivers; a new middleware is one
`GatewayBinding` class and the engine never changes. See
[`docs/SECURITY.md`](docs/SECURITY.md).

### Serve it remotely: one gateway, reached by URL

Run the bridge over streamable HTTP so many callers, or an agent runtime that only
takes a remote MCP URL, reach one gateway instead of each running a local copy:

```
thingctx-mcp --http --host 0.0.0.0 --port 8080 ./examples/registry/
```

A client points at `http://host:8080/`. This is the central-gateway shape: one process
serves the fleet, the per operation authorization and approval gate apply to every
caller, and no one holds the raw credentials. Run it in a container:

```
docker run -p 8080:8080 -v $PWD/things:/things ghcr.io/thingctx/thingctx:0.2.0
```

(The `0.2.0` image tag resolves once that release is published; until then use
the latest published tag.)

A Kubernetes example (stateless Deployment + Service, credentials from a mounted
Secret) is in [`packaging/k8s/`](packaging/k8s/).

## No Thing Description? Compile one from OpenAPI

You do not have to author one. If your system already has an OpenAPI 3.x spec,
that is a description too, and thingctx compiles it:

```bash
pip install "thingctx[openapi,http]"
thingctx import openapi https://api.example.com/openapi.json --out weather.td.json
```

The spec can be a file or a URL, JSON or YAML. `--base-url`, `--id` and
`--title` override what the spec says. The same thing in Python, when you would
rather not keep a file around:

```python
from thingctx.openapi import from_openapi, load_spec

td = from_openapi(load_spec("openapi.json"))   # a TD dict, ready to drive
```

Either way you get a normal Thing Description, so everything else in this README
applies to it unchanged. A three line spec above became `urn:thingctx:weather`
with one action, projected to the tool `weather__getForecast`, and it passes
`validate_td` against the W3C schema.

Each `get`, `put`, `post`, `delete` and `patch` operation becomes an action.
`GET` is marked safe, so a read-only policy allows it and refuses the writes,
and `PUT` and `DELETE` are marked idempotent even though they are not safe,
which is what makes them retryable. Path parameters stay as `{city}`
placeholders the runtime fills at call time.

## Write a description by hand

A Thing Description is a static file: write it, or generate it, then check it
into git or serve it from a URL. The same document is read by the direct client,
the LLM loop, and the MCP bridge.

A messy device (binary protocol, a session dance) gets one thin connector that
exposes a clean WoT face; the TD describes *that*. The connector is consumed
the same way by an LLM, an MCP client, or anything else.

A full worked Thing lives at
[`examples/02_thingctx_baseline.py`](examples/02_thingctx_baseline.py): one
pump TD spanning four transports (local, HTTP, MQTT, SSE), driven end to end
with every result asserted against the device.

## Interoperability

thingctx consumes a Thing Description no matter who wrote it, including TDs emitted
by standards compliant producers, not just hand written ones. Two demos under
[`examples/interop/`](examples/interop/) prove it end to end:

- [**node-wot**](examples/interop/nodewot/): the
  [W3C WoT reference implementation](https://github.com/eclipse-thingweb/node-wot)
  exposes a `counter` Thing; thingctx fetches its served TD and drives it
  (read, increment, read) with no node-wot client in the loop.
- [**Eclipse Ditto**](examples/interop/ditto/): Ditto generates a TD for a
  digital twin; thingctx consumes it and round-trips twin state straight
  through Ditto's API.

Same consumer, different producers, zero glue: any conformant TD producer →
thingctx.

## Why a Thing Description, and not another tool format

Being a standard is the least interesting reason. Two others decide it.

**It is general enough to describe anything you would point an agent at.** One
format covers a REST endpoint, an MQTT topic, an SSE stream and a piece of
hardware, with properties, actions and events as first class ideas rather than
one flat list of functions. Most tool formats describe a function you call. A TD
describes a system you interact with, which is why a camera and a Stripe
endpoint fit the same document.

**You can get there from the descriptions you already have.** A format you must
hand author is a format nobody adopts. An OpenAPI spec compiles to a TD today
with `thingctx import openapi`, and the same translation is possible from other
self describing systems: an OPC UA server publishes an address space you can
browse, and the OPC Foundation has standardised the mapping in
[OPC 10101](https://reference.opcfoundation.org/specs/OPC-10101/1), its official
OPC UA to WoT binding. That importer is not written yet, it is
[issue #38](https://github.com/thingctx/thingctx/issues/38), but the standard it
would follow already exists. A TD is a destination you can reach by compiling,
rather than one more format asking you to start over.

The rest is what you would expect from a ratified
[W3C Web of Things](https://www.w3.org/WoT/) Recommendation:

- **Discovery built in.** The [WoT Thing Description Directory](https://www.w3.org/TR/wot-discovery/)
  is a standard for serving and searching a whole fleet of Things; thingctx reads
  any compliant TDD.
- **Vendor neutral and stable.** It is a W3C Recommendation, so a TD you write is
  portable across consumers. Nothing here is specific to thingctx, and another
  WoT consumer reads the same file.

## Reference

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
supplied to the binding at runtime, never in the TD, so a TD is safe to commit
and share. Secrets are keyed by Thing id, then slug, then scheme name, so one
client can carry a different secret per Thing.

```python
thingctx.HttpBinding(credentials={"weather": "secret"})  # by Thing id/slug, or scheme name
```

## Contributing

Driving your own device needs nothing from this repository. You write a
description, thingctx reads it, and neither side has to know about the other.

What the project does need is transports it cannot speak yet. Each one is a
single class. Start at the issues labeled
[`good first issue`](https://github.com/thingctx/thingctx/labels/good%20first%20issue),
and see [CONTRIBUTING.md](CONTRIBUTING.md) for how a binding fits together.

Questions do not need an issue.
[Discussions](https://github.com/thingctx/thingctx/discussions) is for "does this
already do X", "would you take a PR for Y", and how to reach a device you have.

## License

Apache-2.0. Copyright 2026 The thingctx Authors.
