# Trust: approval and grounding

## Scope

This document specifies the two trust primitives a consumer applies when it lets
an agent drive a real system from a [W3C Thing
Description](https://www.w3.org/TR/wot-thing-description11/): **approval gating**
of risky calls, and **grounding** a description against the live Thing.

Both are opt-in, have no LLM dependency, and are implemented in
`thingctx.trust` and surfaced on `ThingClient`. They are defined one layer above
the Thing Description and its transport bindings, and are not otherwise
standardized.

A Thing Description is data: it can be authored anywhere and may drift from the
system it describes. The grounding check exists because a description is trusted
only insofar as it still matches reality. The approval gate exists because some
actions change the world and an agent should not be the only thing deciding to
run them.

## Approval gating

### Risk

An action is *risky* when either holds:

- The Thing Description marks it so: `tc:requiresApproval` is truthy on the
  action, or its `@type` includes `tc:Destructive`.
- A non-idempotent action is treated as destructive (`is_destructive()`), since
  re-running it is not safe.

A property write is always a state mutation and is risky under the wider
policies.

### Policy

`ThingClient(approve=..., approve_when=<policy>)` selects when the approver is
consulted:

| policy        | gated calls                                                            |
| ------------- | ---------------------------------------------------------------------- |
| `declared`    | actions the TD marks risky (`tc:requiresApproval` / `tc:Destructive`)  |
| `destructive` | the above, plus any non-idempotent action and every property write     |
| `all`         | every action and every property write                                  |
| `never`       | nothing (gating off)                                                   |

`declared` is the default. It honors exactly what the description author marked,
and adds no friction to actions they declared safe.

### Approver

The approver is any callable, sync or async, that receives an `ApprovalRequest`
and returns truthy to allow the call:

```python
@dataclass
class ApprovalRequest:
    tool_name: str            # e.g. "pump.estop"
    arguments: dict           # the call arguments (or {"value": ...} for a write)
    thing_id: str
    action_name: str
    reason: str               # why approval was required
    description: str
```

### Default deny

When a call is gated but **no approver is configured**, the call is denied and
returns an error envelope rather than running. A gate with nobody to open it
stays shut. To run risky calls without a prompt, set `approve_when="never"`
explicitly, which records the intent in code.

### Outcome

The gate is enforced inside `ThingClient.invoke` and `write_property`, so it
applies uniformly to the LLM tool-calling loop and to direct callers. A blocked
call returns a structured envelope and never reaches the transport:

```python
{"error": "approval required but no approver configured", "tool": "pump.estop",
 "reason": "TD-declared (tc:requiresApproval / tc:Destructive)", "hint": "..."}
{"error": "approval denied", "tool": "pump.estop", "reason": "..."}
```

## Grounding

### What verify() checks

`await client.verify(thing_id=None)` grounds each Thing against its live
endpoint and returns one report per Thing. For every **readable** property it:

1. reads the current value over the property's transport, and
2. checks the value against the property's declared type, when that type is a
   scalar (`integer`, `number`, `string`, `boolean`).

The check is lenient: an absent or non-scalar declared type passes, so an
object- or array-valued property is grounded by a successful read alone. A read
that returns an error envelope, or raises, fails that check.

Actions are **never invoked** , invoking has side effects, so grounding is
read-only and safe to run against production. Actions are validated structurally
when the TD is parsed (and against the W3C schema with `validate=True`), not by
exercising them.

### Report

```python
@dataclass
class Check:
    target: str               # e.g. "property:rpm"
    ok: bool
    detail: str

@dataclass
class VerifyReport:
    thing_id: str
    ok: bool                  # True iff every check passed
    checks: list[Check]
    def __bool__(self) -> bool: ...
    def as_dict(self) -> dict: ...
```

```python
for report in await client.verify():
    if not report:
        print("drifted:", report.as_dict())
```

## Over the MCP bridge (Claude / Copilot CLI)

The bridge (`thingctx-mcp`) executes every tool call through the same
`ThingClient.invoke`, so the gate protects MCP clients exactly as it protects a
direct caller. Two layers apply:

1. **Client-side hints.** Each tool is annotated with `destructiveHint`,
   `idempotentHint`, and `readOnlyHint`, derived from the TD (`is_destructive()`
   / `idempotent`). Claude/Copilot CLI use these to prompt before a risky tool.
2. **Server-side enforcement.** When a gated call runs, the bridge asks the
   connected client to confirm via **MCP elicitation**. Accept lets the call
   proceed; decline, cancel, or a client that cannot elicit means the call is
   denied and never reaches the device.

The policy is set with the `THINGCTX_APPROVE_WHEN` environment variable
(`declared` default, or `destructive` / `all` / `never`). Mark risky actions in
the TD for the `declared` policy:

```json
{ "actions": { "reboot": { "@type": "tc:Destructive",
  "forms": [{ "href": "https://device.local/reboot" }] } } }
```

`.mcp.json` for a CLI, gating any non-idempotent call:

```json
{ "mcpServers": { "things": {
    "command": "thingctx-mcp",
    "args": ["./registry/"],
    "env": { "THINGCTX_APPROVE_WHEN": "destructive" } } } }
```

A custom approver (audit log, out-of-band confirm) can replace elicitation:
`build_mcp_server(client, approve=my_callable)`.

## API summary

| name                              | purpose                                         |
| --------------------------------- | ----------------------------------------------- |
| `ThingClient(approve=, approve_when=)` | wire the gate                              |
| `ApprovalRequest`                 | what the approver is asked to allow             |
| `client.verify(thing_id=None)`    | ground TD(s) against the live Thing             |
| `VerifyReport`, `Check`           | grounding results                               |

## Notes and limits

- Grounding checks declared **scalar** types only; it does not validate nested
  object or array schemas against live values. A successful read is the signal
  for those.
- Risk is read from the description and policy; thingctx does not infer that an
  un-annotated, idempotent action is dangerous. Mark such actions in the TD, or
  widen `approve_when`.
- The approver is the integration point for a UI, an audit log, or an
  out-of-band confirmation. thingctx supplies the request and enforces the
  verdict; it does not prescribe how the decision is made.
