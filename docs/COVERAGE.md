# WoT authorization coverage

Full coverage: every W3C WoT affordance and operation is enforced by the Policy
Enforcement Point at the thingctx dispatch layer, driven by grants derived from
the TD's own form `op` arrays.

| Affordance | Operation        | Enforced | PEP method            | Notes |
|------------|------------------|----------|-----------------------|-------|
| Property   | readproperty     | yes      | `read_property`       | request/response gate |
| Property   | writeproperty    | yes      | `write_property`      | request/response gate |
| Property   | observeproperty  | yes      | `subscribe`           | gate + per-delivery filter |
| Action     | invokeaction     | yes      | `invoke`              | request/response gate |
| Event      | subscribeevent   | yes      | `subscribe`           | gate + per-delivery filter |
| Media      | (read a signal)  | yes      | `frames`              | gate + per-delivery filter |
| Media      | (write a signal) | yes      | `publish`             | request/response gate |

## Two enforcement shapes

Request/response operations (read, write, invoke, media publish) are gated at the
call: authorize, then delegate, or deny before the device is touched.

Streaming operations (observe, subscribe, media frames) use two points:
1. Subscribe-time GATE: authorize the operation before the stream opens.
2. Per-delivery FILTER: re-authorize before each value (reading the token exp), stop when the
   token expires or the grant is revoked (a token expiring or a role being revoked mid-stream). The filter
   does not claw back already-delivered values; it cuts the stream forward, the
   correct semantics for a live-feed revocation.

## Multi-transport

The PEP is BELOW the transport choice (it fires before `ThingClient` resolves a
form), so a Thing with an HTTP form AND an MQTT form for the same affordance is
enforced once, whichever transport the call would route to. A broker/topic ACL
only sees the transport it brokers and would miss the other path; this PEP cannot.

## TD-derived and TD-closed

Grants are `(thing, affordance, op)` tuples built from each form's `op` array (with
the WoT default-op rule applied). A grant is valid only if its tuple is in that
closed vocabulary, so a wildcard can never grant an operation no form declares, and
a read-only property (`readOnly: true`, or a form listing only `readproperty`) can
never yield a `writeproperty` or `observeproperty` grant it did not declare.

## Not authorization, by design

The (identity -> grant) POLICY is deployment-specific and lives in the pluggable
`GrantSource` (a local policy, or an external OPA/Cedar/enterprise PDP via
AuthZEN). This package enforces the policy across every affordance; it does not
decide what the policy should be.
