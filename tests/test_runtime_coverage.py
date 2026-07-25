# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Full functional TD 1.1 coverage: the runtime drives gzip content-coding,
bulk property ops, the async action lifecycle, filtered subscriptions, the
declared error response, and semantic validation, end to end against the
example device's real HTTP server (no MQTT broker needed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from _pump import DEVICE_TOKEN, PumpDevice, pump_td, start_http_server  # noqa: E402

from thingctx import HttpBinding, LocalBinding, ThingClient  # noqa: E402
from thingctx.validate import validate_semantics  # noqa: E402


@pytest.fixture
def pump_client():
    pump = PumpDevice()
    url, server = start_http_server(pump)
    td = pump_td(url, "localhost:1883")
    client = ThingClient(
        tds=[td],
        bindings=[LocalBinding(pump), HttpBinding(credentials={"bearer_sc": DEVICE_TOKEN})],
    )
    try:
        yield pump, client
    finally:
        server.shutdown()


async def test_status_gzip_roundtrips(pump_client):
    """status declares contentCoding=gzip; the device gzips the body and the
    binding decodes it transparently to the same value the device returns."""
    pump, client = pump_client
    assert await client.invoke("pump__status") == pump.status()


async def test_bulk_read_and_write_route_to_bulk_form(pump_client):
    pump, client = pump_client
    wrote = await client.write_properties({"target_rpm": 1700, "rpm": 999})
    assert wrote == {"ok": True, "written": {"target_rpm": 1700}}  # rpm read-only, ignored
    assert pump.target_rpm == 1700
    allp = await client.read_all_properties()
    assert allp["target_rpm"] == 1700
    subset = await client.read_properties(["target_rpm"])
    assert subset == {"target_rpm": 1700}


async def test_bulk_falls_back_without_a_bulk_form():
    """A TD with no Thing-level bulk form still reads/writes every property via
    a per-property loop, so the bulk API is always functional."""
    pump = PumpDevice()
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:mini",
        "title": "Mini",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "properties": {
            "rpm": {"type": "integer", "readOnly": True, "forms": [{"href": "local://rpm"}]},
            "target_rpm": {"type": "integer", "forms": [{"href": "local://target_rpm"}]},
        },
    }
    client = ThingClient(tds=[td], bindings=[LocalBinding(pump)])
    pump.target_rpm = 42
    allp = await client.read_all_properties()
    assert allp == {"rpm": 0, "target_rpm": 42}
    await client.write_properties({"target_rpm": 7})
    assert pump.target_rpm == 7


async def test_async_lifecycle_completes(pump_client):
    import asyncio

    pump, client = pump_client
    # handle -> poll to completion (one job at a time so the setpoint is stable)
    handle = await client.invoke("pump__calibrate", {"target": 1300})
    assert handle.status == "running" and handle.href
    while not handle.terminal:
        await asyncio.sleep(0.05)
        handle = await client.query_action(handle)
    assert handle.status == "completed"
    assert handle.output == {"calibrated_to": 1300}
    assert pump.target_rpm == 1300
    # the wait=True convenience polls to a terminal state for you
    done = await client.invoke("pump__calibrate", {"target": 1400}, wait=True)
    assert done.status == "completed" and pump.target_rpm == 1400


async def test_async_lifecycle_cancel(pump_client):
    import asyncio

    pump, client = pump_client
    handle = await client.invoke("pump__calibrate", {"target": 99})
    cancelled = await client.cancel_action(handle)
    assert cancelled.status == "cancelled"
    await asyncio.sleep(0.3)
    assert (await client.query_action(handle)).status == "cancelled"


async def test_subscription_threshold_filters(pump_client):
    pump, client = pump_client
    pump.start_telemetry(temps=(70, 85, 99), period=0.05)
    got = []
    async for evt in await client.subscribe("pump__overheat", {"threshold": 90}):
        assert evt["temp"] >= 90
        got.append(evt["temp"])
        if len(got) >= 2:
            break
    assert got == [99, 99]


async def test_subscribe_authenticates_as_owner(pump_client):
    """The SSE endpoint requires the bearer token; the subscription succeeds
    only because subscribe threads the affordance owner for auth."""
    pump, client = pump_client
    pump.start_telemetry(temps=(95,), period=0.05)
    evt = await anext(aiter(await client.subscribe("pump__overheat")))
    assert evt == {"temp": 95, "limit": 80}


async def test_subscribe_unauthed_owner_gets_no_events():
    """Without credentials the authenticated SSE endpoint returns 401, so the
    stream is empty rather than silently unauthenticated."""
    import asyncio

    pump = PumpDevice()
    url, server = start_http_server(pump)
    td = pump_td(url, "localhost:1883")
    client = ThingClient(tds=[td], bindings=[LocalBinding(pump), HttpBinding()])
    try:
        pump.start_telemetry(temps=(95,), period=0.05)
        stream = await client.subscribe("pump__overheat")
        # 401 closes the stream (no events) rather than yielding unauthenticated:
        # either the iterator ends or nothing arrives in the window.
        with pytest.raises((StopAsyncIteration, asyncio.TimeoutError)):
            await asyncio.wait_for(anext(aiter(stream)), timeout=0.6)
    finally:
        server.shutdown()


async def test_additional_response_is_surfaced(pump_client):
    pump, client = pump_client
    pump.fault = True
    res = await client.invoke("pump__status")
    assert res["status"] == 503
    assert res["response"]["schema"] == "errorResponse"
    assert res["response"]["schemaDefinition"]["type"] == "object"


def test_semantic_validator_accepts_the_pump_td():
    td = json.loads(
        (Path(__file__).resolve().parents[1] / "examples" / "pump.td.json")
        .read_text()
        .replace("{BASE_URL}", "http://x")
        .replace("{MQTT_BROKER}", "b")
    )
    assert validate_semantics(td) == []


def test_semantic_validator_flags_problems():
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:bad",
        "title": "Bad",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {
            "go": {
                "forms": [{"href": "go/{id}", "op": ["subscribeevent"], "security": ["ghost_sc"]}]
            }
        },
    }
    problems = validate_semantics(td)
    joined = " ".join(problems)
    assert "uriVariable" in joined  # {id} undeclared
    assert "not legal" in joined  # subscribeevent on an action
    assert "ghost_sc" in joined  # undefined security


async def test_mcp_emits_output_schema_and_resource_templates(pump_client):
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    _, client = pump_client
    server = build_mcp_server(client, approve="elicit", approve_when="never", tool_mode="flat")
    async with connect(server) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        # a synchronous action advertises outputSchema; the async one does not
        # (its tool returns a status envelope, not the raw output)
        assert tools["pump__status"].outputSchema is not None
        assert tools["pump__calibrate"].outputSchema is None
        # unified surface: a cancel tool and a writable-property set tool
        assert "pump__calibrate__cancel" in tools
        assert "pump__target_rpm__set" in tools
        # a safe uriVariable read becomes a resource template
        tmpls = [t.uriTemplate for t in (await s.list_resource_templates()).resourceTemplates]
        assert "thing://pump__read_sensor/{id}" in tmpls
        rr = await s.read_resource("thing://pump__read_sensor/temp-1")
        assert json.loads(rr.contents[0].text) == {"id": "temp-1", "value": 72}


def test_strict_validation_raises_on_bad_td():
    from thingctx.validate import TDValidationError

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:bad2",
        "title": "Bad2",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {"go": {"forms": [{"href": "go/{missing}"}]}},
    }
    with pytest.raises(TDValidationError):
        ThingClient(tds=[td], bindings=[LocalBinding(PumpDevice())], validate="strict")
