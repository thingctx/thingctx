# Bindings

A *binding* teaches thingctx a transport. `http`, `mqtt`, `media`, and `local`
are the built-in bindings; each is only an implementation of one contract. You can
replace any built-in with your own, or add a new protocol (OPC UA, CoAP, Modbus),
by registering a binding. Bindings load in process: you `pip install` a library,
you do not run a server.

## Extending thingctx

thingctx is extended through a few small, published contracts. You depend on
thingctx as a library, implement a contract in your own package, register it at
run time, keep your package private if you wish, and never fork.

| Part            | Contract                          | Optional base | Register with                   | Discover         | Conformance |
| --------------- | --------------------------------- | ------------- | ------------------------------- | ---------------- | ----------- |
| Transport       | `ProtocolBinding`                 | `AuthMixin` (if it authenticates) | `BindingRegistry` / `bindings=` | `discover_bindings()` | `assert_binding_contract` |
| Authentication  | `CredentialProvider`              | `BaseAuth`    | `register_auth` / `extra_auth=` | `discover_auth()` | `assert_provider_contract` |
| Discovery       | `Registry` (`fetch`)              | none          | `ThingClient.from_registry`     | n/a (constructed)  | `assert_registry_contract` |
| Media engine    | `MediaBackend`                    | none          | `MediaBinding(backends=[...])`  | n/a (via binding)  | `assert_media_backend_contract` |

Every part is the same pattern: a `typing.Protocol` is the contract, `@implements`
checks it at import, an optional base saves boilerplate, and a conformance kit
asserts behaviour. One example covers all four:
[13_custom_stack.py](../examples/13_custom_stack.py). Auth is transport-neutral, so
one provider works across every transport (see [AUTH.md](AUTH.md)). The media engine
is a sub-contract *behind* the media binding, covered below. Discovery sources and
media engines are constructed explicitly rather than auto-discovered, because they
are not standalone installable transports; the entry-point path is for bindings and
auth providers.

**Multi-language.** These parts are in-process Python contracts: an extension is
a Python package loaded into the client, not a server you run. There is no
in-process path for a non-Python binding, by design, because that would require
an out-of-process boundary (the "a server per integration" shape this project
avoids). The language-neutral path is the Thing Description itself: a connector or
gateway written in any language can expose a WoT face and be described by a TD,
which thingctx then consumes as data. The TD is the cross-language contract; a
second-language *client* (rather than foreign-language plugins) is the way a non
Python runtime participates.

## The contract

A binding names a `scheme` and exposes an async `invoke`:

```python
class OpcUaBinding:
    scheme = "opc.tcp"                      # or: schemes = ("opc.tcp", "opc.https")

    async def invoke(self, action, form, arguments):
        ...
```

That is the whole required surface. A binding opts into more by adding the
matching method; the runtime checks for each at call time. The capability
protocols in `thingctx.bindings` name them:

| Capability        | Method                                   | Used by                |
| ----------------- | ---------------------------------------- | ---------------------- |
| `ContentRouted`   | `handles(form) -> bool`                  | routing (before scheme)|
| `Readable`        | `read(prop, form)`                       | `read_property`        |
| `Writable`        | `write(prop, form, value)`               | `write_property`       |
| `Subscribable`    | `subscribe(name, form)`                  | `subscribe`            |
| `MediaConsumer`   | `frames(action, form, arguments, *, track)` | `frames`            |
| `MediaPublisher`  | `publish(action, form, frames, arguments, *, track)` | `publish`  |
| `SecurityAware`   | `with_things(things)` / `with_security(thing)` | auth binding     |
| (closeable)       | `aclose()`                               | `client.aclose()`      |

`invoke`, `read`, `write`, `subscribe`, and `publish` are coroutines; `handles`
and `frames` are synchronous (`frames` returns an async iterator). Auth never
lives in a binding: implement `with_things` and resolve credentials through the
shared auth layer (see [AUTH.md](AUTH.md)).

## Registering a binding

```python
from thingctx import BindingRegistry, ThingClient

reg = BindingRegistry.default()   # http + local
reg.register(OpcUaBinding())     # add a new protocol
reg.register(MyHttpBinding())    # replace the built-in http binding

client = ThingClient(tds=[...], bindings=reg)
```

A form routes to exactly one binding, so you cannot have two bindings serving the
same plain scheme. `register` makes a binding the one that serves its scheme(s):
it removes any binding it fully covers and goes to the front, so registering a
binding for `http` *replaces* the built-in rather than stacking beside it. The one
exception is a content-routed binding (one with `handles`, like media): it claims
a form by content, so it can sit beside a plain binding on the same scheme and the
runtime picks it by the form's content first.

Passing a plain list still works (`ThingClient(tds=[...], bindings=[MyHttp()])`),
as does the original `bindings=` argument.

Enable the optional built-ins through the same registry:

```python
BindingRegistry.default(mqtt=True, media=True)
```

## Authenticating a binding

Auth never lives in a binding's transport logic. A binding that needs credentials
inherits `AuthMixin`, the **same** helper the built-in bindings use, so a custom
binding works exactly like a built-in. Resolve the owner's declared security into
neutral credential material, then map it onto your wire:

```python
from thingctx import AuthMixin, ProtocolBinding, implements

@implements(ProtocolBinding)
class OpcUaBinding(AuthMixin):
    scheme = "opc.tcp"

    def __init__(self, *, credentials=None, **kw):
        self._init_auth(credentials=credentials, auth=None, extra_auth=None, timeout=30.0)

    async def invoke(self, action, form, arguments):
        creds = await self._resolve_credentials(getattr(action, "thing_id", None), form)
        # map the neutral Credential material onto your transport here
        ...
```

`AuthMixin` gives you `with_things` / `with_security` (the `SecurityAware`
capability the runtime binds automatically) and `_resolve_credentials`. The secret
itself is supplied at runtime via `credentials=` (keyed by id, slug, or scheme),
never in the TD. Pass the `form` so a form's own security overrides the Thing's for
that affordance (WoT form-level security), letting one Thing use a different scheme
per plane. Custom auth *methods* are a separate, already-extensible part: register a
provider (see [AUTH.md](AUTH.md) and `examples/13_custom_stack.py`); a binding
consumes whatever providers resolve, transport-neutrally. A binding that needs no
auth (like the local one) simply does not inherit `AuthMixin`.

## Auto-discovery (opt in)

A package can advertise a binding through an entry point in the
`thingctx.bindings` group. Nothing loads it implicitly, because importing a binding
runs third-party code in process; call `discover_bindings()` to opt in:

```python
from thingctx.bindings import discover_bindings, BindingRegistry

reg = BindingRegistry.default().extend(discover_bindings())
```

## Checking the contract

The contract is a `typing.Protocol`, so it is checked at three levels, each
optional and additive:

1. **Static (your type checker).** thingctx ships a `py.typed` marker (PEP 561),
   so mypy/pyright verify your binding's method names, signatures, and
   `async`-ness against `ProtocolBinding` with no decorator and no inheritance.
2. **Definition time (`@implements`).** Opt-in sugar that fails at import if a
   contract member is missing, the early error an abstract base gives, without
   subclassing anything:

   ```python
   from thingctx import ProtocolBinding, implements

   @implements(ProtocolBinding)
   class OpcUaBinding:
       scheme = "opc.tcp"
       async def invoke(self, action, form, arguments): ...
   ```

3. **Behaviour (the conformance kit).** Run in your tests to assert what the type
   system cannot express, async/generator shape and runtime behaviour:

   ```python
   from thingctx.testing import assert_binding_contract, binding_capabilities

   assert_binding_contract(MyBinding())
   print(binding_capabilities(MyBinding()))
   ```

The built-in bindings pass this kit; a third-party binding that passes is driven by
the runtime the same way. `@implements` and `assert_media_backend_contract` work
the same way for the [media backend](#media-backends) contract.

## Media backends

The media plane has a second part *inside* the media binding. `MediaBinding` is
the transport (it routes media forms, resolves auth, and bridges blocking work to
the event loop); a `MediaBackend` is the engine it runs to decode or encode a
stream. PyAV (FFmpeg) and a page extractor ship as the built-in backends; a custom
one might wrap GStreamer or an industrial GigE camera engine.

A backend implements three synchronous methods. They run in a worker thread off
the event loop and stop when the passed `threading.Event` is set:

```python
import threading
from collections.abc import Iterator
from thingctx import Frame, MediaBackend   # both are top-level exports

class GstreamerBackend:
    def can_open(self, url: str, hint: dict) -> bool: ...
    def read(self, url, *, options: dict, stop: threading.Event) -> Iterator[Frame]: ...
    def write(self, frames, target, *, options: dict, stop: threading.Event) -> None: ...
```

`read` yields `Frame` objects (decode); `write` consumes a frame iterator and
pushes to `target` (encode). `options` carries the track (`video`/`audio`), any
form hint (codec, container), and a resolved auth plan when the Thing declares
security. The track and auth come from the binding, so a backend never touches the
auth layer.

Register backends by passing them to the binding; the first whose `can_open`
returns true serves the source:

```python
from thingctx import MediaBinding, ThingClient

client = ThingClient(tds=[...], bindings=[MediaBinding(backends=[GstreamerBackend()])])
```

Prove a backend before shipping it:

```python
from thingctx.testing import assert_media_backend_contract

assert_media_backend_contract(GstreamerBackend())
```

## Packaging

Distribute a binding as its own package named `thingctx-<protocol>` that depends
on `thingctx`. It stays private if you wish; it never needs to be contributed
back. The contract is versioned (`thingctx.bindings.CONTRACT_VERSION`) so a binding
can refuse an incompatible runtime.
