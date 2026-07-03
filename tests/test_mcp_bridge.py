# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The MCP bridge: a TD becomes MCP tools, callable over a real session."""

from __future__ import annotations

import pytest

from thingctx import LocalBinding, ThingClient

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:pump:v1",
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {
        "status": {"idempotent": True, "forms": [{"href": "local://status"}]},
        "set_speed": {
            "input": {"type": "object", "properties": {"rpm": {"type": "integer"}}},
            "forms": [{"href": "local://set_speed"}],
        },
    },
}


@pytest.mark.asyncio
async def test_td_becomes_callable_mcp_tools():
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    inv = LocalBinding(
        {"status": lambda: {"rpm": 0}, "set_speed": lambda rpm=0: {"ok": True, "rpm": rpm}}
    )
    server = build_mcp_server(ThingClient(tds=[TD], bindings=[inv]), name="pump")
    async with connect(server) as s:
        await s.initialize()
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert "pump.set_speed" in tools
        # the risk hints come from the TD's own semantics
        assert tools["pump.status"].annotations.readOnlyHint is True
        # call a tool for real
        res = await s.call_tool("pump.set_speed", {"rpm": 1200})
        assert "1200" in res.content[0].text


TELEMETRY_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:pump",
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "properties": {
        "target_rpm": {
            "type": "integer",
            "observable": True,
            "forms": [
                {
                    "href": "local://target_rpm",
                    "op": ["readproperty", "writeproperty", "observeproperty"],
                }
            ],
        }
    },
    "actions": {
        "set_speed": {
            "input": {"type": "object", "properties": {"rpm": {"type": "integer"}}},
            "forms": [{"href": "local://set_speed"}],
        }
    },
    "events": {
        "overheat": {
            "data": {"type": "object"},
            "forms": [{"href": "local://overheat", "op": ["subscribeevent"]}],
        }
    },
}


class _Dev:
    """An in-process pump: a writable+observable setpoint, an action that can
    fail, and an event the test pushes by hand."""

    def __init__(self) -> None:
        self.target = 1000

    def get_target_rpm(self) -> int:
        return self.target

    def set_target_rpm(self, value: int) -> dict:
        self.target = value
        return {"ok": True, "target_rpm": value}

    def set_speed(self, rpm: int = 0) -> dict:
        if rpm > 5000:
            return {"error": "rpm too high"}
        return {"ok": True, "rpm": rpm}


@pytest.mark.asyncio
async def test_writable_property_becomes_set_tool_and_errors_signal():
    """A writable property surfaces as a ``<property>.set`` tool (MCP resources
    are read-only), and a runtime error is flagged ``isError`` rather than
    reported to the model as success."""
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    client = ThingClient(tds=[TELEMETRY_TD], bindings=[LocalBinding(_Dev())])
    server = build_mcp_server(client, name="pump", approve=None)
    async with connect(server) as s:
        await s.initialize()
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert "pump.target_rpm.set" in tools
        setter = tools["pump.target_rpm.set"]
        assert setter.annotations.readOnlyHint is False
        # the value is typed from the property's own schema
        assert setter.inputSchema["properties"]["value"]["type"] == "integer"

        # write through the tool, then read back through the property resource
        res = await s.call_tool("pump.target_rpm.set", {"value": 1500})
        assert res.isError is False
        assert res.structuredContent == {"ok": True, "target_rpm": 1500}
        read = await s.read_resource("thing://pump.target_rpm")
        assert "1500" in read.contents[0].text

        # a runtime error becomes isError (with the message), not a silent pass
        bad = await s.call_tool("pump.set_speed", {"rpm": 99999})
        assert bad.isError is True
        assert "too high" in bad.content[0].text


@pytest.mark.asyncio
async def test_events_and_observables_are_subscribable_resources():
    """WoT events and observable properties map to subscribable MCP resources:
    a push becomes resources/updated, and the client re-reads the URI (the
    latest event payload, or the live property value)."""
    pytest.importorskip("mcp")
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    dev = _Dev()
    binding = LocalBinding(dev)
    server = build_mcp_server(
        ThingClient(tds=[TELEMETRY_TD], bindings=[binding]), name="pump", approve=None
    )

    updated: list[str] = []

    async def on_message(msg):
        node = getattr(msg, "root", msg)
        if type(node).__name__ == "ResourceUpdatedNotification":
            updated.append(str(node.params.uri))

    async with connect(server, message_handler=on_message) as s:
        await s.initialize()
        resources = {r.name: str(r.uri) for r in (await s.list_resources()).resources}
        assert resources["pump.overheat"] == "event://pump.overheat"
        assert resources["pump.target_rpm"] == "thing://pump.target_rpm"

        # an event: subscribe, the device emits one, a resources/updated arrives,
        # and reading the URI drains the buffered payload(s) (events have no live read)
        await s.subscribe_resource("event://pump.overheat")
        binding.emit("overheat", {"temp": 99, "limit": 80})
        await asyncio.sleep(0.05)
        assert "event://pump.overheat" in updated
        read = await s.read_resource("event://pump.overheat")
        assert "99" in read.contents[0].text

        # an observable property: an external change pushes an update, and the
        # re-read reflects the new live value
        updated.clear()
        await s.subscribe_resource("thing://pump.target_rpm")
        dev.target = 3000
        binding.emit("target_rpm", 3000)
        await asyncio.sleep(0.05)
        assert "thing://pump.target_rpm" in updated
        read = await s.read_resource("thing://pump.target_rpm")
        assert "3000" in read.contents[0].text


@pytest.mark.asyncio
async def test_event_buffer_delivers_burst_in_order_and_flags_drops():
    """A burst of events between reads is delivered whole and in order, bounded
    by the ring size; occurrences shed beyond the ring are counted in ``dropped``,
    and a monotonic ``seq`` lets the client detect a gap rather than miss it
    silently."""
    pytest.importorskip("mcp")
    import asyncio
    import json

    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    binding = LocalBinding(_Dev())
    server = build_mcp_server(
        ThingClient(tds=[TELEMETRY_TD], bindings=[binding]),
        name="pump",
        approve=None,
        event_history=3,
    )
    uri = "event://pump.overheat"
    async with connect(server) as s:
        await s.initialize()
        await s.subscribe_resource(uri)
        await asyncio.sleep(0.02)  # let the relay establish before emitting

        for i in range(1, 6):  # five events, ring holds three
            binding.emit("overheat", {"n": i})
        await asyncio.sleep(0.05)

        batch = json.loads((await s.read_resource(uri)).contents[0].text)
        assert [v["n"] for v in batch["values"]] == [3, 4, 5]  # newest three, in order
        assert batch["count"] == 3
        assert batch["dropped"] == 2  # n=1 and n=2 shed before the read
        assert batch["seq"] == 5

        # the ring is drained; a second burst continues the same sequence
        for i in range(6, 8):
            binding.emit("overheat", {"n": i})
        await asyncio.sleep(0.05)
        batch2 = json.loads((await s.read_resource(uri)).contents[0].text)
        assert [v["n"] for v in batch2["values"]] == [6, 7]
        assert batch2["dropped"] == 0
        assert batch2["seq"] == 7

        # nothing new since the last read: empty, not a stale repeat
        empty = json.loads((await s.read_resource(uri)).contents[0].text)
        assert empty.get("pending") is True


@pytest.mark.asyncio
async def test_subscription_relay_deregisters_so_resubscribe_resumes():
    """A finished stream (or a dropped connection) must leave the resource
    subscribable again: the relay always deregisters on exit, so a second
    subscribe starts a fresh relay rather than silently doing nothing."""
    pytest.importorskip("mcp")
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    class _FiniteEvents:
        """A 'local' binding whose event stream yields one value then ends."""

        scheme = "local"

        def __init__(self) -> None:
            self.opens = 0

        async def subscribe(self, target, form, args=None):  # noqa: ANN001
            self.opens += 1
            n = self.opens

            async def _one():
                yield {"n": n}

            return _one()

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:pump",
        "title": "Pump",
        "events": {
            "overheat": {
                "data": {"type": "object"},
                "forms": [{"href": "local://overheat", "op": ["subscribeevent"]}],
            }
        },
    }
    server = build_mcp_server(ThingClient(tds=[td], bindings=[_FiniteEvents()]), approve=None)

    updated: list[str] = []

    async def on_message(msg):
        node = getattr(msg, "root", msg)
        if type(node).__name__ == "ResourceUpdatedNotification":
            updated.append(str(node.params.uri))

    uri = "event://pump.overheat"
    async with connect(server, message_handler=on_message) as s:
        await s.initialize()
        await s.subscribe_resource(uri)
        await asyncio.sleep(0.05)
        assert updated.count(uri) == 1
        assert "1" in (await s.read_resource(uri)).contents[0].text

        # the stream has ended; subscribing again starts a fresh relay
        await s.subscribe_resource(uri)
        await asyncio.sleep(0.05)
        assert updated.count(uri) == 2
        assert "2" in (await s.read_resource(uri)).contents[0].text


CAMERA_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:cam:v1",
    "title": "Camera",
    "actions": {"watch": {"forms": [{"href": "rtsp://cam/stream", "x-thingctx-media": {"k": 1}}]}},
}


@pytest.mark.asyncio
async def test_media_td_becomes_snapshot_image_tool():
    pytest.importorskip("mcp")
    pytest.importorskip("PIL")
    import threading

    import numpy as np
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.bindings.builtin.media import Frame, MediaBinding
    from thingctx.integrations.mcp import build_mcp_server

    class _FakeBackend:
        def can_open(self, url, hint):
            return True

        def read(self, url, *, options, stop: threading.Event):
            yield Frame(data=np.zeros((4, 4, 3), dtype=np.uint8), kind="video", pts=0.0)

        def write(self, *a, **k):
            raise NotImplementedError

    client = ThingClient(tds=[CAMERA_TD], bindings=[MediaBinding(backends=[_FakeBackend()])])
    server = build_mcp_server(client, name="cam")
    media_name = client.list_media()[0]  # e.g. "cam.watch"
    snapshot = f"{media_name.split('.', 1)[0]}.snapshot"  # becomes "cam.snapshot"

    async with connect(server) as s:
        await s.initialize()
        tools = {t.name: t for t in (await s.list_tools()).tools}
        # the stream surfaces as a read only snapshot tool, not the stream name
        # and not an invoke action
        assert snapshot in tools
        assert media_name not in tools
        assert tools[snapshot].annotations.readOnlyHint is True
        # calling it returns one frame as MCP image content
        res = await s.call_tool(snapshot, {})
        assert res.content[0].type == "image"
        assert res.content[0].mimeType == "image/jpeg"
        assert res.content[0].data  # base64 jpeg


@pytest.mark.asyncio
async def test_media_snapshot_can_return_a_clip():
    """frames > 1 turns the snapshot tool into a short clip: several image
    blocks sampled over time (MCP has no video content type)."""
    pytest.importorskip("mcp")
    pytest.importorskip("PIL")
    import threading

    import numpy as np
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.bindings.builtin.media import Frame, MediaBinding
    from thingctx.integrations.mcp import build_mcp_server

    class _ClipBackend:
        def can_open(self, url, hint):
            return True

        def read(self, url, *, options, stop: threading.Event):
            for i in range(20):
                yield Frame(data=np.zeros((4, 4, 3), dtype=np.uint8), kind="video", pts=float(i))

        def write(self, *a, **k):
            raise NotImplementedError

    # A clip is a lossless sample: pace the source to the consumer ("all") so a
    # bursty finite backend can't shed frames before they're sampled.
    media = MediaBinding(backends=[_ClipBackend()], backpressure="all")
    client = ThingClient(tds=[CAMERA_TD], bindings=[media])
    server = build_mcp_server(client, name="cam")
    snapshot = f"{client.list_media()[0].split('.', 1)[0]}.snapshot"

    async with connect(server) as s:
        await s.initialize()
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert "frames" in tools[snapshot].inputSchema["properties"]
        res = await s.call_tool(snapshot, {"frames": 3, "every": 2.0})
        images = [c for c in res.content if c.type == "image"]
        assert len(images) == 3
        assert all(c.mimeType == "image/jpeg" and c.data for c in images)
