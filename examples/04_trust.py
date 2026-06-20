"""04, trust: the safety layer that protects against a wrong decision , by a
human, by an LLM, or by an MCP client (Claude / Copilot CLI).

Same pump as 02/03. The new thing is a trust gate on the client:

  - approval , a risky call (here, any non-idempotent action under the
    "destructive" policy) is routed to an approver, which may deny it. A
    denied call never reaches the device.
  - grounding , verify() reads the TD against the live pump and reports whether
    it still matches (declared types vs. real values).

The same gate sits on ``ThingClient.invoke``, so it protects:
  * a hand-written loop (Part A),
  * an LLM driving the Thing (Part B), and
  * an MCP client over the bridge (Part C note) , Claude/Copilot CLI calls go
    through the same invoke path, and the bridge asks them to confirm a gated
    call via MCP elicitation.

Run::  python examples/04_trust.py
       (Part B uses a local Ollama model or an API key if present; see _pump)
"""

from __future__ import annotations

import asyncio

from _pump import DEVICE_TOKEN, pick_llm_model, start_device

import thingctx
from thingctx import HttpInvoker, LocalInvoker, MqttInvoker, ThingClient


def safety_policy(deny: set[str]):
    """A policy approver: deny the named (dangerous) actions, allow the rest.
    Stands in for a human prompt or an audit rule. Prints each decision."""

    def approve(req) -> bool:
        ok = req.action_name not in deny
        print(
            f"    approver: {req.tool_name}{req.arguments}  reason={req.reason!r}  -> "
            f"{'ALLOW' if ok else 'DENY'}"
        )
        return ok

    return approve


def _invokers(pump):
    return [
        LocalInvoker(pump),
        HttpInvoker(credentials={"bearer_sc": DEVICE_TOKEN}),
        MqttInvoker(timeout=5),
    ]


async def main() -> None:
    pump, td, stop = start_device()
    # Gate every non-idempotent action; the policy denies estop (the dangerous
    # one) but allows ordinary commands like set_speed.
    approve = safety_policy(deny={"estop"})
    try:
        client = ThingClient(
            tds=[td], invokers=_invokers(pump), approve=approve, approve_when="destructive"
        )

        print("== Part A: the gate (no LLM) ==")
        ok = await client.invoke("pump.set_speed", {"rpm": 900})
        print(f"set_speed(900)  -> {ok}")
        assert ok == {"ok": True, "rpm": 900}

        blocked = await client.invoke("pump.estop", {"reason": "panic"})
        print(f"estop()         -> {blocked}")
        assert blocked["error"] == "approval denied"
        assert pump.stopped is False, "the device must NOT have stopped"
        print(f"device protected: pump.stopped == {pump.stopped} (estop never reached it)\n")

        print("== grounding: verify() the TD against the live pump ==")
        for report in await client.verify():
            print(f"verify {report.thing_id} -> ok={report.ok}")
            for c in report.checks:
                print(f"    {c.target}: {'ok' if c.ok else 'FAIL'}  {c.detail}")
        print()

        print("== Part B: an LLM tries the wrong thing ==")
        model = pick_llm_model()
        if model is None:
            print("  (no LLM reachable; skipping. start Ollama qwen2.5:7b or set an API key.)")
        else:
            host = thingctx.from_td(
                td,
                model=model,
                invokers=_invokers(pump),
                approve=approve,
                approve_when="destructive",
                resilient=True,
            )
            print(f"  model: {model}")
            answer = await host.chat(
                "The pump is overheating. Emergency-stop it now by calling estop."
            )
            print(f"  LLM -> {answer}")
            # The gate, not the model, is the guarantee: estop was denied.
            assert pump.stopped is False, "the gate must have blocked the LLM's estop"
            print(f"  device protected: pump.stopped == {pump.stopped} (gate denied the LLM)")

        print("\n== Part C: same gate over MCP (Claude / Copilot CLI) ==")
        print("  `thingctx-mcp <registry>` builds the same client; call_tool routes through")
        print("  ThingClient.invoke, so gated tools are blocked there too. The bridge wires an")
        print("  elicitation approver, so the CLI asks you to confirm. Set the policy with")
        print("  THINGCTX_APPROVE_WHEN=declared|destructive|all|never. Mark risky actions in")
        print("  the TD (tc:requiresApproval / @type tc:Destructive) for the 'declared' policy.")
    finally:
        stop()


if __name__ == "__main__":
    asyncio.run(main())
