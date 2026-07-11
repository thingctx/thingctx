# Contributing to thingctx

thingctx is small on purpose, and built so you can add real value in one
focused pull request. Two contributions matter most:

## Add a transport (a binding)

A binding teaches thingctx to speak one transport. The built-in bindings are
Local, HTTP, MQTT, and media. CoAP, WebSocket, OPC-UA, Modbus, gRPC, serial ,
each is one self-contained class against the `ProtocolBinding` contract that no
one has to coordinate on:

```python
class CoapBinding:
    schemes = ("coap", "coaps")
    async def invoke(self, action, form, arguments): ...
    async def read(self, prop, form): ...
    async def write(self, prop, form, value): ...
```

You do not have to contribute it back: register it with `BindingRegistry` and
pass `ThingClient(bindings=...)` from your own package. To add one here, drop it
under `src/thingctx/bindings/builtin/`, prove it with the conformance kit
(`thingctx.testing.assert_binding_contract`), add a test, and a line in the
README. See the bindings section in `docs/USAGE.md`. A new transport reaches every device that speaks
it, for a small amount of code.

## Add a Thing Description

A TD describes a device or service so any agent can drive it. Contribute
one to `examples/registry/` (or propose a shared catalog). No Python
needed , a TD is JSON. The more TDs exist, the more useful thingctx is to
everyone.

## Ground rules

- Keep it small. The core stays stdlib-only; transports and helpers are
  opt-in extras.
- Tests pass offline: `pytest -m "not network"`.
- Match the surrounding style. Plain comments, no fluff.

## Sign your commits (DCO)

This project uses the [Developer Certificate of Origin](DCO) (DCO), not a
CLA. By signing off you certify that you wrote the contribution, or
otherwise have the right to submit it under Apache-2.0.

Add the sign-off with `-s`:

```bash
git commit -s -m "add a CoAP binding"
```

That appends a trailer matching the commit author:

```
Signed-off-by: Your Name <you@example.com>
```

If you forget it, amend with `git commit -s --amend`. Every pull request's
commits must be signed off.

## Good first issues

Adding a binding for a transport you use, or a TD for a device you own, is
the best first contribution , scoped, testable, and immediately useful to
the next person.
