"""03, thingctx + an LLM: the same pump as 02, now driven by a model.

02 called the surface by hand (invoke / read_property / subscribe ...).
03 wires a real LLM to the same TD and lets the model drive it, you
write no tool-calling. Two ways:

  1. a plain instruction -> the model picks the actions, thingctx routes
     each call to the transport its form names.
  2. a tc:PromptTemplate (`get_prompt`) -> the prompt's expanded messages
     seed the conversation; the model then executes them against the
     Thing. This is where prompts shine: a user-picked template becomes
     the agent's opening turn.

Run::  python examples/03_thingctx_llm.py
       (uses local Ollama qwen2.5:7b if present; see pick_llm_model)
"""

from __future__ import annotations

import asyncio

from _pump import DEVICE_TOKEN, pick_llm_model, start_device

import thingctx
from thingctx import HttpInvoker, LocalInvoker, MqttInvoker
from thingctx.extensions.prompts import get_prompt, list_prompts


async def main() -> None:
    model = pick_llm_model()
    if model is None:
        print("No LLM reachable, start Ollama (qwen2.5:7b) or set an API key.")
        return

    pump, td, stop = start_device()
    try:
        # Same TD + invokers as 02, the full surface. The only new thing
        # is `model=`: the LLM now drives it.
        host = thingctx.from_td(
            td,
            model=model,
            invokers=[
                LocalInvoker(pump),
                HttpInvoker(credentials={"bearer_sc": DEVICE_TOKEN}),
                MqttInvoker(timeout=5),
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
        msgs = await get_prompt(host.client, "pump.diagnose", {"severity": "high"})
        seed = msgs[0]["content"]  # the expanded template text
        print("  get_prompt('pump.diagnose', severity=high) ->")
        print(f"    {seed!r}")
        diagnosis = await host.chat(seed)  # feed it to the LLM
        print(f"  LLM acted on the prompt -> {diagnosis}")

        print("\nNo tool-calling written. The model drove the same TD as 02;")
        print("the prompt template (from the TD) seeded the diagnosis turn.")
    finally:
        stop()


if __name__ == "__main__":
    asyncio.run(main())
