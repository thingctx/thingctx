# Trust: approval and grounding

Two opt-in primitives for letting an agent drive a real system from a TD:
**approval gating** of risky calls and **grounding** a TD against the live Thing.
Both have no LLM dependency, live in `thingctx.trust`, and are surfaced on
`ThingClient`.

## Approval gating

A call is *risky* when the TD marks it (`tc:requiresApproval`, or `@type`
includes `tc:Destructive`), when an action is non-idempotent, or when it writes a
property. `ThingClient(approve=<callable>, approve_when=<policy>)` picks when the
approver is consulted:

| policy | gated calls |
|---|---|
| `declared` (default) | actions the TD marks risky |
| `destructive` | the above + any non-idempotent action and every property write |
| `all` | every action and every property write |
| `never` | nothing (gating off) |

The approver is any callable (sync or async) that receives an `ApprovalRequest`
(`tool_name`, `arguments`, `thing_id`, `action_name`, `reason`) and returns
truthy to allow the call. Gating is enforced inside `invoke` / `write_property`,
so it applies to the LLM tool-loop and direct callers alike.

**Default deny:** if a call is gated but no approver is configured, it returns an
error envelope instead of running. Set `approve_when="never"` to run risky calls
without a prompt.

## Grounding

`await client.verify(thing_id=None)` returns a `VerifyReport` per Thing. For each
**readable** property it reads the live value and checks it against the declared
type when that type is scalar. The check is lenient (absent/non-scalar types pass
on a successful read) and **read-only** — actions are never invoked, so it is
safe against production.

```python
for report in await client.verify():
    if not report:                  # VerifyReport.__bool__ == all checks passed
        print("drifted:", report.as_dict())
```

## Over the MCP bridge

The MCP bridge (`thingctx-mcp`) runs every tool call through the same
`ThingClient.invoke`, so the gate protects MCP clients too. Tools carry
`destructiveHint` / `idempotentHint` / `readOnlyHint` from the TD, and a gated
call asks the client to confirm via MCP elicitation (decline, or a client that
can't elicit, denies). Set the policy with `THINGCTX_APPROVE_WHEN`, or pass a
custom approver: `build_mcp_server(client, approve=my_callable)`.

## API summary

| name | purpose |
|---|---|
| `ThingClient(approve=, approve_when=)` | wire the gate |
| `ApprovalRequest` | what the approver is asked to allow |
| `client.verify(thing_id=None)` | ground TD(s) against the live Thing |
| `VerifyReport`, `Check` | grounding results |

Risk is read from the TD and policy — thingctx does not infer that an
un-annotated idempotent action is dangerous; mark it in the TD or widen
`approve_when`. The approver is the integration point for a UI, audit log, or
out-of-band confirmation.
