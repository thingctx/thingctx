# Security: control who does what, and serve a fleet

Authorize every operation on a `ThingClient`, and reserve a fleet over a bus
with the same control. For driving and parsing Things, see [USAGE.md](USAGE.md).

## Authorize the operation

Pass a `pdp` (the decider) and an `identity` (validated claims) when you build
the client. Every method that reaches a device authorizes the resolved
`(thing_id, affordance, op)` before it selects a binding:

```python
from thingctx import ThingClient
client = ThingClient(tds=[td], bindings=[...], pdp=pdp, identity=claims)
```

The check sits on the dispatch methods (`invoke`, `read_property`,
`write_property`, `subscribe`, media `frames`/`publish`), below the transport
choice, so a Thing on more than one transport cannot be reached around it. `as_tools()`
returns the authorized `invoke`. With no `pdp`, authorization is off.

Rebind an existing client for one caller with `guarded`:

```python
caller_client = client.guarded(pdp, identity=claims)
```

The decision is fail closed and closed to the TD. No identity, no grant, an unknown
claim, or an unreachable external PDP: deny. A grant is honored only if the
`(thing, affordance, op)` is in the vocabulary derived from the TD's own form
`op` arrays, and the op is intersected with the property's `readable`/`writable`,
so a property that is read only is never written. Worked example: a `target_rpm` property
grants `readproperty` only; the read is allowed, the write is denied before the
device is touched. Runnable: [`examples/14_authz.py`](../examples/14_authz.py).

Streams need two checks. A gate at subscribe time authorizes before the stream
opens; a filter reruns the check before each value and stops the stream
when the grant lapses (the token's `exp` passes the wall clock).

Every op (`readproperty`, `writeproperty`, `observeproperty`, `invokeaction`,
`subscribeevent`) is enforced, across every transport. End to end:
[`examples/15_authn_to_authz.py`](../examples/15_authn_to_authz.py).

## Identity

Four distinct identities, two hops, none spans both.

| Identity | Hop | Direction | Held by | Provided by | Validated by |
|---|---|---|---|---|---|
| A: caller | 1 | agent → gateway | the agent | an external IdP (Entra, Cloudflare, your issuer) | the gateway (`thingctx.identity` guard) |
| Gateway's own | 1 | (the gateway itself) | thingctx | its own broker registration | the broker / IdP |
| D: device credential | 2 | gateway → device | thingctx (its credential store) | the device's TD security scheme + the operator's secret | the device |
| Device up | 2 | device → gateway | the device | not modeled | not modeled |

The caller's identity (A) never becomes the device credential (D). On permit,
thingctx reaches the device with the device's own credential, resolved by
`AuthMixin` from its declared schemes and the operator's secrets. The caller
token never travels to the device.

## Bring your own PDP

The PEP calls `pdp.decide(identity, request) -> Decision`. Anything implementing
that is a PDP. Two ship:

- `PolicyDecisionPoint` + `LocalPolicyGrantSource`: the lean default. A role to
  grant map, closed to the TD, zero deps, works offline.
- `AuthZenPDP(base_url)`: delegates every decision to an external AuthZEN 1.0
  endpoint (`POST /access/v1/evaluation`). Fail closed: unreachable or malformed
  denies.

Point at OPA. Front it with (or configure) an AuthZEN shaped decision endpoint,
then:

```python
from thingctx import ThingClient
from thingctx.authz.authzen import AuthZenPDP

pdp = AuthZenPDP("https://opa.internal:8181", headers={"Authorization": "Bearer <opa-token>"})
client = ThingClient(tds=[td], bindings=[...], pdp=pdp, identity=validated_claims)
```

Your policy receives the AuthZEN request: `input.subject` (the caller's claims),
`input.action.name` (the WoT op: readproperty / writeproperty / invokeaction /
observeproperty / subscribeevent), `input.resource.id` (`<thing>/<affordance>`),
`input.context` (the form scheme). Return `{"decision": true|false}`.

Point at Cedar behind an AuthZEN endpoint. The mapping is the same: `action.name`
is the op, `resource.id` is `<thing>/<affordance>`, subject carries the claims,
same `AuthZenPDP` wiring.

Whatever you point at, thingctx enforces the same coverage and fails closed: a
PDP that is unreachable or returns anything other than an explicit permit denies.

## Serve a fleet

A `Gateway` holds a `ThingClient` to reach the devices and a `GatewayBinding` to
serve them on the bus:

```python
from thingctx import ThingClient
from thingctx.gateways import Gateway
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding

client = ThingClient(tds=[...], bindings=[...])              # reach the devices
gateway = Gateway(client, MqttGatewayBinding("broker:1883"))  # serve them on the bus
await gateway.start()
```

A driver hands the engine a neutral `ServeRequest`, the engine resolves it
(`gateway.dispatch(req)`), and the result goes back. The driver owns addressing,
the op to wire mapping, encode/decode, and the transport's features.

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

Check it against the conformance kit:

```python
from thingctx.testing import assert_gateway_binding_contract
assert_gateway_binding_contract(MyGatewayBinding("endpoint"))
```

A driver declares what its transport can do by implementing optional capability
protocols; the engine calls only what a driver advertises (detected with `isinstance`).

| Capability | Method | Implement when |
|---|---|---|
| `RequestReply` | `async reply(request, result)` | the transport carries a reply (a read value, an invoke result) |
| `EventMirroring` | `async mirror_event(slug, event, payload)` | you can push native events onto the wire |
| `PubSubOnly` | `is_pubsub_only = True` | the transport is fire and forget with NO reply channel |
| `Announces` | `async announce(gateway)` / `async reap()` | you announce the fleet on connect and reap on teardown (birth/death) |
| `QoSAware` | `quality_terms() -> tuple[str, ...]` | you read per affordance quality options off the form |

Protocol specific options ride in the projected form as namespaced,
ignore if unknown vocabulary. The driver writes its own terms and reads only its
own; the engine passes the form through opaquely.

- MQTT (`mqv:`): `mqv:qos`, `mqv:retain`, `mqv:userProperties` (MQTT v5).
- MCP (`mcpv:`): `mcpv:kind` (`tool`/`resource`/`prompt`), MCP specific annotations.

Ship a third party middleware as `thingctx-<bus>-gateway`, advertising its driver
under the `thingctx.gateways` entry-point group:

```toml
[project.entry-points."thingctx.gateways"]
mybus = "thingctx_mybus_gateway:MyGatewayBinding"
```

```python
from thingctx.gateways import discover_gateway_bindings
drivers = discover_gateway_bindings()      # {"mqtt": ..., "mcp": ..., "mybus": ...}
```

### Authorization holds on the bus

A client built with `pdp`/`identity` enforces for the bus too: the engine
dispatches every inbound request through the native `ThingClient`. An ungranted
operation becomes an error reply on the wire, never a silent bypass. Two rules a
guarded gateway follows:

- Events are not mirrored automatically to an open topic. A consumer must request a
  subscription on an authenticated topic; the gateway checks the `subscribeevent`
  grant for that caller and mirrors only to that caller's own stream, which
  inherits the per delivery stream reauthorization.
- A per caller guard is refused unless the broker binds connection identity (mTLS
  or per client ACLs), attested with `broker_binds_identity=True`. A message level
  token is not bound to the connection, so on an open broker a replayed token
  would be a confused deputy.

Reference drivers:

- MQTT (`thingctx.gateways.builtin.mqtt.MqttGatewayBinding`): request/reply plus
  event mirroring; implements `RequestReply`, `EventMirroring`, `QoSAware`.
- MCP (`thingctx.gateways.builtin.mcp.McpGatewayBinding`): a driver whose "topics"
  are MCP tools/resources/prompts.
