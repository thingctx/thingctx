# Bring your own Policy Decision Point

thingctx ships a lean, zero-dependency local PDP (`thingctx.authz`) as the
default. If your organization already runs an authorization engine, keep it as
the source of truth: thingctx is the Policy Enforcement Point, and it calls YOUR
PDP over the AuthZEN 1.0 standard. No thingctx change, no second policy engine to
trust.

## The seam

The PEP calls `pdp.decide(identity, request) -> Decision`. Anything implementing
that is a PDP. Two ship in the box:

- `PolicyDecisionPoint` + `LocalPolicyGrantSource` — the lean default. A role ->
  grant map, TD-closed, zero deps, works offline.
- `AuthZenPDP(base_url)` — delegates every decision to an external AuthZEN 1.0
  endpoint (`POST /access/v1/evaluation`). Fail-closed: unreachable or malformed
  => deny.

## Point at your OPA

OPA exposes decisions over HTTP. Front it with (or configure) an AuthZEN-shaped
decision endpoint, then:

    from thingctx import ThingClient
    from thingctx.authz.authzen import AuthZenPDP

    pdp = AuthZenPDP("https://opa.internal:8181", headers={"Authorization": "Bearer <opa-token>"})
    client = ThingClient(tds=[td], bindings=[...], pdp=pdp, identity=validated_claims)

Your Rego policy receives the AuthZEN request: `input.subject` (the caller's
claims), `input.action.name` (the WoT op: readproperty / writeproperty /
invokeaction / observeproperty / subscribeevent), `input.resource.id`
(`<thing>/<affordance>`), `input.context` (the form scheme). Return
`{"decision": true|false}`.

## Point at Cedar

Run a Cedar service behind an AuthZEN endpoint (or use a Cedar-backed AuthZEN
gateway). The mapping is the same: action.name is the op, resource.id is
`<thing>/<affordance>`, subject carries the claims. The same `AuthZenPDP` wiring
applies.

## The rule for every PDP

Whatever you point at, thingctx enforces the SAME WoT coverage (every affordance
and op, across every transport) and fails closed: a PDP that is unreachable or
returns anything other than an explicit permit denies the call. Your PDP decides
WHAT is allowed; thingctx guarantees the decision is enforced at the device
boundary.
