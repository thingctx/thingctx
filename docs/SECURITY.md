# Security: control who does what, and serve a fleet

Authorize every operation on a `ThingClient`, and reserve a fleet over a bus
with the same control. For driving and parsing Things, see [USAGE.md](USAGE.md).
Transport level hardening, including the SSRF controls, is in
[Harden the transport](#harden-the-transport) at the end.

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

## Bound the arguments

A grant can bound what a call sends. `limited` builds a grant whose fourth element
is a JSON Schema: an `invokeaction` permits only an action input that validates
against it, a `writeproperty` only a value that does.

```python
from thingctx.authz import LocalPolicyGrantSource, limited

arm = "urn:demo:arm"
box = {"properties": {"x": {"type": "number", "minimum": -0.65, "maximum": 0.65}}}
grants = LocalPolicyGrantSource({"operator": {limited(arm, "move_to", "invokeaction", box)}})
```

The PDP checks the input after the TD's `const` and `default` values are filled,
so it sees what the device will receive, and after the client has refused input
the TD's own schema does not allow, so a bound only ever sees the declared types. An unlimited grant on the same operation
still permits anything; when every covering grant is limited, the arguments must
satisfy one of them, and a refusal names the first violation. A limited grant never
permits an operation that carries no arguments, such as a read. Checking needs the
`validate` extra; without it a limited grant denies. It also denies NaN and the
infinities, and a schema that cannot be applied. `limited` refuses a numeric bound
without a numeric `type` (`number` or `integer`) beside it, because the bound
ignores a value that is not a number: `"99"` would pass `{"maximum": 4}`. It also
refuses a `$ref` or `$dynamicRef`, which could hide such a bound; write the bound
in place.

The check runs in the client, so it holds whether a caller reaches the device
directly or through a gateway, which serves through a client too. A broker cannot
enforce it, because it never reads a payload: `acl_file` compiles a limited grant to
the same rules as an unlimited one.

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
observeproperty / subscribeevent), `input.action.properties.arguments` (the action
input or written value, when the call carries one), `input.resource.id`
(`<thing>/<affordance>`), `input.context` (the form scheme). Return
`{"decision": true|false}`.

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

client = ThingClient(tds=[...], bindings=[...])  # reach the devices
gateway = Gateway(client, MqttGatewayBinding("broker:1883"))  # serve them on the bus
await gateway.start()
```

A driver hands the engine a neutral `ServeRequest`, the engine resolves it
(`gateway.dispatch(req)`), and the result goes back. The driver owns addressing,
the op to wire mapping, encode/decode, and the transport's features.

A gateway binding is a class satisfying `GatewayBinding`:

```python
class MyGatewayBinding:
    scheme = "mybus"  # the re-served form scheme

    def project_forms(self, thing, affordance, op):
        # return this driver's re-served form(s) for one (affordance, op),
        # carrying your OWN namespaced vocabulary. Return [] for an op your
        # transport cannot carry, so the projected TD is honest.
        ...

    async def serve(self, gateway):
        # connect; for each inbound message build a ServeRequest and await
        # gateway.dispatch(req); deliver the result on your wire.
        ...

    async def aclose(self): ...
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
| `DeclaresSecurity` | `security_definitions() -> dict[str, dict]` | callers authenticate on your wire, and the projected TDs should say how |
| `DeclaresUriVariables` | `uri_variables() -> dict[str, dict]` | your forms carry a URI template variable you declare once for every Thing |

Protocol specific options ride in the projected form as namespaced,
ignore if unknown vocabulary. The driver writes its own terms and reads only its
own; the engine passes the form through opaquely.

- MQTT (`mqv:`): `mqv:qos`, `mqv:retain`.
- MCP (`mcpv:`): `mcpv:kind` (`tool`/`resource`/`prompt`), MCP specific annotations.

Ship a third party middleware as `thingctx-<bus>-gateway`, advertising its driver
under the `thingctx.gateways` entry-point group:

```toml
[project.entry-points."thingctx.gateways"]
mybus = "thingctx_mybus_gateway:MyGatewayBinding"
```

```python
from thingctx.gateways import discover_gateway_bindings

drivers = discover_gateway_bindings()  # {"mqtt": ..., "mcp": ..., "mybus": ...}
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
- A per caller guard is refused unless the broker keeps each login to its own
  topic level, attested with `broker_binds_identity=True`. The gateway checks that
  a token's `sub` is the User Name its request topic ends in, and only the broker
  can make that name the sender's, so on an open broker a replayed token would be a
  confused deputy.

A guarded MQTT gateway's projected Thing Descriptions say how a caller
authenticates: a header bearer scheme named `authorization`, which on an mqtt form
is the MQTT 5 User Property of that name, sent with every request; a thingctx client
reads the scheme and sends the token with each request. Pass the broker's own
scheme as `bus_security`, such as `{"bus_basic": {"scheme": "basic"}}`, and the
descriptions declare it too, so a client authenticates the connection as well. A
guarded gateway needs that basic scheme, because every request topic ends in the
sender's MQTT User Name:

```
<prefix>/<thing>/actions/<action>/<user>
<prefix>/<thing>/props/<property>/read/<user>
<prefix>/<thing>/props/<property>/write/<user>
<prefix>/<thing>/props/<property>/observe/<user>
<prefix>/<thing>/events/<event>/subscribe/<user>
```

Each topic names the whole operation, so a broker rule can allow a property's read
without its write. A write carries its value as `{"value": ...}`.

The descriptions declare that level once, as a Thing level URI variable typed
`tc:MqttUserName`; a thingctx client fills it with the user name of its basic
credential. The gateway answers on `<request>/reply`, streams an event or an
observed property's changes on `<request>/stream` after an authorized request, and
runs a request only when the token's `sub` is the User Name the topic ends in. A
request carrying another login's token gets the answer a request with no token gets:
`{"error": "authentication required", "denied": true}`.

The broker holds the other half: whoever can read a request topic can read and
replay its token. `acl_file` writes Mosquitto rules that let only gateway users read
requests, and let each caller publish requests only under its own User Name and read
only its own replies and streams:

```python
from pathlib import Path

from thingctx.gateways.mosquitto import acl_file

Path("acl").write_text(acl_file(gateways=["gw-1"], callers=["alice", "zone-rule"]))
```

Given each caller's grants, the `(thing, affordance, op)` sets the gateway's policy
decision point holds, keyed by user name, the rules allow that caller exactly those
operations:

```python
grants = {"alice": {("urn:demo:pump", "stop", "invokeaction")}}
Path("acl").write_text(acl_file(gateways=["gw-1"], callers=["alice"], grants=grants))
```

Alice may then publish only `tc/pump/actions/stop/alice` and read its reply; the
broker refuses any other request of hers before the gateway sees it. The gateway
still decides every request it receives: the broker sees no payload, device state
or approval, and the gateway alone keeps the record. A grant of `*` becomes a
wildcard level; an operation the gateway does not carry over MQTT adds no rule. A
caller not named in `grants` may send any request under its own User Name, and the
gateway alone decides.

A gateway login reads every request, token included, for the Things it may serve,
and writes their replies, streams and retained descriptions. Given the Things each
gateway serves, by id, the rules limit its login to them, so one gateway can neither
read another's requests nor answer them:

```python
serves = {"gw-door": ["urn:demo:door"], "gw-agents": ["urn:demo:agent:watcher"]}
acl_file(gateways=["gw-door", "gw-agents"], callers=["alice"], serves=serves)
```

A lone gateway not named in `serves` covers every Thing under the prefix. With more
than one gateway, `acl_file` refuses to write rules unless `serves` names each, so
no gateway login can read every caller's requests and the tokens in them.

The rules name each user, so to add a caller, change a grant or move a Thing, write
the file again and reload the broker. A user name or affordance name may not contain
whitespace, `/`, `+`, `#` or `?`, and a user name may not be `reply`.

To run a guarded gateway on a locked broker:

- Run Mosquitto with `allow_anonymous false`, a `password_file` and the `acl_file`
  output, then attest `broker_binds_identity=True`.
- Serve MQTT over TLS, so no password, token or reply crosses the network in the
  clear.
- Give each identity one login whose user name is its token's `sub`.
- Log the gateway in by overriding `MqttGatewayBinding.configure`
  (`paho.username_pw_set(...)`) and build it with
  `bus_security={"bus_basic": {"scheme": "basic"}}`.
- Build each caller's `MqttBinding` with `credentials={"bus_basic": (user,
  password), "bus_bearer": token}`, keyed by scheme name.

The level binds the token's `sub` alone, not its issuer, so run one issuer per
guarded gateway, or issuers whose subjects cannot collide.

Reference drivers:

- MQTT (`thingctx.gateways.builtin.mqtt.MqttGatewayBinding`): request/reply plus
  event mirroring; implements `RequestReply`, `EventMirroring`, `QoSAware`,
  `Authenticates`, `DeclaresSecurity` and `DeclaresUriVariables`.
- MCP (`thingctx.gateways.builtin.mcp.McpGatewayBinding`): a driver whose "topics"
  are MCP tools/resources/prompts.

## Close the paths around thingctx

Authorization decides who may call an operation through thingctx; it cannot stop a
client that reaches the device another way. If the broker accepts anonymous
publishes, or the device's own stream or API is open on the network, anything on
that network can drive or watch the device, unauthorized and unrecorded. A Thing
Description that declares `nosec` is often the sign.

Make thingctx the only way in: per client credentials and topic ACLs on the broker
(Mosquitto's `password_file` and `acl_file`), authentication on the device's own
endpoints, and a network that routes no other client to them. The guarded gateway
applies the same rule on the bus: a per caller guard needs a broker that keeps each
login to its own topic level.

## Harden the transport

Authorization decides who may call an operation. These controls decide what the
process may do once a call is allowed. They are off or permissive by default,
because the common case is a developer driving a device on their own LAN, and a
default that blocks `192.168.1.50` would break it. Turn them on when the Things
you load are not fully trusted, or when thingctx is reachable by anyone but you.

### Put MQTT on TLS

Use TLS whenever the broker is not on loopback: plain MQTT carries the password, the
token in each request and every reply in the clear. Name the broker
`mqtts://host[:port]`, port 8883 by default, in a form's href or as a gateway's
`broker`, or pass `tls=MqttTls(ca_certs=...)` to `MqttBinding` or
`MqttGatewayBinding`. Either verifies the broker's certificate, against the system's
CA store unless `ca_certs` names another CA, and its hostname. A gateway on TLS
writes `mqtts://` in the forms it projects, so its callers connect over TLS too.

### Refuse requests to private and metadata addresses

`THINGCTX_BLOCK_PRIVATE=1` refuses outbound HTTP to loopback, private,
link local, and cloud metadata addresses.

A Thing Description is a document, and its `href` can point anywhere. A response
chain can follow a URL that came back in a response. So an untrusted or
attacker influenced TD can aim the client at `http://169.254.169.254/`, the
cloud metadata endpoint that hands out instance credentials, or at an internal
service whose security model is "only something inside can reach me". That is
server side request forgery: the request is made by the process that has the
access, on behalf of whoever supplied the URL.

Set it when you load TDs you did not write, and whenever thingctx runs as a
hosted or shared service:

```bash
THINGCTX_BLOCK_PRIVATE=1 thingctx-mcp ./tds/
```

The streamable HTTP server turns this on for you on a non loopback bind, because
a public bind is where SSRF bites and the laptop default (off, so LAN devices
stay reachable) is wrong there. A trusted LAN gateway that must reach private
hosts on a public bind sets `THINGCTX_BLOCK_PRIVATE=0` explicitly to keep it off;
an explicit value is never overridden.

The check resolves the host, validates every resolved address, then connects to
that validated address, so a name that resolves to a public address on the first
lookup and a private one at connect time (DNS rebinding) does not slip through.
Encoded forms are canonicalized first: `2130706433`, `0x7f000001`, and the short
and IPv4 mapped forms are all `127.0.0.1` and all blocked.

The guarantee is that the check runs on every HTTP path when enabled, including
every response chain follow mode. It is not a claim that SSRF is impossible.

### Require inbound auth before binding a public interface

`THINGCTX_REQUIRE_AUTH=1` turns the non loopback bind warning into a startup
error. The streamable HTTP MCP endpoint performs no inbound authentication of
its own, so binding it to `0.0.0.0` publishes every tool to the network. With
this set, thingctx refuses to start rather than serve an open surface. Put an
authenticating proxy in front and keep the flag on.

### Bound what a call can consume

Each of these caps a resource an untrusted document could otherwise exhaust.

| Variable | Bounds |
| --- | --- |
| `THINGCTX_MAX_TD_BYTES` | Size of a fetched Thing Description |
| `THINGCTX_MAX_DOWNLOAD_BYTES` | Size of a media or file download |
| `THINGCTX_MAX_CHAIN_HOPS` | Follow steps in a response chain |
| `THINGCTX_FS_MAX_BYTES` | Bytes a local filesystem handler may read or write |

### Confine the filesystem

`THINGCTX_FS_ROOT` bounds the sandboxed filesystem handler to one directory; the
handler refuses every call until it is set. `THINGCTX_DOWNLOAD_DIR` does the same
for downloads. With neither set, a relative path that traverses out of the
working directory is refused; an absolute path is treated as operator intent and
allowed, so set a root when the caller is not you.

### Keep secrets out of swap

`THINGCTX_MLOCK_SECRETS=1` asks the OS to pin secret buffers in memory so they
are not written to swap. Best effort: it fails quietly where the platform or the
process limits disallow it, and it does nothing about a process that can already
read this process's memory.

### What none of this defeats

Root on the same machine, another process running as you, and physical access.
These controls raise the cost of reaching your network and your files from a
document you did not write. They are not a boundary against a local adversary.
