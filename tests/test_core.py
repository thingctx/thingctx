"""The core: parse a TD, invoke/read/write/subscribe over a local invoker."""

from __future__ import annotations

import pytest

import thingctx
from thingctx import LocalInvoker, ThingClient

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:pump:v1",
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "properties": {
        "rpm": {"type": "integer", "readOnly": True, "forms": [{"href": "local://rpm"}]},
        "target_rpm": {"type": "integer", "forms": [{"href": "local://target_rpm"}]},
    },
    "actions": {
        "set_speed": {
            "input": {"type": "object", "properties": {"rpm": {"type": "integer"}}},
            "forms": [{"href": "local://set_speed"}],
        }
    },
}


class Pump:
    def __init__(self):
        self._rpm = 0
        self.target_rpm = 0  # plain attribute (LocalInvoker setattr path)

    def rpm(self):
        return self._rpm

    def set_speed(self, rpm=0):
        self._rpm = rpm
        return {"ok": True, "rpm": rpm}


@pytest.fixture
def client():
    p = Pump()
    return ThingClient(tds=[TD], invokers=[LocalInvoker(p)]), p


def test_tools_are_listed(client):
    c, _ = client
    names = [s["function"]["name"] for s in c.list_actions()]
    assert "pump.set_speed" in names


@pytest.mark.asyncio
async def test_invoke_routes_to_the_device(client):
    c, p = client
    out = await c.invoke("pump.set_speed", {"rpm": 1200})
    assert out == {"ok": True, "rpm": 1200}
    assert p._rpm == 1200


@pytest.mark.asyncio
async def test_read_property(client):
    c, p = client
    p._rpm = 900
    assert await c.read_property("pump.rpm") == 900


@pytest.mark.asyncio
async def test_write_property(client):
    c, p = client
    await c.write_property("pump.target_rpm", 1500)
    assert p.target_rpm == 1500


def test_td_validates():
    assert thingctx.validate_td(TD) == []


async def test_unknown_action_errors(client):
    c, _ = client
    r = await c.invoke("pump.nope")
    assert "unknown action" in r["error"]
