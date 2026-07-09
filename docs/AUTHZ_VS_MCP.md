# Authorization: WoT-derived vs MCP server-scoped

A precise, checkable comparison of what thingctx can enforce versus what MCP's
model can express. The claim is narrow and defensible: it is NOT "thingctx is more
secure." It is "thingctx enforces a finer-grained, resource-aware authorization
model that MCP's opaque-tool architecture cannot express." For actuation, that
difference is the difference between being able to represent a safety policy and
not.

## The one example that makes it concrete

A pump exposes a `setpoint` property: readable and writable. The policy an
operator needs:

    operators may READ the setpoint, but NOT WRITE it.

thingctx enforces this. The property's forms declare `op: [readproperty,
writeproperty]`, so the grant vocabulary contains two distinct tuples:

    (pump, setpoint, readproperty)
    (pump, setpoint, writeproperty)

A caller granted only the first: `read_property("pump.setpoint")` succeeds,
`write_property("pump.setpoint", ...)` is denied at the dispatch layer, before any
device is touched. This is a passing test, not a claim (see the read/write-split
tests in the identity module).

MCP cannot express this policy. In MCP, the pump would be one or two opaque
tools, say `get_setpoint` and `set_setpoint`. MCP's authorization (the OAuth 2.1
resource-server model) gates ACCESS TO THE MCP SERVER, not which tool, and
certainly not "read vs write of the same resource." MCP has no notion that
`get_setpoint` and `set_setpoint` are the read and write of one property; they are
just two functions. There is no resource, no operation, nothing for a policy to
bind to at that granularity. The finest MCP can say is "this caller may reach this
server." It cannot say "may read the setpoint but not write it."

## Why this is architectural, not a maturity gap

MCP flattens a system into a list of opaque tools. That flattening is what makes
MCP simple, and it is exactly what removes the information a fine-grained policy
needs. thingctx keeps the WoT resource model: Things have affordances
(properties/actions/events), affordances have operations (read/write/invoke/
observe/subscribe), declared in the TD. The authorization vocabulary is DERIVED
from that model. You cannot authorize per-operation if your interface has thrown
away the concept of an operation.

So it is not that MCP has not gotten around to per-operation authorization; it is
that MCP's architecture does not carry the operation, the same choice that makes
MCP's tool discovery coarse makes its authorization coarse.

## Where MCP is AHEAD (stated plainly)

- MCP's authorization spec is a ratified standard (OAuth 2.1 resource server,
  RFC 9728/8414/8707) with many vendor implementations and SDK support. thingctx's
  authz layer is newer. On ECOSYSTEM MATURITY and drop-in adoption, MCP wins today.
- MCP's model is simpler because the problem is smaller. Gating server access is
  easier than per-affordance enforcement, and for many uses coarse is enough.
- The crypto floor is the same. thingctx CONFORMS to the same OAuth/JWT
  resource-server validation MCP uses (it uses PyJWT, signature always verified).
  There is no "who validates tokens better" difference; thingctx uses MCP's own
  inbound model and adds the fine-grained layer on top.

## The honest framing

thingctx does not REPLACE MCP's authorization; it is the layer MCP's architecture
cannot be. An MCP agent authenticates to a thingctx gateway with an OAuth/Entra
token (MCP's model, which thingctx conforms to), and THEN thingctx enforces the
per-affordance, per-operation policy MCP cannot express, at the device boundary,
across every transport, driven by the device's own TD, and pluggable to any
AuthZEN PDP (OPA/Cedar/enterprise).

The bet, consistent with thingctx's whole direction: as agents move from calling
cloud APIs to actuating real fleets, operation-granular authorization on the
device stops being optional, and that is precisely where MCP's opaque-tool model
cannot follow. It is a CAPABILITY differentiation for actuation, not an adoption
one; MCP has the ecosystem, thingctx has the model that goes deeper where it
matters.

## Summary table

| Question | MCP | thingctx |
|----------|-----|----------|
| Inbound token validation (OAuth/JWT) | yes (standard) | yes (conforms; PyJWT) |
| Authorize access to the server | yes | n/a (it is a library, not a server) |
| Authorize a specific TOOL | not in the base spec | n/a (tools are the projection) |
| Read a property but not WRITE it | cannot express | enforced (distinct grants) |
| Per-operation across affordances | cannot express | enforced (read/write/invoke/observe/subscribe) |
| Enforce below the transport (multi-transport resource) | one transport per server | enforced at dispatch, all transports |
| Stream authorization with token-expiry cutoff | no per-delivery concept | enforced (per-delivery exp filter) |
| Policy derived from the resource's own description | no resource model | derived from the TD op arrays |
| Ecosystem maturity / vendor adoption | ahead | newer |
