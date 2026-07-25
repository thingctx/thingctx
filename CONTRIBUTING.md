# Contributing to thingctx

thingctx is small on purpose, and built so you can add real value in one
focused pull request. The contribution that matters most:

## Add a transport (a binding)

A binding teaches thingctx to speak one transport. The built-in bindings are
Local, HTTP, MQTT, and media. CoAP, WebSocket, OPC-UA, Modbus, gRPC, serial ,
each is one self-contained class against the `ProtocolBinding` contract that no
one has to coordinate on:

```python
class CoapBinding:
    scheme = "coap"                 # the contract; add schemes = (...) for more
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

## Describing your own device is not a contribution

That is the design working, not an omission. A Thing Description is a file you
keep next to your own code, and thingctx reads it without knowing anything about
your device, so nothing in this repository has to change for you to drive one.
You never need to send us your hardware.

The exception is teaching. If a description makes a point the docs miss, a worked
example under `examples/` is welcome. The device stays yours.

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

## AI-assisted contributions, and the human behind them

You are welcome to use AI tools to write code here. Many good patches are
drafted with an assistant, and we do not treat that as a problem. What we
require is simple and it follows straight from the sign-off above: a real
person stands behind every contribution.

The sign-off is not a formality. When you add `Signed-off-by`, you are
certifying, under the Developer Certificate of Origin, that you have the right
to submit the work and that you take responsibility for it. An AI tool cannot
make that certification. A person can. So:

- The `Signed-off-by` name and email must be a real human's, reachable and
  accountable. Not a tool's name, not a bot account, not an alias created to
  farm contributions.
- You are responsible for what you submit, whether you typed it or an assistant
  did. "The model wrote it" is not an answer to a review question. Read your
  own patch, understand it, and be able to explain why it is correct.
- Test it yourself before you open the PR. Run `pytest -m "not network"` and
  the relevant example. A patch that only compiles in theory wastes a review.
- Disclose heavy AI involvement if it helps a reviewer, for example if a large
  change was generated. A one-line note in the PR is enough. This is courtesy,
  not a confession; the point is an honest, reviewable history.

What we will not accept: automated or drive-by pull requests opened by a bot or
a scripted account, patches submitted purely to inflate a contribution count,
and PRs whose author cannot answer questions about their own change. These
waste maintainer time and we close them, however clean the diff looks. The bar
is a human who understands and vouches for the work, not the absence of AI.

If you are a real person new to the project and used an assistant, you are
exactly who this section is for. Sign off, claim the issue, and say hello.

## Claiming an issue first

Before you open a pull request, comment on the issue to claim it, so two people
do not build the same thing. A maintainer will confirm. This also lets us point
you at anything the issue does not spell out. Unclaimed PRs may be closed,
especially on a well-scoped issue that someone else already claimed.

## Where to start

Adding a binding for a transport thingctx cannot yet speak is scoped, testable,
and immediately useful. See the open issues
labeled [`good first issue`](https://github.com/thingctx/thingctx/labels/good%20first%20issue)
or [`help wanted`](https://github.com/thingctx/thingctx/labels/help%20wanted),
claim one, and go.

## Asking before you build

Not everything needs an issue. If you want to know whether thingctx already does
something, whether an idea fits, or how to reach a device you have,
[Discussions](https://github.com/thingctx/thingctx/discussions) is the place, and
a question there is welcome on its own.
