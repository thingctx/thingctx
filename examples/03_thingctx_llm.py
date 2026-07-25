# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""03, thingctx + an LLM: the same pump as 02, now driven by a model.

02 called the surface by hand (invoke / read_property / subscribe ...).
03 wires a real LLM to the same TD and lets the model drive it, you
write no tool-calling. Four ways:

  1. a plain instruction -> the model picks the actions, thingctx routes
     each call to the transport its form names.
  2. a tc:PromptTemplate (`get_prompt`) -> the prompt's expanded messages
     seed the conversation; the model then executes them against the
     Thing. This is where prompts shine: a user-picked template becomes
     the agent's opening turn.
  3. the long-running `calibrate` action -> one blocking tool call that
     returns the final result; the TD declares queryaction/cancelaction, so
     the same handle can be polled or cancelled mid-flight (and a
     `<action>.cancel` tool is on the surface).
  4. a bulk read -> the model reads every property in one call via the
     Thing-level readallproperties tool.
  5. a live event stream (`summarize_telemetry`) -> a pushed subscription
     seeds an LLM turn directly.

Over the MCP bridge a long-running action is polled server-side to completion
and gets a cancel tool, but the client never holds the running handle; and an
event is a subscribable resource the client must re-read (signal, then read,
over a bounded buffer). Here invoke returns the handle and the event value
arrives inline and seeds the turn.

Run::  python examples/03_thingctx_llm.py
       (uses local Ollama qwen2.5:7b if present; see pick_llm_model)
"""

from __future__ import annotations

import asyncio

from _pump import DEVICE_TOKEN, pick_llm_model, start_device

import thingctx
from thingctx import HttpBinding, LocalBinding, MqttBinding
from thingctx.extensions.prompts import get_prompt, list_prompts


async def main() -> None:
    model = pick_llm_model()
    if model is None:
        print("No LLM reachable, start Ollama (qwen2.5:7b) or set an API key.")
        return

    pump, td, stop = start_device()
    try:
        # Same TD + bindings as 02, the full surface. The only new thing
        # is `model=`: the LLM now drives it.
        host = thingctx.from_td(
            td,
            model=model,
            bindings=[
                LocalBinding(pump),
                HttpBinding(credentials={"bearer_sc": DEVICE_TOKEN}),
                MqttBinding(timeout=5),
            ],
            resilient=True,
        )
        print(f"model: {model}\n")

        # 1) Plain instruction, the model picks + routes the actions.
        answer = await host.chat("Spin the pump to 1500 rpm, then report the status.")
        print("CHAT   'spin to 1500, report status'")
        print(f"  -> {answer}")
        print(f"  (device.rpm={pump.rpm})\n")

        # 2) PROMPT, a user picks a declared template; it seeds the agent.
        prompts = list_prompts(host.client)
        print(f"PROMPTS the Thing declares (tc:PromptTemplate): {[p['name'] for p in prompts]}")
        msgs = await get_prompt(host.client, "pump__diagnose", {"severity": "high"})
        seed = msgs[0]["content"]  # the expanded template text
        print("  get_prompt('pump__diagnose', severity=high) ->")
        print(f"    {seed!r}")
        diagnosis = await host.chat(seed)  # feed it to the LLM
        print(f"  LLM acted on the prompt -> {diagnosis}")

        # 3) Long-running action, the model calls `calibrate` like any other
        # tool; the runtime blocks it to completion (synchronous:false, with
        # queryaction/cancelaction) and hands the final result back, so the
        # model sees a normal tool return. A `pump__calibrate__cancel` tool is on
        # the same surface for stopping a run mid-flight.
        answer = await host.chat("Calibrate the pump to 1200 rpm.")
        print("\nCHAT   'calibrate to 1200'   [long-running; blocks to completion]")
        print(f"  -> {answer}")
        print(f"  (device.target_rpm={pump.target_rpm})")

        # 4) Bulk read, the model reads every property in one call (the
        # Thing-level readallproperties tool), not property by property.
        answer = await host.chat("Read all of the pump's properties at once and report them.")
        print("\nCHAT   'read all properties at once'   [bulk readallproperties tool]")
        print(f"  -> {answer}")

        # 5) Live event, a pushed subscription seeds an LLM turn. Over MCP this
        # event is a subscribable resource the client re-reads after a signal;
        # here summarize_telemetry consumes the stream and the values arrive
        # inline for the model to reason over.
        pump.start_telemetry(temps=(70, 85, 99), period=0.2)
        summary = await host.summarize_telemetry(
            "pump__overheat",
            "These are overheat readings (temp vs limit). Is the pump overheating, "
            "and by how much over the limit at worst?",
            samples=3,
        )
        print("\nMONITOR summarize_telemetry('pump__overheat', samples=3)  [live SSE -> LLM]")
        print(f"  -> {summary}")

        print("\nNo tool-calling written. The model drove the same TD as 02,")
        print("including a long-running action, a bulk read, and a live event")
        print("stream, the WoT surface that is trickier over the MCP bridge.")
    finally:
        stop()


if __name__ == "__main__":
    asyncio.run(main())
