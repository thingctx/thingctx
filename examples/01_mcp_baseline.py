"""01: the MCP baseline. The head-to-head partner to 02.

The MCP server here is a proxy. It reaches the pump over the SAME real
transports thingctx uses in 02 (HTTP for status/read_sensor, MQTT for
set_coolant, local for the rest), via a thingctx client. So the only
thing MCP adds over 02 is the server hop itself: the client talks the MCP
protocol to this server, which then makes the same call thingctx makes
directly. The server is a removable middle point.

It also covers MCP's full surface: tools, resources, prompts, and a real
resources/subscribe -> resources/updated flow.

Run::  python examples/01_mcp_baseline.py
"""

from __future__ import annotations

import asyncio
import json

import mcp.types as types
from _pump import (
    DEVICE_TOKEN,
    PumpDevice,
    start_device,
)
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session as connect


def build_server(pump: PumpDevice, http_url: str, mqtt_addr: str) -> Server:
    """An MCP server that reaches the pump over the SAME transports
    thingctx uses in 02. Note the hand-written transport wiring below:
    httpx + the bearer header for HTTP, the path templating for the
    sensor id, a paho publish/await for MQTT. thingctx reads all of that
    from the TD's forms; an MCP server author writes it per tool."""
    import urllib.parse

    server: Server = Server("pump")
    subscribers: set[str] = set()  # resource URIs the client subscribed to
    auth = {"Authorization": f"Bearer {DEVICE_TOKEN}"}

    async def http_get(path: str):
        import httpx

        async with httpx.AsyncClient() as c:
            r = await c.get(f"{http_url}/{path}", headers=auth)
            r.raise_for_status()
            return r.json()

    async def http_post(path: str, body: dict):
        import httpx

        async with httpx.AsyncClient() as c:
            r = await c.post(f"{http_url}/{path}", json=body, headers=auth)
            r.raise_for_status()
            return r.json()

    async def mqtt_call(topic: str, body: dict):
        import asyncio as _a

        import paho.mqtt.client as mqtt

        host, _, port = mqtt_addr.partition(":")
        loop = _a.get_event_loop()
        fut: _a.Future = loop.create_future()
        cli = mqtt.Client()
        cli.on_message = lambda c, u, m: (
            fut.done() or loop.call_soon_threadsafe(fut.set_result, json.loads(m.payload.decode()))
        )
        cli.connect(host, int(port))
        cli.subscribe(f"{topic}/reply")
        cli.loop_start()
        try:
            cli.publish(topic, json.dumps(body))
            return await _a.wait_for(fut, timeout=5)
        finally:
            cli.loop_stop()
            cli.disconnect()

    #  tools: commands + the property WORKAROUND (a get/set pair)
    @server.list_tools()
    async def list_tools():
        obj = {"type": "object"}
        return [
            types.Tool(
                name="set_speed",
                description="Set rpm.",
                inputSchema={
                    "type": "object",
                    "properties": {"rpm": {"type": "integer"}},
                    "required": ["rpm"],
                },
            ),
            types.Tool(name="estop", description="Emergency stop.", inputSchema=obj),
            types.Tool(
                name="read_sensor",
                description="Read a sensor by id.",
                inputSchema={
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            ),
            # the pump's mqtt action -> just another tool here. COST: the
            # server PROXIES it; MCP can't say "this tool is over MQTT".
            types.Tool(
                name="set_coolant",
                description="Open/close coolant.",
                inputSchema={"type": "object", "properties": {"open": {"type": "boolean"}}},
            ),
            # WRITABLE PROPERTY -> a get/set tool pair (the workaround).
            types.Tool(name="get_target_rpm", description="Read setpoint.", inputSchema=obj),
            types.Tool(
                name="set_target_rpm",
                description="Write setpoint.",
                inputSchema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
            ),
            # device-side trigger so the demo can make the server emit a
            # resources/updated notification (the telemetry push).
            types.Tool(
                name="trigger_overheat",
                description="(demo) heat up.",
                inputSchema={"type": "object", "properties": {"temp": {"type": "integer"}}},
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, args: dict):
        if name == "trigger_overheat":
            pump.temp = args["temp"]
            ctx = server.request_context
            await ctx.session.send_resource_updated("pump://overheat/latest")
            return [types.TextContent(type="text", text='{"notified": true}')]
        # Each tool: the author picks the transport and wires it by hand.
        if name == "read_sensor":
            sid = urllib.parse.quote(str(args["id"]), safe="")  # path templating
            result = await http_get(f"sensors/{sid}")  # HTTP + bearer
        elif name == "set_coolant":
            result = await mqtt_call("pump/set_coolant", args)  # MQTT round trip
        elif name == "set_speed":
            result = pump.set_speed(args["rpm"])  # local
        elif name == "estop":
            result = pump.estop()
        elif name == "get_target_rpm":
            result = {"target_rpm": pump.get_target_rpm()}
        elif name == "set_target_rpm":
            result = pump.set_target_rpm(args["value"])
        else:
            raise ValueError(f"unknown tool {name}")
        return [types.TextContent(type="text", text=json.dumps(result))]

    #  resources: state read + the EVENT workaround (a polled resource)
    @server.list_resources()
    async def list_resources():
        return [
            types.Resource(uri="pump://status", name="status", description="Pump status."),
            types.Resource(
                uri="pump://overheat/latest",
                name="overheat",
                description="Latest overheat reading (poll).",
            ),
        ]

    @server.read_resource()
    async def read_resource(uri):
        u = str(uri)
        if u == "pump://status":
            return json.dumps(await http_get("status"))  # HTTP + bearer
        if u == "pump://overheat/latest":
            return json.dumps({"temp": pump.temp, "limit": 80})
        raise ValueError(f"unknown resource {u}")

    #  resources/subscribe: MCP's real push (notify a changed URI)
    @server.subscribe_resource()
    async def subscribe_resource(uri):
        subscribers.add(str(uri))

    #  a prompt (WoT has no equivalent)
    @server.list_prompts()
    async def list_prompts():
        return [types.Prompt(name="diagnose", description="Diagnose the pump.")]

    @server.get_prompt()
    async def get_prompt(name: str, args):
        return types.GetPromptResult(
            description="Diagnose the pump.",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text", text="Read pump://status and report if the pump is healthy."
                    ),
                )
            ],
        )

    server._pump_subscribers = subscribers  # exposed so we can notify
    return server


async def main() -> None:
    # Same device as 02: HTTP server (bearer + SSE) + real MQTT broker.
    pump, td, stop = start_device()
    http_url = td["base"].rstrip("/")
    mqtt_addr = td["actions"]["set_coolant"]["forms"][0]["href"].split("//")[1].split("/")[0]
    server = build_server(pump, http_url, mqtt_addr)

    notes: list = []

    async def on_message(msg):
        n = getattr(msg, "root", msg)
        if type(n).__name__ == "ResourceUpdatedNotification":
            notes.append(str(n.params.uri))

    # Oracle: a second pump in the same start state. Every MCP result is
    # asserted equal to calling the device directly.
    oracle = PumpDevice()

    def check(label, got_json, expected) -> None:
        got = json.loads(got_json) if isinstance(got_json, str) else got_json
        assert got == expected, f"{label}: MCP gave {got!r}, device gives {expected!r}"
        print(f"{label:<34}-> {got}  ok == device")

    try:
        async with connect(server, message_handler=on_message) as session:
            await session.initialize()

            async def call(name, args=None):
                r = await session.call_tool(name, args or {})
                return "".join(
                    c.text for c in r.content if getattr(c, "type", "") == "text"
                ).strip()

            async def read(uri):
                r = await session.read_resource(uri)
                return r.contents[0].text

            check(
                "COMMAND   set_speed(900)",
                await call("set_speed", {"rpm": 900}),
                oracle.set_speed(900),
            )
            check("COMMAND   estop()", await call("estop"), oracle.estop())
            check(
                "PATH-READ read_sensor('temp-1')   [hand-wired HTTP + path]",
                await call("read_sensor", {"id": "temp-1"}),
                oracle.read_sensor("temp-1"),
            )
            check(
                "WRITE     set_target_rpm(1500)     [a tool pair]",
                await call("set_target_rpm", {"value": 1500}),
                oracle.set_target_rpm(1500),
            )
            check(
                "READ      get_target_rpm()",
                await call("get_target_rpm"),
                {"target_rpm": oracle.get_target_rpm()},
            )
            check(
                "MQTT-ACT  set_coolant(open=True)     [hand-wired MQTT round trip]",
                await call("set_coolant", {"open": True}),
                oracle.set_coolant(True),
            )

            check(
                "STATE     read pump://status        [hand-wired HTTP]",
                await read("pump://status"),
                oracle.status(),
            )
            check(
                "EVENT     read overheat/latest",
                await read("pump://overheat/latest"),
                {"temp": oracle.temp, "limit": 80},
            )

            prompt = await session.get_prompt("diagnose", {})
            text = prompt.messages[0].content.text
            assert "pump://status" in text
            print(f"{'PROMPT    get_prompt(diagnose)':<34}-> {text!r}  ok")

            # subscribe, heat the device, get the resources/updated push,
            # then re-read (the notification carries a URI, not the value).
            await session.subscribe_resource("pump://overheat/latest")
            oracle.temp = 98
            await call("trigger_overheat", {"temp": 98})
            await asyncio.sleep(0.05)
            assert notes, "no resources/updated notification arrived"
            uri = notes[-1]
            assert uri == "pump://overheat/latest"
            check(
                "PUSH      notified->re-read           [URI, not payload]",
                await read(uri),
                {"temp": oracle.temp, "limit": 80},
            )

        print("\nEvery result reached the pump over the same transports as 02")
        print("(HTTP, MQTT, local), but through the hand-wired MCP server.")
        print("The server is a middle hop; 02 makes the same calls without it.")
    finally:
        stop()


if __name__ == "__main__":
    asyncio.run(main())
