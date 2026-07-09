# Discovery (proposed, not yet frozen)

Status: PROPOSAL. These three additions are additive and non-breaking, but they
are NOT part of the frozen binding contract yet. They extend the existing
capability pattern (opt-in methods a binding declares, detected by duck typing;
see docs/BINDINGS.md) to cover discovery.

Discovery here is NOT tool discovery (the gateway's job: which existing Thing
matches a live-state predicate). It is the step BEFORE a TD exists. It has two
folds, and closing the loop needs one runtime addition.

## The two folds, and the loop

1. Endpoint discovery: find that a server exists on a transport. "There is an
   OPC-UA server at opc.tcp://plc:4840." A protocol library already does this
   (rust-opcua find_servers/get_endpoints, a CoAP GET of /.well-known/core, an
   mDNS scan). This is a binding capability, wrapping the library's own call.
2. Thing discovery: browse that server and emit a WoT TD for it (OPC-UA
   variables to properties, methods to actions). The protocol library already
   browses (rust-opcua browse, asyncua get_children); the binding maps the
   browse result to a TD. This is a generator that legitimately runs IN the
   binding, because the binding is the one component already connected to the
   server and holding its nodeset.

The boundary that stays: a STANDALONE, curated, versioned nodeset-to-TD factory
(offline, published to a directory) is a separate producer's job, not the
client's. thingctx consumes a TD; it does not curate and publish them. A binding
that self-describes the live server it is connected to is a convenience the
binding is uniquely able to offer, not that factory. Same transform, different
posture: one is a published, version-pinned product, the other is an ephemeral
runtime describe of whatever live device the binding is talking to.

The loop, once the three pieces exist:

    endpoints = await binding.discover()        # fold 1a, wraps the library
    td = await binding.describe(endpoints[0])   # fold 2, wraps the library browse
    client.add_things([td])                     # the missing runtime path
    # the Thing is now projected to tools and gateway indexable, and drivable

Today the loop cannot close: TDs enter the runtime ONLY through the constructor
(parse_thing over the tds= list). ThingClient has no add_things, no refresh; the
Registry protocol (fetch() -> list[dict]) is consulted once at build time. So a
self-describing binding has nowhere to hand the TD it generated. That gap is the
one runtime addition below.

## Proposed capability: Discoverable (fold 1a)

A binding that can enumerate servers/endpoints on its transport declares:

    @runtime_checkable
    class Discoverable(Protocol):
        async def discover(self) -> list[Endpoint]: ...

`Endpoint` is a small record (url, plus optional protocol hints). Opt in: the
runtime checks for it by duck typing, exactly like Readable/Writable. The binding
wraps the library's discovery call and maps the result; it does not implement
discovery.

## Proposed capability: Describable (fold 2)

A binding that can browse an endpoint and emit a TD declares:

    @runtime_checkable
    class Describable(Protocol):
        async def describe(self, endpoint: Endpoint) -> dict: ...

Returns a WoT TD dict (@context, id, title, properties/actions, each with a form
whose href targets the endpoint + node id). The browse is the library's; the
mapping is the binding's. The returned dict is a normal TD the runtime consumes,
so nothing downstream (projection, gateway, trust) needs to know it was
generated rather than authored.

## Proposed runtime path: add_things (the missing register step)

    class ThingClient:
        def add_things(self, tds: list[dict]) -> None:
            """Register TDs into a live client: parse, append to the things,
            re-project to tools, re-bind declared security to bindings. The
            counterpart of the constructor's tds= for TDs that appear at runtime
            (a self-describing binding, a directory push)."""

This is the only piece that is not "wrap the library". It is a small runtime
change: parse the new TDs, extend self._things, re-run actions_to_tools, and
re-run the with_things/with_security auth binding over the new things. Costs to
handle explicitly:

- Re-projection: the tool specs and route map must be rebuilt (or extended) so
  the new Thing's actions become callable. Cheap; it is the same actions_to_tools
  path the constructor runs.
- Gateway re-index: if a gateway is in front, the new Thing must enter its
  searchable index. Additive.
- Auth re-binding: the new things must go through with_things/with_security so
  their declared security resolves. The existing loop already does this per
  binding; add_things re-runs it for the delta.

## Scope of the first cut

Do the simple version first: add_things appends and re-projects. Leave a live,
mutable directory (Things joining and leaving as devices come and go on the
network) for when a real fleet needs it; that is closer to the gateway/TDD and is
a larger design. Discoverable and Describable are opt-in, so a binding that does
neither is unaffected, and the frozen contract (scheme + async invoke, the eight
existing capabilities) does not change.
