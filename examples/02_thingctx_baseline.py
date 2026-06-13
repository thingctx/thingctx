"""02, thingctx baseline: the same pump as 01, the WoT programming model.

This is the head-to-head with 01 (MCP). Same device, same capabilities,
same order, but here you write no server. You consume the device's TD
(which names the device's own endpoints) and call its surface directly
through the pure ``ThingClient`` (no LLM). Read 01 then this, line for
line.

01 (MCP)                              02 (thingctx)
  author+run a FastMCP server     ->    none, consume the TD
  @tool set_speed / estop         ->    invoke("pump.set_speed", ...)
  @tool read_sensor(id)           ->    invoke("pump.read_sensor", {id})  (uriVar)
  @resource pump://status         ->    invoke("pump.status")  (idempotent -> GET)
  get_/set_target_rpm TOOL PAIR   ->    read_property / write_property (typed)
  poll pump://overheat/latest     ->    subscribe("pump.overheat")  (data inline)

Run::  python examples/02_thingctx_baseline.py
"""

from __future__ import annotations

import asyncio

from _pump import DEVICE_TOKEN, PumpDevice, start_device

from thingctx import HttpInvoker, LocalInvoker, MqttInvoker, ThingClient


async def main() -> None:
    pump, td, stop = start_device()
    pump.start_telemetry(temps=(70, 85, 99), period=0.2)
    try:
        # No server authored. We consume the device's TD and call its
        # endpoints; validate=True checks it against the W3C TD 1.1 schema.
        # One invoker per transport the TD's forms name: local, http(+bearer),
        # mqtt, covering the full surface.
        client = ThingClient(
            tds=[td],
            invokers=[
                LocalInvoker(pump),
                HttpInvoker(credentials={"bearer_sc": DEVICE_TOKEN}),
                MqttInvoker(timeout=5),
            ],
            validate=True,
        )

        # No wrappers needed: client.invoke / read_property /
        # write_property / subscribe ARE the surface, and they return the
        # value directly (01 needs helpers to unwrap session.call_tool's
        # content). ORACLE: a second pump; assert every result equals
        # calling the device directly, same correctness bar as 01.
        oracle = PumpDevice()

        def check(label, got, expected):
            assert got == expected, f"{label}: thingctx {got!r}, device {expected!r}"
            print(f"{label:<34}-> {got}  ok == device")

        check(
            "COMMAND    set_speed(900)",
            await client.invoke("pump.set_speed", {"rpm": 900}),
            oracle.set_speed(900),
        )
        check("COMMAND    estop()", await client.invoke("pump.estop"), oracle.estop())
        check(
            "PATH-READ  read_sensor('temp-1')   [uriVar {id}, no hand-coded REST]",
            await client.invoke("pump.read_sensor", {"id": "temp-1"}),
            oracle.read_sensor("temp-1"),
        )
        check(
            "WRITE      target_rpm <- 1500       [a typed property, not a pair]",
            await client.write_property("pump.target_rpm", 1500),
            oracle.set_target_rpm(1500),
        )
        check(
            "READ       target_rpm",
            await client.read_property("pump.target_rpm"),
            oracle.get_target_rpm(),
        )
        check(
            "READ-ONLY  rpm write rejected",
            await client.write_property("pump.rpm", 5),
            {"error": "property pump.rpm is read-only"},
        )
        check("STATE      status()", await client.invoke("pump.status"), oracle.status())
        # MQTT, set_coolant routes over a real broker (the form is mqtt://)
        check(
            "MQTT       set_coolant(open=True)    [routed over real MQTT]",
            await client.invoke("pump.set_coolant", {"open": True}),
            oracle.set_coolant(True),
        )

        # PROMPT, the WoT-native equivalent of MCP's get_prompt, via the
        # tc:PromptTemplate extension. The template lives in the TD, so it
        # expands CLIENT-SIDE, no device call (the device has no diagnose
        # method). The TD is self-sufficient.
        from thingctx.extensions.prompts import get_prompt

        msgs = await get_prompt(client, "pump.diagnose", {"severity": "high"})
        check(
            "PROMPT     get_prompt(diagnose)     [tc:template, no device call]",
            msgs,
            [
                {
                    "role": "user",
                    "content": "Severity high: read the pump status, list any sensor "
                    "reading over its limit, and say whether the pump is healthy.",
                }
            ],
        )

        # EVENT, a real subscription; the typed payload arrives INLINE.
        # Assert the pushed value is the device's event, not a URI.
        print(f"{'PUSH       subscribe(overheat)':<34}-> next 2 events, data inline:")
        n = 0
        async for evt in await client.subscribe("pump.overheat"):
            assert "temp" in evt and "limit" in evt, "event must carry data inline"
            print(f"           {evt}  ok inline payload (not a URI)")
            n += 1
            if n >= 2:
                break

        print("\nEvery thingctx result asserted == calling the pump directly.")
        print("full surface, 4 transports (local/http/mqtt/sse), no server written.")
        print("Pure ThingClient; hand `as_tools()` to any agent (an LLM, or any loop).")
    finally:
        stop()


if __name__ == "__main__":
    asyncio.run(main())
