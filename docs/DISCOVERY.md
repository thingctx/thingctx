# Discovery (partly shipped, the two binding folds still proposed)

Status: the runtime path (`ThingClient.add_things`) is SHIPPED. The two binding
capabilities that would generate a TD from a live server (`Discoverable`,
`Describable`) are PROPOSED, additive and non-breaking, and not part of the frozen
binding contract yet. They extend the existing capability pattern (opt-in methods
a binding declares, detected by duck typing; see docs/BINDINGS.md) to cover
discovery.

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

The loop, once the two binding folds exist:

    endpoints = await binding.discover()        # fold 1a, wraps the library (PROPOSED)
    td = await binding.describe(endpoints[0])   # fold 2, wraps the library browse (PROPOSED)
    client.add_things([td])                     # the runtime path (SHIPPED)
    # the Thing is now projected to tools and gateway indexable, and drivable

The runtime path already exists. `ThingClient.add_things` registers TDs into a
live client (see below), so a TD that appears at runtime has somewhere to land.
What is still PROPOSED is the two binding folds, `discover()` and `describe()`,
that would let a binding generate that TD from a live server. Until a binding
implements them, you call `add_things` with a TD you obtained some other way.

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

## The runtime path: add_things (SHIPPED)

    class ThingClient:
        def add_things(self, tds: list[dict], *, validate: bool = False) -> list[str]:
            """Register TDs into a live client: parse, append to the things,
            re-project to tools, re-bind declared security to bindings. Returns
            the ids of the added Things. The counterpart of the constructor's
            tds= for TDs that appear at runtime (a self-describing binding, a
            directory push)."""

`add_things` is the counterpart of the constructor's `tds=` for TDs that appear
at runtime. It parses the new TDs, extends `self._things`, re-runs
`actions_to_tools`, and re-runs the with_things/with_security auth binding over
the new things, returning the ids it added. What it handles:

- Re-projection: the tool specs and route map are rebuilt so the new Thing's
  actions become callable. It is the same actions_to_tools path the constructor
  runs.
- Gateway re-index: if a gateway is in front, the new Thing enters its searchable
  index. Additive.
- Auth re-binding: the new things go through with_things/with_security so their
  declared security resolves, the same per-binding loop the constructor runs, for
  the delta.

## Scope

The simple version shipped: `add_things` appends and re-projects. Still open is a
live, mutable directory (Things joining and leaving as devices come and go on the
network); that is closer to the gateway/TDD and is a larger design, left for when
a real fleet needs it. Discoverable and Describable remain opt-in proposals, so a
binding that does neither is unaffected, and the frozen contract (scheme + async
invoke, the existing capabilities) does not change.
