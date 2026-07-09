# The MQTT gateway: one bus for a whole fleet

thingctx drives a fleet from one client by adding a binding per protocol. That
unifies protocols *inside one process*. Sometimes you need the other kind of
unification: a shared MQTT bus where *many independent consumers* (agents,
dashboards, other Things) all see the fleet as MQTT, no matter what each Thing
really speaks. That needs a process in the middle. The `MqttGateway` is that
process, and it is a sibling of the MCP bridge: the bridge exposes a fleet to MCP
clients, the gateway exposes a fleet to an MQTT bus.

## The line: binding vs gateway

- A **binding** unifies protocols for **one client**. Nothing runs in the middle;
  the client speaks every protocol natively. The "document, not a server" thesis
  is intact. This is the common case and needs no gateway.
- A **gateway** unifies protocols on a **shared bus for everyone**. A bus is
  shared, out-of-process state, so a client-side binding cannot make a remote CoAP
  device appear on a broker for *other* consumers. Something must bridge the wire.

One gateway serves a fleet; it is generic infrastructure run once, like the
broker, not one server per Thing.

## What it does

You hand `MqttGateway` a `ThingClient` built over the Things' real (native)
transports. On `start()` it, per Thing:

1. **Projects an mqtt-faced TD**: copies the Thing's TD, rewrites every form's
   href to `mqtt://<broker>/tc/<slug>/...`, and replaces the native security with
   the bus's own (the native secret stays in the gateway, never on the bus). It
   retains this TD at `tc/<slug>/td` so consumers can discover the fleet.
2. **Bridges actions** (bus to native): subscribes `tc/<slug>/actions/<name>`; on
   a message it calls native `invoke` and publishes the result to
   `tc/<slug>/actions/<name>/reply`.
3. **Bridges properties**: a read is request/reply; a write is a message carrying
   `{"value": ...}`.
4. **Mirrors events** (native to bus): holds a long-lived native `subscribe()` for
   each event and republishes each payload to `tc/<slug>/events/<name>`. This is
   what makes the bus reactive: a CoAP observe or an HTTP SSE stream becomes an
   MQTT topic anyone can watch.

## The consumer drives it with a stock binding

The reply shape is exactly what `MqttBinding` already expects: publish input to a
topic, await the reply on `<topic>/reply`. So a consumer needs no special code, it
reads the projected TD and drives it like any other MQTT Thing.

## Topic convention

```
tc/<slug>/actions/<name>          request: publish input here
tc/<slug>/actions/<name>/reply    reply:   the gateway publishes the result
tc/<slug>/props/<name>            property read (req/reply) or write ({"value":...})
tc/<slug>/props/<name>/reply      property read reply
tc/<slug>/events/<name>           the gateway republishes native events here
tc/<slug>/td                      retained: the projected mqtt-faced TD
```

`tc/+/td` retained messages give any consumer the whole fleet's descriptions with
one subscription, a discovery surface for free.

## Two auth planes, kept apart

The gateway has native auth (the real device secret, resolved inside the gateway
from the TD's security scheme) and bus auth (who may drive a topic, the broker's
job). They never mix: the native token is never put on the bus. If the gateway's
`ThingClient` was built with the authorization seam (`pdp`/`identity`), every
bridged call is authorized before the native device is touched, so the bus is not
a way around authorization, an ungranted write is refused at the gateway, and only
a reply carrying the denial goes back on the bus.

## Multi-form: the no-gateway case

If a Thing already speaks MQTT *in addition* to its native protocol, it needs no
gateway. WoT lets one affordance carry multiple forms:

```json
"setSpeed": { "forms": [
  { "href": "https://pump.local/speed", "htv:methodName": "POST" },
  { "href": "mqtt://bus/tc/pump/actions/setSpeed" }
] }
```

`ThingClient(prefer=("mqtt",))` picks the mqtt form; an http-preferring consumer
picks the http one. This unifies at the description level with zero
infrastructure. The gateway is for the common case where a device speaks only its
native protocol and cannot offer an mqtt form itself.

## Honest costs

- The gateway is a process you run. That is the price of unifying *heterogeneous*
  protocols on a shared bus; you cannot translate CoAP-to-MQTT with nothing in the
  middle. The integration is still the TD; the gateway is generic infrastructure
  run once per fleet.
- Latency: every call is consumer to broker to gateway to native and back. Fine
  for control and state. Media never transits the bus, continuous frames stay
  referenced (see the media binding); the gateway bridges control, state, and
  events only.
- Correlation: concurrent calls to one action share a reply topic. For high
  concurrency, carry a correlation id in the payload and match it on the reply.

## Scope

This is a thin reference gateway. A stateful, multi-tenant, policy-driven bus
(retained-state caches, rules engines, bus-auth policy) is a larger system and is
out of scope for the lean client; the reference gateway is meant to be small,
clear, and easy to run once per fleet.
