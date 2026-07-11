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
pip install thingctx[all]      # litellm + httpx + paho-mqtt + jsonschema + mcp + azure-identity
# or pick extras: thingctx[llm] [http] [mqtt] [validate] [mcp] [authz] [entra]
```

The base install is dependency-free, including the authorization seam
(`thingctx.authz`). The `authz` extra adds the token guard (pyjwt + httpx); the
`entra` extra adds the Entra provider (azure-identity). Nothing heavy ever lands
in the base install.

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

## Safe by default: approval + grounding

Two opt-in layers stand between an agent and a real system.

**Approval** gates risky calls behind a human or a policy. Risk is read from the
TD (`tc:requiresApproval`, or `@type tc:Destructive`) and from a policy you pick:

```python
def approve(req):                      # sync or async; return True to allow
    return input(f"run {req.tool_name}{req.arguments}? [y/N] ").lower() == "y"

client = thingctx.ThingClient(
    tds=[td], bindings=[...], approve=approve, approve_when="declared")

await client.invoke("pump.estop")      # asks approve() first; if denied, never runs
```

`approve_when` is `declared` (default, only TD-marked risky actions),
`destructive` (the above plus any non-idempotent action and every property
write), `all`, or `never`. A gated call with no approver is **denied** , a gate
with nobody to open it stays shut. The check sits in `ThingClient.invoke`, so it
applies to the LLM loop and to direct callers alike.

**Grounding** checks a description against the *live* Thing before you trust it.
`verify()` reads every readable property and confirms it answers and matches its
declared type. It is read-only and safe , actions are never invoked.

```python
for report in await client.verify():
    assert report.ok, report.as_dict()
```

The gate is on `ThingClient.invoke`, so it holds for any caller , a hand loop,
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
  dependency-free core (`thingctx.authz`): no crypto, no network. It takes an
  already-validated identity and answers permit or deny for each
  `(thing, affordance, operation)`.

That split is why the core stays dependency-free while still enforcing: it never
validates a token, it consumes an identity someone already validated.

Authorization is native to `ThingClient`. Pass a `pdp` and an `identity`, and
every device-reaching call authorizes before it selects a transport:

```python
from thingctx import LocalBinding, ThingClient
from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary

vocab = build_vocabulary(ThingClient(tds=[td], bindings=[...]).things)
# the operator role may READ the setpoint, not WRITE it
grants = LocalPolicyGrantSource({"operator": {(thing_id, "target_rpm", "readproperty")}})
pdp = PolicyDecisionPoint(vocabulary=vocab, grant_source=grants)

client = ThingClient(tds=[td], bindings=[...], pdp=pdp, identity=claims)
await client.read_property("pump.target_rpm")    # ALLOWED
await client.write_property("pump.target_rpm", 3000)  # AuthorizationDenied, device untouched
```

The decision is TD-closed: a grant is honored only if the TD's forms actually
declare that operation, so a wildcard grant can never permit an operation no form
exposes, and a read-only property can never be written. The check is at the
dispatch layer, below the transport, so a multi-transport Thing cannot be reached
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
per-integration server.

```bash
pip install "thingctx[mcp,http]"
thingctx-mcp ./examples/registry/        # a folder, a URL, or a TD Directory
```

```json
{ "mcpServers": { "things": { "command": "thingctx-mcp",
  "args": ["./examples/registry/"] } } }
```

Risky tools are gated here too (see [Safe by default](#safe-by-default-approval--grounding)
above): the bridge sends MCP destructive hints and asks the client to confirm a
gated call (elicitation); decline , or a client that can't ask , means denied.
Pick the policy with `THINGCTX_APPROVE_WHEN` (`declared` default, or
`destructive` / `all` / `never`):

```json
{ "mcpServers": { "things": { "command": "thingctx-mcp", "args": ["./examples/registry/"],
  "env": { "THINGCTX_APPROVE_WHEN": "destructive" } } } }
```

MCP is just one way to deliver the description, for agents where direct tool
calling isn't available.

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
(`pdp`/`identity`), the same per-operation check runs for a bus request, so the
gateway is not a bypass. MQTT and MCP ship as drivers; a new middleware is one
`GatewayBinding` class and the engine never changes. See
[`docs/SECURITY.md`](docs/SECURITY.md).

## Where MCP fits

MCP is a way to deliver tools to an agent you cannot hand them to directly. It is
a delivery channel, not the integration itself. The integration, what a system
does, how to reach it, who may call it, is the description. thingctx reads that
description and drives the system client-side; when an agent is closed and only
takes tools through MCP, the same description is delivered over the bridge (see
above). The document is the durable part; the channel is the agent vendor's choice,
and it can change without touching a Thing Description.

To expose a system over MCP you write a server, deploy it, and keep it running,
one per integration. N systems means N processes to operate. A Thing Description
is a static file: write it, or generate one from an existing spec
(`thingctx import openapi <spec>` compiles an OpenAPI file or URL into a TD), then
check it into git or serve it from a URL. There is nothing to run, nothing to keep
alive. Integration becomes data, not a service, and data scales to a fleet for free.

A messy device (binary protocol, a session dance) gets one thin connector that
exposes a clean WoT face; the TD describes *that*. The connector is consumed
the same way by an LLM, an MCP client, or anything else.

The table below measures one axis: what you build and operate per integration, not
what each approach can do. thingctx wins on operational weight and cold-start; it
says nothing about auth depth, policy, or ecosystem, which are their own questions.

See [`examples/01_mcp_baseline.py`](examples/01_mcp_baseline.py) (a server per
integration) and
[`examples/02_thingctx_baseline.py`](examples/02_thingctx_baseline.py) (no
server). Both drive the same pump; every result is asserted equal to calling
the system directly.

| per integration      | MCP (stdio) | MCP (http)   | thingctx |
| -------------------- | ----------- | ------------ | -------- |
| server process       | per session | 1, long-run  | 0        |
| hand-written lines   | 142         | 142          | 10       |
| time to first call   | 540 ms      | 13 ms        | 2 ms     |

thingctx calls in milliseconds because there is no server to start. MCP needs
one, and the transport sets the cost: stdio spawns it per session (the first
call pays process startup, 540 ms), streamable-HTTP is a server you keep running
and connect to (13 ms, warm). Once connected, per-call latency is small for all
three; the difference is the server you build and run to get there. The Thing
Description is data, about 145 lines of JSON, written once and read by every
consumer. Reproduce with `python examples/_measure.py`.

## Interoperability

thingctx consumes a Thing Description no matter who wrote it, including TDs emitted
by standards-compliant producers, not just hand-written ones. Two demos under
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

## And UTCP

[UTCP](https://www.utcp.io/) shares thingctx's thesis: the integration is a
description the client reads, not a server you operate. The difference is the
description. UTCP defines its own *manual* format and ships SDKs in several
languages today. thingctx builds on the **ratified [W3C Web of Things](https://www.w3.org/WoT/)
Thing Description** instead, which buys four things a bespoke manual does not:

- **One format for devices and APIs.** A TD describes a REST endpoint, an MQTT
  topic, an SSE event stream, or a piece of hardware in the same document, so an
  agent reaches an industrial gateway and a SaaS API through one interface.
- **Discovery built in.** The [WoT Thing Description Directory](https://www.w3.org/TR/wot-discovery/)
  is a standard for serving and searching a whole fleet of Things; thingctx reads
  any compliant TDD.
- **Vendor-neutral and stable.** It is a W3C Recommendation, not a single
  project's schema, so a TD you write is portable across consumers.
- **Built for device interaction patterns.** A TD models properties, actions,
  and **events** as first-class affordances, so a consumer can observe a property
  or subscribe to a stream of readings straight from the description. UTCP's
  manual centers on describing callable tools; event subscription is not part of
  what it defines.

The trade-off: UTCP's manual is lighter to hand-write and UTCP ships more
language clients today. thingctx bets that a ratified standard, read the same way
by an LLM, an MCP client, and a factory gateway, is worth that.

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
supplied to the binding at runtime, never in the TD, so a TD is safe to commit
and share. Secrets are keyed by Thing id, then slug, then scheme name, so one
client can carry a different secret per Thing.

```python
thingctx.HttpBinding(credentials={"weather": "secret"})  # by Thing id/slug, or scheme name
```

## License

Apache-2.0. Copyright 2026 The thingctx Authors.
