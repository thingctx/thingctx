# Identity: who is who, and which identity is used where

This document is the map of every identity in a thingctx deployment: who holds it,
who provides it, who validates it, and where it flows. It sits above
[AUTH.md](AUTH.md) (authenticating outbound to a device), [AUTHZ.md](AUTHZ.md)
(deciding what a caller may do), and [MIDDLEWARE.md](MIDDLEWARE.md) (serving a
fleet over a bus). Read it to answer "which identity reaches the device" and "what
does a bus consumer see."

## Three concepts, kept separate

- **Authentication (authn)** proves WHO a caller is. `thingctx.identity`'s guard
  validates a bearer token into a claims dict.
- **Authorization (authz)** decides WHAT that caller may do. `thingctx.authz`'s PDP
  decides on the claims plus a requested `(thing, affordance, op)`.
- **Identity** is the claims dict that flows from authn to authz. It is the only
  currency between them.

The module names say which is which: `identity` authenticates, `authz` authorizes.

## The four identities, and two hops

A gateway sits between a caller and a device. There are two hops and four distinct
identities. No single identity spans both hops.

```
   AGENT / CONSUMER          THE GATEWAY (thingctx)              THE DEVICE
        │  ── HOP 1 (north) ──►    │    ── HOP 2 (south) ──►         │
   [A: caller identity]      [gateway's own]              [D: device credential]
```

| Identity | Hop | Direction | Held by | Provided by | Validated by |
|---|---|---|---|---|---|
| A: caller | 1 | agent → gateway | the agent | an external IdP (Entra, Cloudflare, your issuer) | the gateway (`thingctx.identity` guard) |
| Gateway's own | 1 | (the gateway itself) | thingctx | its own broker registration | the broker / IdP |
| D: device credential | 2 | gateway → device | thingctx (its credential store) | the device's TD security scheme + the operator's secret | the device |
| Device-upward | 2 | device → gateway | the device | not modeled | not modeled |

- **A (caller):** the agent gets a token from its IdP before touching the gateway.
  The gateway validates it into claims and authorizes the operation as that caller.
- **D (device credential):** thingctx authenticates TO the device with the device's
  own credential (from its TD), resolved by `AuthMixin`. This is not the caller's
  identity.

## The invariant: the planes stay separate

The caller's identity (A) NEVER becomes the device credential (D). The claims flow
authn → PDP → permit/deny and stop; on permit, thingctx reaches the device with the
device's OWN credential. This is foreclosed in code, not by convention: the south
`AuthMixin` reads only the device's declared schemes and the operator's secrets; no
path turns caller claims into device auth. So a caller token never travels south to
the device, an independent review confirmed this by construction.

On-behalf-of (forwarding the caller's identity down to the device so the device
enforces per-user policy) is a DELIBERATE non-goal. It would put the caller token on
the device wire, which the separation above prevents. If you need it, it is a
conscious design departure, not a default.

Device-upward identity (the device attesting who IT is to the gateway) is not
modeled today; the gateway trusts the endpoint it was configured with.

## Per-caller vs server-level identity on the bus

When thingctx serves a fleet over a bus (the gateway), authorization runs against
one of two identities:

- **Server-level (default):** with no guard, the gateway authorizes every inbound
  request as the identity it was built with. Simple; the whole face is one principal
  to the bus.
- **Per-caller:** with a guard set (the `Authenticates` capability), the driver
  validates each message's token and the engine re-authorizes as THAT caller (via
  `client.guarded(pdp, identity=claims)`), so a granted caller and an ungranted one
  get different decisions.

The bus edges have two enforced controls, so a guarded gateway is not falsely
trusted:

1. **Connection binding (confused-deputy guardrail).** A message-level token is not
   bound to the broker connection that delivered it. On an open or shared-connection
   broker, a sender could replay another party's still-valid token. So a per-caller
   guard is REFUSED unless the broker is attested to bind connection identity (mTLS
   client cert or per-client ACLs), passed as `broker_binds_identity=True`. This is
   a config-time control, not a docstring.
2. **Event authorization.** A guarded gateway does NOT auto-mirror events to an open
   topic. A consumer must request a subscription on an authenticated topic; the
   gateway checks the `subscribeevent` grant for that caller and mirrors only to
   that caller's own stream, which also inherits the per-delivery stream
   re-authorization (the stream stops when the token expires).

## What a bus consumer and a device see (worked example)

A gateway on MQTT (say an Event Grid / Event Hub broker), an app consuming from the
bus, and an OPC-UA device driven on the south side:

- **App command → device:** the broker sees the gateway publishing; the device sees
  the gateway's OWN OPC-UA credential (D), never the app's identity. With a guard,
  the gateway authorizes the command as the app (A); without one, as itself.
- **Device event → app:** the broker sees the gateway publishing; the app receives
  the payload named by topic. The device's identity does not propagate upward (no
  attestation). With a guard, only an app granted `subscribeevent` receives the
  stream.

So to the broker, relayed traffic is one principal: the gateway's. The caller's
identity stops at the gateway (no on-behalf-of), and the device's identity does not
travel up (no attestation). Each concern has one home.

## Reference

- The guard (authn): [AUTH.md](AUTH.md), `thingctx.identity`.
- The decision (authz): [AUTHZ.md](AUTHZ.md), `thingctx.authz`. Every claim there is
  backed by a test; see [CLAIMS.md](CLAIMS.md).
- Serving a fleet over a bus: [MIDDLEWARE.md](MIDDLEWARE.md).
