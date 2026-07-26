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
    server = build_mcp_server(ThingClient(tds=[TD], bindings=[inv]), name="pump", tool_mode="flat")
    async with connect(server) as s:
        await s.initialize()
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert "pump__set_speed" in tools
        # the risk hints come from the TD's own semantics
        assert tools["pump__status"].annotations.readOnlyHint is True
        # call a tool for real
        res = await s.call_tool("pump__set_speed", {"rpm": 1200})
        assert "1200" in res.content[0].text


@pytest.mark.asyncio
async def test_gateway_mode_projects_verbs_and_routes_invoke():
    """Gateway mode collapses per-action tools to a constant verb surface. The
    verbs are listed instead of pump__status/pump__set_speed, and invoke_action
    routes back to the real action."""
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    inv = LocalBinding(
        {"status": lambda: {"rpm": 0}, "set_speed": lambda rpm=0: {"ok": True, "rpm": rpm}}
    )
    server = build_mcp_server(
        ThingClient(tds=[TD], bindings=[inv]), name="pump", tool_mode="gateway"
    )
    async with connect(server) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        # the constant verb surface, not one tool per action
        assert "invoke_action" in names
        assert "search_things" in names
        assert "describe" in names
        # per-action tools are NOT listed in gateway mode
        assert "pump__set_speed" not in names
        assert "pump__status" not in names
        # events are read only through the background trio; there is no separate
        # collect verb (one event-read model, no transport-vs-intent duplication)
        assert "subscribe_event" not in names
        assert "start_subscription" in names
        # search finds the Thing by keyword
        found = await s.call_tool("search_things", {"query": "pump"})
        assert "pump" in found.content[0].text
        # invoke_action routes through to the real action
        res = await s.call_tool(
            "invoke_action",
            {"thing_id": "pump", "action": "set_speed", "arguments": {"rpm": 1200}},
        )
        assert "1200" in res.content[0].text


@pytest.mark.asyncio
async def test_auto_mode_is_flat_for_small_fleet_gateway_for_large():
    """The default tool mode is auto: a small fleet stays flat (per-Thing names
    like pump__set_speed match user intent and resist the bypass), a large fleet
    flips to the constant gateway surface. Selected by the flat tool count."""
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    inv = LocalBinding(
        {"status": lambda: {"rpm": 0}, "set_speed": lambda rpm=0: {"ok": True, "rpm": rpm}}
    )
    # Small fleet (2 actions) with the default (no tool_mode) -> flat.
    small = build_mcp_server(ThingClient(tds=[TD], bindings=[inv]))
    async with connect(small) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        assert "pump__set_speed" in names  # per-Thing name, intent-matching
        assert "invoke_action" not in names  # not the generic verb

    # Large fleet (> FLAT_MAX actions) with the default -> gateway.
    def _many(i):
        return {
            "@context": "https://www.w3.org/2022/wot/td/v1.1",
            "id": f"urn:demo:m{i}",
            "title": f"m{i}",
            "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
            "security": ["nosec_sc"],
            "actions": {f"a{j}": {"forms": [{"href": f"local://a{j}"}]} for j in range(4)},
        }

    big = build_mcp_server(ThingClient(tds=[_many(i) for i in range(20)], bindings=[]))
    async with connect(big) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        assert "invoke_action" in names  # gateway verb
        assert not any(n.startswith("m0__") for n in names)  # no per-Thing tools


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
    server = build_mcp_server(client, name="pump", approve=None, tool_mode="flat")
    async with connect(server) as s:
        await s.initialize()
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert "pump__target_rpm__set" in tools
        setter = tools["pump__target_rpm__set"]
        assert setter.annotations.readOnlyHint is False
        # the value is typed from the property's own schema
        assert setter.inputSchema["properties"]["value"]["type"] == "integer"

        # write through the tool, then read back through the property resource
        res = await s.call_tool("pump__target_rpm__set", {"value": 1500})
        assert res.isError is False
        assert res.structuredContent == {"ok": True, "target_rpm": 1500}
        read = await s.read_resource("thing://pump__target_rpm")
        assert "1500" in read.contents[0].text

        # a runtime error becomes isError (with the message), not a silent pass
        bad = await s.call_tool("pump__set_speed", {"rpm": 99999})
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
        assert resources["pump__overheat"] == "event://pump__overheat"
        assert resources["pump__target_rpm"] == "thing://pump__target_rpm"

        # an event: subscribe, the device emits one, a resources/updated arrives,
        # and reading the URI drains the buffered payload(s) (events have no live read)
        await s.subscribe_resource("event://pump__overheat")
        binding.emit("overheat", {"temp": 99, "limit": 80})
        await asyncio.sleep(0.05)
        assert "event://pump__overheat" in updated
        read = await s.read_resource("event://pump__overheat")
        assert "99" in read.contents[0].text

        # an observable property: an external change pushes an update, and the
        # re-read reflects the new live value
        updated.clear()
        await s.subscribe_resource("thing://pump__target_rpm")
        dev.target = 3000
        binding.emit("target_rpm", 3000)
        await asyncio.sleep(0.05)
        assert "thing://pump__target_rpm" in updated
        read = await s.read_resource("thing://pump__target_rpm")
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
    uri = "event://pump__overheat"
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

        async def subscribe(self, target, form, args=None):
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

    uri = "event://pump__overheat"
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


@pytest.mark.asyncio
async def test_gateway_start_subscription_fills_event_urivariables():
    """start_subscription must fill the event's uriVariables (e.g. topic) into the
    form href before subscribing, so a parameterized event is reachable. There is no
    separate collect verb: the trio is the one event-read model over MCP."""
    pytest.importorskip("mcp")
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    seen: dict = {}

    class _Events:
        scheme = "local"

        async def subscribe(self, target, form, args=None):
            # The topic uriVariable is filled into the href (form.fill) before the
            # binding is called, so it arrives on the form, not in args.
            seen["href"] = form.href

            async def _gen():
                yield {"n": 0}

            return _gen()

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:bus",
        "title": "Bus",
        "events": {
            "feed": {
                "uriVariables": {"topic": {"type": "string"}},
                "data": {"type": "object"},
                "forms": [{"href": "local://feed/{+topic}", "op": ["subscribeevent"]}],
            }
        },
    }
    server = build_mcp_server(
        ThingClient(tds=[td], bindings=[_Events()]), approve=None, tool_mode="gateway"
    )
    import json as _json

    async with connect(server) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        assert "subscribe_event" not in names  # no collect verb
        assert "start_subscription" in names
        r = await s.call_tool(
            "start_subscription",
            {"thing_id": "bus", "event": "feed", "arguments": {"topic": "orders"}},
        )
        sid = _json.loads(r.content[0].text)["subscription_id"]
        await asyncio.sleep(0.05)
        await s.call_tool("stop_subscription", {"subscription_id": sid})
    # the event's uriVariable was filled into the form href before subscribe
    assert seen["href"] == "local://feed/orders"


@pytest.mark.asyncio
async def test_gateway_background_subscription_buffers_between_reads():
    """start_subscription buffers an event's messages as they arrive; a later
    read_subscription drains what accumulated between reads; stop ends it. This is
    the 'receive anytime' path (vs subscribe_event's in-flight-only collect)."""
    pytest.importorskip("mcp")
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    push = asyncio.Queue()

    class _PushEvents:
        scheme = "local"

        async def subscribe(self, target, form, args=None):
            async def _gen():
                while True:
                    yield await push.get()

            return _gen()

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:bus",
        "title": "Bus",
        "events": {
            "feed": {
                "data": {"type": "object"},
                "forms": [{"href": "local://feed", "op": ["subscribeevent"]}],
            }
        },
    }
    server = build_mcp_server(
        ThingClient(tds=[td], bindings=[_PushEvents()]), approve=None, tool_mode="gateway"
    )
    import json as _json

    async def tool(s, name, args):
        r = await s.call_tool(name, args)
        return _json.loads(r.content[0].text)

    async with connect(server) as s:
        await s.initialize()
        started = await tool(s, "start_subscription", {"thing_id": "bus", "event": "feed"})
        sid = started["subscription_id"]
        # nothing pushed yet: an immediate read is empty
        await asyncio.sleep(0.05)
        r0 = await tool(s, "read_subscription", {"subscription_id": sid})
        assert r0["count"] == 0
        # push 2 messages "between" reads
        await push.put({"n": 1})
        await push.put({"n": 2})
        await asyncio.sleep(0.05)
        r1 = await tool(s, "read_subscription", {"subscription_id": sid})
        assert r1["messages"] == [{"n": 1}, {"n": 2}]
        assert r1["dropped"] == 0
        # already drained
        r2 = await tool(s, "read_subscription", {"subscription_id": sid})
        assert r2["count"] == 0
        stopped = await tool(s, "stop_subscription", {"subscription_id": sid})
        assert stopped["stopped"] is True


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
    server = build_mcp_server(client, name="cam", tool_mode="flat")
    media_name = client.list_media()[0]  # e.g. "cam__watch"
    from thingctx.thing import TOOL_SEP, _tool_slug

    snapshot = f"{_tool_slug(media_name)}{TOOL_SEP}snapshot"

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
async def test_gateway_snapshot_verb_captures_media_and_hides_per_thing_tool():
    """In gateway mode media is the single `snapshot` VERB (one surface shape for
    every affordance), not a per-Thing <slug>__snapshot tool. describe flags the
    affordance as media so the model uses snapshot, not subscribe_event."""
    pytest.importorskip("mcp")
    pytest.importorskip("PIL")
    import json as _json
    import threading

    import numpy as np
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.bindings.builtin.media import Frame, MediaBinding
    from thingctx.integrations.mcp import build_mcp_server

    class _FakeBackend:
        def can_open(self, url, hint):
            return True

        def read(self, url, *, options, stop: threading.Event):
            yield Frame(data=np.zeros((4, 4, 3), dtype=np.uint8), kind="video", pts=None)

        def write(self, *a, **k):
            raise NotImplementedError

    client = ThingClient(tds=[CAMERA_TD], bindings=[MediaBinding(backends=[_FakeBackend()])])
    server = build_mcp_server(client, name="cam", tool_mode="gateway")
    async with connect(server) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        assert "snapshot" in names  # the media verb
        assert not any(n.endswith("__snapshot") for n in names)  # no per-Thing tool
        # describe steers the model: watch is media, use snapshot not subscribe
        d = _json.loads(
            (await s.call_tool("describe", {"thing_id": "cam", "affordance": "watch"}))
            .content[0]
            .text
        )
        assert d.get("media") is True
        assert "snapshot" in d.get("how_to_read", "")
        # the verb returns an image
        res = await s.call_tool("snapshot", {"thing_id": "cam", "affordance": "watch"})
        assert res.content[0].type == "image"
        assert res.content[0].mimeType == "image/jpeg"


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
    server = build_mcp_server(client, name="cam", tool_mode="flat")
    from thingctx.thing import TOOL_SEP, _tool_slug

    snapshot = f"{_tool_slug(client.list_media()[0])}{TOOL_SEP}snapshot"

    async with connect(server) as s:
        await s.initialize()
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert "frames" in tools[snapshot].inputSchema["properties"]
        res = await s.call_tool(snapshot, {"frames": 3, "every": 2.0})
        images = [c for c in res.content if c.type == "image"]
        assert len(images) == 3
        assert all(c.mimeType == "image/jpeg" and c.data for c in images)


# A media form whose href carries uriVariables (the registry pattern, e.g.
# rtsp://{+host}:8554/{+path}) must have them filled from the snapshot tool's
# arguments before the stream opens; otherwise the backend gets a literal
# "{+host}" and fails.
_MEDIA_URIVAR_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:cam:vars",
    "title": "Camera with vars",
    "events": {
        "watch": {
            "data": {"type": "string", "contentMediaType": "video/x-thingctx-media"},
            "uriVariables": {
                "host": {"type": "string"},
                "path": {"type": "string"},
            },
            "forms": [
                {
                    "href": "rtsp://{+host}:8554/{+path}",
                    "op": "subscribeevent",
                    "contentType": "video/x-thingctx-media",
                }
            ],
        }
    },
}


@pytest.mark.asyncio
async def test_media_snapshot_fills_href_urivariables_from_args():
    pytest.importorskip("mcp")
    pytest.importorskip("PIL")
    import threading

    import numpy as np
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.bindings.builtin.media import Frame, MediaBinding
    from thingctx.integrations.mcp import build_mcp_server

    seen: dict = {}

    class _RecordingBackend:
        def can_open(self, url, hint):
            return True

        def read(self, url, *, options, stop: threading.Event):
            seen["url"] = url
            yield Frame(data=np.zeros((4, 4, 3), dtype=np.uint8), kind="video", pts=None)

        def write(self, *a, **k):
            raise NotImplementedError

    client = ThingClient(
        tds=[_MEDIA_URIVAR_TD], bindings=[MediaBinding(backends=[_RecordingBackend()])]
    )
    server = build_mcp_server(client, name="cam", tool_mode="flat")
    from thingctx.thing import TOOL_SEP, _tool_slug

    snapshot = f"{_tool_slug(client.list_media()[0])}{TOOL_SEP}snapshot"

    async with connect(server) as s:
        await s.initialize()
        res = await s.call_tool(snapshot, {"host": "10.0.0.5", "path": "front"})
        assert res.content[0].type == "image"
    # the uriVariables reached the backend filled, not as literal "{+host}"
    assert seen["url"] == "rtsp://10.0.0.5:8554/front"


async def test_server_reports_thingctx_version_not_the_sdk():
    """A host shows serverInfo in its UI and logs, so a bug report from Claude
    Desktop must name thingctx's release, not whatever MCP SDK is installed."""
    pytest.importorskip("mcp")
    import importlib.metadata as md

    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from thingctx.integrations.mcp import build_mcp_server

    inv = LocalBinding({"status": lambda: {"rpm": 0}})
    server = build_mcp_server(ThingClient(tds=[TD], bindings=[inv]), name="pump", tool_mode="flat")
    async with connect(server) as s:
        info = (await s.initialize()).serverInfo
    # Expected with the same fallback the bridge uses, so a source checkout with
    # no installed metadata still runs this. Comparing against the SDK version
    # would fail the day the two releases happen to share a string.
    try:
        expected = md.version("thingctx")
    except md.PackageNotFoundError:
        expected = "unknown"
    assert info.version == expected


def test_version_falls_back_when_metadata_is_absent(monkeypatch):
    """A checkout with no installed metadata still has to start the bridge."""
    pytest.importorskip("mcp")
    import importlib.metadata as md

    from thingctx.integrations import mcp as bridge

    def _missing(_name: str) -> str:
        raise md.PackageNotFoundError("thingctx")

    monkeypatch.setattr(md, "version", _missing)
    assert bridge._thingctx_version() == "unknown"
