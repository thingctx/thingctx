# Gateway bindings: serve a fleet over any bus

thingctx drives devices, and it serves them. This document is how to add a NEW
middleware, an MQTT bus, MCP, DDS, Kafka, so that a fleet of Things becomes
reachable over it, by writing one driver and shipping it as a package. The engine
never changes.

The design follows how OPC-UA (Part 14), W3C WoT (Binding Templates), and DDS all
separate a neutral capability model from a swappable transport, and carry
protocol-specific richness in a per-transport slot the core never inspects.

## The two sides

- **Consumer binding** (`ProtocolBinding`): drives a device.
  thingctx speaks one transport outbound to reach a real Thing. Documented in
  [BINDINGS.md](BINDINGS.md).
- **Gateway binding** (`GatewayBinding`): serves a fleet. thingctx re-serves
  Things onto a middleware so any consumer drives them uniformly. This document.

A `Gateway` joins them: it holds a `ThingClient` to reach the devices, and a
`GatewayBinding` to serve them on the bus.

```python
from thingctx import ThingClient
from thingctx.gateways import Gateway
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding

client = ThingClient(tds=[...], bindings=[...])              # reach the devices
gateway = Gateway(client, MqttGatewayBinding("broker:1883"))  # serve them on the bus
await gateway.start()
```

## The engine owns the neutral model; the driver owns the transport

The `Gateway` engine is transport-neutral. It holds the fleet, names the five
abstract WoT operations (`readproperty`, `writeproperty`, `observeproperty`,
`invokeaction`, `subscribeevent`), and runs the invariant loop: a driver hands it
a neutral `ServeRequest`, the engine resolves it against the native device
(`gateway.dispatch(req)`), and the result goes back to the driver. The engine
references no topic, QoS, retain, observe, or partition, ever.

The driver owns everything transport-shaped: addressing, the op-to-wire mapping,
encode/decode, and the transport's specific features.

## The contract: what a driver implements

A gateway binding is a class satisfying `GatewayBinding`:

```python
class MyGatewayBinding:
    scheme = "mybus"                       # the re-served form scheme

    def project_forms(self, thing, affordance, op):
        # return this driver's re-served form(s) for one (affordance, op),
        # carrying your OWN namespaced vocabulary. Return [] for an op your
        # transport cannot carry, so the projected TD is honest.
        ...

    async def serve(self, gateway):
        # connect; for each inbound message build a ServeRequest and await
        # gateway.dispatch(req); deliver the result on your wire.
        ...

    async def aclose(self):
        ...
```

Run it against the conformance kit so a contract breach fails loudly:

```python
from thingctx.testing import assert_gateway_binding_contract
assert_gateway_binding_contract(MyGatewayBinding("endpoint"))
```

## Capability richness: use your protocol's real features, never flatten them

The seam is NOT a lowest-common-denominator abstraction. A driver declares what
its transport can do by IMPLEMENTING optional capability protocols; the engine
calls only what a driver advertises (`isinstance`-detected, the same idiom the
south side uses for `Readable`/`Writable`). Implement the ones your transport
supports, omit the rest:

| Capability | Method | Implement when |
|---|---|---|
| `RequestReply` | `async reply(request, result)` | the transport carries a reply (a read value, an invoke result) |
| `EventMirroring` | `async mirror_event(slug, event, payload)` | you can push native events onto the wire |
| `PubSubOnly` | `is_pubsub_only = True` | the transport is fire-and-forget with NO reply channel |
| `Announces` | `async announce(gateway)` / `async reap()` | you announce the fleet on connect and reap on teardown (birth/death) |
| `QoSAware` | `quality_terms() -> tuple[str, ...]` | you read per-affordance quality options off the form |

The rule that matters: a driver that cannot carry an operation does not silently
flatten it. A `PubSubOnly` driver returns an explicit error for a reply-bearing
op, so a caller learns the bus cannot do it, rather than getting a dropped reply.

## Protocol-specific options ride in the form, namespaced

Each protocol's specific features live in the projected form as namespaced,
ignore-if-unknown vocabulary, exactly as W3C WoT does it (`htv:` for HTTP, `mqv:`
for MQTT). The driver WRITES its own terms and READS only its own; the engine
passes the form through opaquely and never enumerates a protocol's fields.

- MQTT (`mqv:`): `mqv:qos`, `mqv:retain`, `mqv:userProperties` (MQTT v5).
- MCP (`mcpv:`): `mcpv:kind` (`tool`/`resource`/`prompt`), MCP-specific annotations.

This is why a driver keeps its transport's full power: MQTT v5 user-properties,
DDS QoS, and MCP resources/prompts all survive projection because they ride in the
driver's own namespace, and none of them leaks into the engine.

## Ship it as a package (auto-discovered)

A third-party middleware ships as `thingctx-<bus>-gateway` and advertises its
driver under the `thingctx.gateways` entry-point group. On `pip install` it is
discoverable with zero change to thingctx, exactly as a south binding registers
under `thingctx.bindings`:

```toml
[project.entry-points."thingctx.gateways"]
mybus = "thingctx_mybus_gateway:MyGatewayBinding"
```

```python
from thingctx.gateways import discover_gateway_bindings
drivers = discover_gateway_bindings()      # {"mqtt": ..., "mcp": ..., "mybus": ...}
```

## Authorization holds on the bus

Because the engine dispatches every inbound request through the native
`ThingClient`, a client built with the authorization seam (`pdp`/`identity`)
enforces before the device is touched, for the bus too. An ungranted operation
becomes an error reply on the wire, never a silent bypass and never a crash of the
serve loop. See [AUTHZ.md](AUTHZ.md) and [IDENTITY.md](IDENTITY.md).

Two rules a guarded gateway follows so the bus edges are not holes:

- Events are NOT auto-mirrored to an open topic when a guard is set. A consumer
  must REQUEST a subscription on an authenticated topic; the gateway checks the
  `subscribeevent` grant for that caller and mirrors only to that caller's own
  stream. Without this, a gated event stream would leak to any subscriber.
- A per-caller guard is refused unless the broker binds connection identity (mTLS
  or per-client ACLs), attested with `broker_binds_identity=True`. A message-level
  token is not bound to the connection, so on an open broker a replayed token would
  be a confused deputy; the config-time gate makes that a control, not a caveat.

## Set-oriented sources: map them to events, not actions

A binding over a set-oriented or continuous source (a SQL database, a time-series
store, a sensor stream) fits the seam, but mind the impedance. A WoT property is
single-valued and an action is one request with one reply. So:

- A query that returns ONE value maps cleanly to a property `read` or an
  `invokeaction` with a single-object reply.
- A query that returns MANY rows does NOT fit `invokeaction` over a gateway: the
  whole result set becomes one reply frame on the bus (no streaming, no paging, no
  backpressure).
- The right home for a growing or unbounded result is an EVENT or observable
  property on the `Subscribable`/`EventMirroring` path, so rows flow as a stream
  the consumer pulls. Model a set-shaped affordance as `subscribeevent` (or
  `observeproperty`), not `invokeaction`.

The gateway does not hide this: a driver advertising `RequestReply` honestly
delivers one reply. Expose a set-oriented source through the event channel so the
gateway streams it, rather than collapsing a result set into a single reply.

## Reference drivers

- MQTT (`thingctx.gateways.builtin.mqtt.MqttGatewayBinding`): the request/reply +
  event-mirroring bus driver; implements `RequestReply`, `EventMirroring`,
  `QoSAware`.
- MCP (`thingctx.gateways.builtin.mcp.McpGatewayBinding`): a rich driver whose
  "topics" are MCP tools/resources/prompts; shows a middleware with
  protocol-specific constructs a plain bus lacks. A driver of a different shape
  (request/reply over UDP, a fire-and-forget bus) shows the seam is not
  MQTT-specific.
