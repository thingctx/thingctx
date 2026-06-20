"""Trust layer: approval gating + verify() grounding, over LocalInvoker."""

from __future__ import annotations

import pytest

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
        "set_speed": {  # safe (idempotent), not gated under "declared"
            "idempotent": True,
            "input": {"type": "object", "properties": {"rpm": {"type": "integer"}}},
            "forms": [{"href": "local://set_speed"}],
        },
        "estop": {  # TD-declared destructive
            "@type": "tc:Destructive",
            "forms": [{"href": "local://estop"}],
        },
        "drain": {"forms": [{"href": "local://drain"}]},  # non-idempotent, undeclared
    },
}


class Pump:
    def __init__(self):
        self.rpm = 1200
        self.target_rpm = 0

    def set_speed(self, rpm=0):
        return {"ok": True, "rpm": rpm}

    def estop(self):
        return {"stopped": True}

    def drain(self):
        return {"drained": True}


def _client(**kw):
    return ThingClient(tds=[TD], invokers=[LocalInvoker(Pump())], **kw)


@pytest.mark.asyncio
async def test_declared_destructive_blocked_without_approver():
    res = await _client().invoke("pump.estop")  # default approve_when="declared"
    assert "approval required" in res["error"]


@pytest.mark.asyncio
async def test_declared_destructive_allowed_when_approved():
    res = await _client(approve=lambda req: True).invoke("pump.estop")
    assert res == {"stopped": True}


@pytest.mark.asyncio
async def test_declared_destructive_denied():
    res = await _client(approve=lambda req: False).invoke("pump.estop")
    assert res["error"] == "approval denied"


@pytest.mark.asyncio
async def test_safe_action_not_gated():
    seen = []
    client = _client(approve=lambda req: seen.append(req) or True)
    res = await client.invoke("pump.set_speed", {"rpm": 900})
    assert res == {"ok": True, "rpm": 900}
    assert seen == []  # approver never consulted for a safe action


@pytest.mark.asyncio
async def test_destructive_policy_gates_non_idempotent():
    assert (
        "approval required"
        in (await _client(approve_when="destructive").invoke("pump.drain"))["error"]
    )
    ok = await _client(approve_when="destructive", approve=lambda req: True).invoke("pump.drain")
    assert ok == {"drained": True}


@pytest.mark.asyncio
async def test_all_policy_gates_even_safe_action():
    seen = []
    client = _client(approve_when="all", approve=lambda req: seen.append(req.tool_name) or True)
    await client.invoke("pump.set_speed", {"rpm": 900})
    assert seen == ["pump.set_speed"]


@pytest.mark.asyncio
async def test_never_policy_disables_gate():
    res = await _client(approve_when="never").invoke("pump.estop")  # no approver, still runs
    assert res == {"stopped": True}


@pytest.mark.asyncio
async def test_async_approver():
    async def approve(req):
        return True

    assert await _client(approve=approve).invoke("pump.estop") == {"stopped": True}


@pytest.mark.asyncio
async def test_property_write_gated_under_all():
    blocked = await _client(approve_when="all").write_property("pump.target_rpm", 1500)
    assert "approval required" in blocked["error"]


@pytest.mark.asyncio
async def test_verify_ok():
    reports = await _client().verify()
    assert len(reports) == 1
    assert reports[0].ok, reports[0].as_dict()


@pytest.mark.asyncio
async def test_verify_detects_type_mismatch():
    bad = {
        **TD,
        "properties": {
            "rpm": {"type": "string", "readOnly": True, "forms": [{"href": "local://rpm"}]}
        },
    }
    # device returns int 1200, TD now declares string -> mismatch
    client = ThingClient(tds=[bad], invokers=[LocalInvoker(Pump())])
    report = (await client.verify())[0]
    assert not report.ok
    assert any("declared string" in c.detail for c in report.checks)


@pytest.mark.asyncio
async def test_verify_detects_unreadable_property():
    bad = {
        **TD,
        "properties": {
            "ghost": {"type": "integer", "readOnly": True, "forms": [{"href": "local://ghost"}]}
        },
    }
    client = ThingClient(tds=[bad], invokers=[LocalInvoker(Pump())])  # no 'ghost' on device
    report = (await client.verify())[0]
    assert not report.ok


@pytest.mark.asyncio
async def test_verify_does_not_flag_binary_media():
    # an image property reads as bytes; declared "string" must NOT be drift
    td = {
        **TD,
        "properties": {
            "frame": {
                "type": "string",
                "readOnly": True,
                "contentMediaType": "image/png",
                "forms": [{"href": "local://frame"}],
            }
        },
    }
    inv = LocalInvoker({"frame": lambda: b"\x89PNG\r\n"})
    report = (await ThingClient(tds=[td], invokers=[inv]).verify())[0]
    assert report.ok, report.as_dict()
