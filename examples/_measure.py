"""Measure 01 (MCP) vs 02 (thingctx) on the SAME pump, for the README.

Three numbers, both paths driving the identical device:

  processes  servers you author + run per integration
  lines      hand-written integration code (non-blank, non-comment),
             excluding docstrings and the shared Thing Description (data)
  ttfc       time-to-first-call: wall-clock from "begin integrating" to the
             first action result, median of N runs (device startup excluded,
             it is the shared external world both paths consume)

Run::  python examples/_measure.py
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

from _pump import DEVICE_TOKEN, PumpDevice, start_device

HERE = Path(__file__).parent
RUNS = 9


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _device_env(td) -> dict:
    http_url = td["base"].rstrip("/")
    mqtt_addr = td["actions"]["set_coolant"]["forms"][0]["href"].split("//")[1].split("/")[0]
    return {**os.environ, "PUMP_HTTP": http_url, "PUMP_MQTT": mqtt_addr}


# ---- lines: count authored integration code, fairly ----


def _code_lines(path: Path, node_filter) -> int:
    """Non-blank, non-comment physical lines of the matching top-level
    node(s), minus their docstring lines. This is the code a human writes
    and maintains for the integration."""
    src = path.read_text()
    lines = src.splitlines()
    tree = ast.parse(src)
    total = 0
    for node in ast.walk(tree):
        if not node_filter(node):
            continue
        start, end = node.lineno, node.end_lineno
        doc = ast.get_docstring(node, clean=False)
        doc_span: set[int] = set()
        if doc and node.body:
            d = node.body[0]
            doc_span = set(range(d.lineno, (d.end_lineno or d.lineno) + 1))
        for i in range(start, end + 1):
            if i in doc_span:
                continue
            s = lines[i - 1].strip()
            if not s or s.startswith("#"):
                continue
            total += 1
    return total


def lines_mcp() -> int:
    # the MCP server you author per integration: build_server(...)
    return _code_lines(
        HERE / "01_mcp_baseline.py",
        lambda n: isinstance(n, ast.FunctionDef) and n.name == "build_server",
    )


def lines_thingctx() -> int:
    # what you author to consume the TD: the ThingClient(...) setup + import.
    src = (HERE / "02_thingctx_baseline.py").read_text()
    lines = src.splitlines()
    tree = ast.parse(src)
    count = 0
    for node in ast.walk(tree):
        # the `from thingctx import ...` line
        if isinstance(node, ast.ImportFrom) and node.module == "thingctx":
            count += 1
        # the `client = ThingClient(...)` assignment
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", "") == "ThingClient"
        ):
            for i in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                s = lines[i - 1].strip()
                if s and not s.startswith("#"):
                    count += 1
    return count


def td_json_lines() -> int:
    return len((HERE / "pump.td.json").read_text().splitlines())


# ---- ttfc: time from "begin integrating" to first action result ----


async def ttfc_mcp(pump, td) -> float:
    """build the MCP server + open a session + initialize + first call_tool.
    In-memory transport: a LOWER BOUND (real MCP spawns a process / opens a
    stdio or http transport, which costs more)."""
    import importlib

    mod = importlib.import_module("01_mcp_baseline")
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    http_url = td["base"].rstrip("/")
    mqtt_addr = td["actions"]["set_coolant"]["forms"][0]["href"].split("//")[1].split("/")[0]
    t0 = time.perf_counter()
    server = mod.build_server(pump, http_url, mqtt_addr)
    async with connect(server) as session:
        await session.initialize()
        await session.call_tool("set_speed", {"rpm": 900})
    return time.perf_counter() - t0


async def ttfc_mcp_stdio(_pump, td) -> float:
    """stdio MCP: the client SPAWNS the server per session, so every first
    call pays process startup + the stdio handshake , the real cost to stand
    up one stdio integration."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=[str(HERE / "_mcp_stdio_server.py")], env=_device_env(td)
    )
    t0 = time.perf_counter()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("set_speed", {"rpm": 900})
    return time.perf_counter() - t0


async def ttfc_mcp_http(url: str) -> float:
    """streamable-HTTP MCP: the server is already running (a process you
    operate). Time only connect + initialize + first call , the per-session
    latency a client pays against a warm server."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    t0 = time.perf_counter()
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("set_speed", {"rpm": 900})
    return time.perf_counter() - t0


def start_http_mcp(td) -> tuple[subprocess.Popen, str]:
    """Bring up the long-running HTTP MCP server; return (proc, url) once it
    is accepting connections."""
    port = _free_port()
    env = {**_device_env(td), "MCP_PORT": str(port)}
    proc = subprocess.Popen([sys.executable, str(HERE / "_mcp_http_server.py")], env=env)
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    time.sleep(0.5)  # let the ASGI app finish its lifespan startup
    return proc, f"http://127.0.0.1:{port}/mcp"


async def ttfc_thingctx(pump, td) -> float:
    """consume the TD (validate against the W3C schema) + first invoke."""
    from thingctx import HttpInvoker, LocalInvoker, MqttInvoker, ThingClient

    t0 = time.perf_counter()
    client = ThingClient(
        tds=[td],
        invokers=[
            LocalInvoker(pump),
            HttpInvoker(credentials={"bearer_sc": DEVICE_TOKEN}),
            MqttInvoker(timeout=5),
        ],
        validate=True,
    )
    await client.invoke("pump.set_speed", {"rpm": 900})
    return time.perf_counter() - t0


async def median_ttfc(fn, td) -> float:
    samples = []
    for _ in range(RUNS + 1):  # +1 warmup, dropped
        pump = PumpDevice()
        samples.append(await fn(pump, td))
    return statistics.median(samples[1:]) * 1000.0  # ms


async def median_http(url: str) -> float:
    samples = []
    for _ in range(RUNS + 1):  # +1 warmup, dropped
        samples.append(await ttfc_mcp_http(url))
    return statistics.median(samples[1:]) * 1000.0  # ms


async def main() -> None:
    sys.path.insert(0, str(HERE))  # so importlib finds 01_mcp_baseline

    proc, td, stop = None, None, None
    _, td, stop = start_device()
    try:
        mcp_stdio_ms = await median_ttfc(ttfc_mcp_stdio, td)
        proc, url = start_http_mcp(td)
        mcp_http_ms = await median_http(url)
        mcp_mem_ms = await median_ttfc(ttfc_mcp, td)
        tc_ms = await median_ttfc(ttfc_thingctx, td)
    finally:
        if proc:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
        if stop:
            stop()

    lm, lt, ltd = lines_mcp(), lines_thingctx(), td_json_lines()

    print(f"runs (median of {RUNS}, 1 warmup dropped)\n")
    print(f"{'metric':<24}{'MCP stdio':>13}{'MCP http':>13}{'thingctx':>13}")
    print("-" * 63)
    print(f"{'server process':<24}{'per session':>13}{'1 (running)':>13}{'0':>13}")
    print(f"{'hand-written lines':<24}{lm:>13}{lm:>13}{lt:>13}")
    print(
        f"{'time to first call':<24}{mcp_stdio_ms:>10.0f} ms"
        f"{mcp_http_ms:>10.0f} ms{tc_ms:>10.1f} ms"
    )
    print(f"\n(Thing Description is data, shared by every consumer: {ltd} lines JSON.)")
    print("(stdio MCP spawns the server per session, so its first call pays process")
    print(" startup; http MCP is a warm long-running server, timed connect+init+call.")
    print(f" In-memory MCP, no process or socket, is {mcp_mem_ms:.1f} ms , a floor.)")


if __name__ == "__main__":
    asyncio.run(main())
