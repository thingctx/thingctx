# Authorization

This document is the model for authorization in thingctx: what it decides, where
the check sits, and why the core stays dependency-free while still enforcing. For
authentication (validating a caller's token) see [AUTH.md](AUTH.md). For why an
opaque tool model cannot express this, see [AUTHZ_VS_MCP.md](AUTHZ_VS_MCP.md).

## Two seams: identity and authorization

thingctx keeps apart two things that are usually blurred:

- **Authentication (authn)** proves *who* the caller is. It validates a bearer
  token (signature, issuer, audience, expiry) into a claims dict. It needs
  crypto, so it lives in `thingctx.identity` behind the `authz` extra. In many
  deployments an upstream gateway does this and thingctx receives the claims.
- **Authorization (authz)** decides *what* that caller may do. It takes an
  already-validated identity and answers permit or deny for each
  `(thing, affordance, operation)`. It needs no crypto and no network, so it
  runs on the dependency-free core in `thingctx.authz`.

The line between them is the reason the core can enforce without any dependency:
core never validates a token, it consumes an identity that was validated
elsewhere. Authn is a claim about the caller; authz is a decision about an
operation. The module names say which is which: `identity` authenticates,
`authz` authorizes.

## Where the check sits

Authorization is native to `ThingClient`, not a wrapper around it. You pass a
`pdp` (the decider) and an `identity` (the validated claims) at construction, and
every device-reaching method authorizes the resolved
`(thing_id, affordance, op)` before it selects a binding:

```python
from thingctx import ThingClient
client = ThingClient(tds=[td], bindings=[...], pdp=pdp, identity=claims)
```

Because the check is on the dispatch methods (`invoke`, `read_property`,
`write_property`, `subscribe`, media `frames`/`publish`), and those sit *below*
the transport choice, a multi-transport Thing cannot be reached around the check.
`as_tools()` returns the authorized `invoke`, so an LLM loop or the MCP bridge
gets the guarded path, never a raw one. With no `pdp`, authorization is off and
the client behaves exactly as before, so the feature is opt-in and backward
compatible.

An already-built client can be re-bound for a specific caller with `guarded`,
which returns a native client sharing the same state, not a proxy:

```python
caller_client = client.guarded(pdp, identity=claims)
```

## The decision: fail-closed and TD-closed

- **Fail-closed.** No identity, no grant, an unknown claim, or an unreachable
  external PDP: deny. There is no path that falls through to allow.
- **TD-closed.** A grant is honored only if the requested
  `(thing, affordance, op)` is in the vocabulary derived from the TD's own form
  `op` arrays. A wildcard grant expands only over that closed set, so it can
  never permit an operation no form declares, and the op is intersected with the
  property's `readable`/`writable`, so a read-only property can never be written
  even by a compromised TD.

The canonical example: a `target_rpm` property that is readable and writable. A
policy grants an operator `readproperty` only. The read is allowed; the write is
denied before the device is touched; re-reading shows the value unchanged. That
read-yes / write-no distinction, per operation on one affordance, is the whole
point. See [`examples/14_authz.py`](../examples/14_authz.py).

## Streams need two enforcement points

A stream (observe, event, media) is not request/response, so authorize-once is
not enough:

1. A **subscribe-time gate** authorizes the operation before the stream opens; an
   ungranted caller never subscribes.
2. A **per-delivery filter** re-checks before each delivered value and stops the
   stream the moment the grant lapses (the token's `exp` passes the wall clock),
   closing the window an authorize-once gate would leave open.

## The PDP is pluggable (AuthZEN)

The default PDP is the lean local `PolicyDecisionPoint` over a `GrantSource` (a
role to grants map, `LocalPolicyGrantSource`, ships as the reference). The same
seam speaks the OpenID **AuthZEN** Authorization API 1.0 wire format, so you can
point at your own OPA, Cedar, or enterprise PDP without changing thingctx: the
PEP is identical, only *where* the decision is made moves. An external PDP that
is unreachable or returns a malformed response denies (fail-closed). See
[BRING_YOUR_OWN_PDP.md](BRING_YOUR_OWN_PDP.md).

## The full chain

```
caller's bearer token
   -> thingctx.identity guard.validate()   (authn: signature + iss + aud + exp)
   -> claims dict (the identity)
   -> ThingClient(pdp=, identity=claims)    (authz: permit/deny per operation)
   -> the device, over its own native credential
```

The device is driven with its OWN credential (from the TD's security scheme); the
caller's token never becomes the device's credential. Runnable end to end:
[`examples/15_authn_to_authz.py`](../examples/15_authn_to_authz.py).

## Coverage and threats

Every WoT operation is enforced across every transport; see
[COVERAGE.md](COVERAGE.md) for the affordance x operation matrix and
[THREAT_MODEL.md](THREAT_MODEL.md) for the threats each control answers.

## What is out of scope

- Validating the token itself: that is authn, in `thingctx.identity` (or your
  gateway). See [AUTH.md](AUTH.md).
- The policy *content* (which identity gets which grant): deployment-specific,
  supplied by the `GrantSource` or your external PDP. thingctx enforces a policy;
  it does not decide what your policy should be.
- Outbound device authentication (thingctx authenticating to the device): the
  core auth stack, see [AUTH.md](AUTH.md).
