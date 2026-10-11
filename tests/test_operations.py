# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Every read, write, command, query and cancel a client runs, and every one a gate
refuses, reaches the client's ``record`` callable as one Operation. These tests pin
what the runtime reports, not what a record model makes of it."""

from __future__ import annotations

from datetime import timezone

import pytest

from thingctx import ThingClient
from thingctx.authz.pdp import AuthorizationDenied, LocalPolicyGrantSource, PolicyDecisionPoint
from thingctx.authz.vocabulary import build_vocabulary
from thingctx.lifecycle import ActionStatus
from thingctx.operations import Operation
from thingctx.thing import parse_thing

THING = "urn:demo:valve"
TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": THING,
    "title": "Valve",
    "version": {"instance": "1.2.0"},
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "properties": {
        "level": {
            "type": "number",
            "observable": True,
            "forms": [{"href": "cap://level", "op": ["readproperty", "writeproperty"]}],
        },
    },
    "actions": {
        "open": {
            "input": {"type": "object", "properties": {"turns": {"type": "integer", "default": 1}}},
            "forms": [{"href": "cap://open"}],
        },
        "purge": {"forms": [{"href": "cap://purge"}]},
        "fill": {
            "synchronous": False,
            "forms": [
                {"href": "cap://fill", "op": ["invokeaction", "queryaction", "cancelaction"]}
            ],
        },
    },
}
GRANTS = {
    (THING, "level", "readproperty"),
    (THING, "level", "writeproperty"),
    (THING, "open", "invokeaction"),
    (THING, "fill", "invokeaction"),
    (THING, "fill", "queryaction"),
    (THING, "fill", "cancelaction"),
}


class CaptureBinding:
    scheme = "cap"

    def __init__(self):
        self.fail = False

    def with_things(self, things):
        return self

    async def read(self, prop, form):
        return 4.2

    async def write(self, prop, form, value):
        return {"ok": True}

    async def invoke(self, action, form, arguments):
        if self.fail:
            raise RuntimeError("valve jammed")
        return {"ok": True}

    async def invoke_async(self, action, form, arguments):
        return ActionStatus(
            status="running",
            href="urn:uuid:1",
            form=form,
            thing_id=action.thing_id,
            name=action.name,
        )

    async def query_action(self, status):
        return ActionStatus(
            status="completed",
            output={"filled": True},
            href=status.href,
            form=status.form,
            thing_id=status.thing_id,
            name=status.name,
        )

    async def cancel_action(self, status):
        return ActionStatus(
            status="cancelled",
            href=status.href,
            form=status.form,
            thing_id=status.thing_id,
            name=status.name,
        )


def _pdp():
    return PolicyDecisionPoint(
        build_vocabulary(parse_thing(TD)), LocalPolicyGrantSource({"operator": GRANTS})
    )


def _client(sink, *, authz_raise=False, approve_when="never", binding=None):
    base = ThingClient(tds=[TD], bindings=[binding or CaptureBinding()], approve_when=approve_when)
    return base.guarded(
        _pdp(), identity={"roles": ["operator"]}, authz_raise=authz_raise, record=sink
    )


async def test_a_permitted_invoke_is_reported_with_its_filled_payload():
    seen: list[Operation] = []
    client = _client(seen.append)
    assert await client.invoke("valve__open", {}) == {"ok": True}
    (op,) = seen
    assert (op.thing_id, op.affordance, op.op) == (THING, "open", "invokeaction")
    assert op.permitted is True
    assert op.arguments == {"turns": 1}
    assert op.result == {"ok": True}
    assert op.error is None
    assert op.href == "cap://open"
    assert op.thing_version == "1.2.0"
    assert op.identity == {"roles": ["operator"]}
    assert op.started.tzinfo is timezone.utc
    assert op.started <= op.ended


async def test_reads_and_writes_are_reported():
    seen: list[Operation] = []
    client = _client(seen.append)
    assert await client.read_property("valve__level") == 4.2
    await client.write_property("valve__level", 7)
    read, write = seen
    assert (read.op, read.arguments, read.result) == ("readproperty", None, 4.2)
    assert (write.op, write.arguments, write.result) == ("writeproperty", 7, {"ok": True})
    assert read.href == write.href == "cap://level"


async def test_a_refusal_is_reported_and_returned_as_an_envelope():
    seen: list[Operation] = []
    client = _client(seen.append)
    out = await client.invoke("valve__purge", {})
    assert out["error"] == "authorization denied"
    (op,) = seen
    assert op.permitted is False
    assert op.reason == out["reason"]
    assert op.result is None
    assert op.arguments == {}
    assert op.href == "cap://purge"


async def test_a_refusal_is_reported_before_it_raises():
    seen: list[Operation] = []
    client = _client(seen.append, authz_raise=True)
    with pytest.raises(AuthorizationDenied):
        await client.invoke("valve__purge", {})
    assert [op.permitted for op in seen] == [False]


async def test_a_refused_stream_is_reported():
    seen: list[Operation] = []
    client = _client(seen.append)
    await client.subscribe("valve__level")
    (op,) = seen
    assert (op.op, op.permitted) == ("observeproperty", False)


async def test_an_approve_gate_block_is_reported_as_not_permitted():
    seen: list[Operation] = []
    client = _client(seen.append, approve_when="all")
    out = await client.invoke("valve__open", {})
    assert "error" in out
    (op,) = seen
    assert op.permitted is False
    assert op.reason
    assert op.result == out


async def test_a_transport_failure_is_reported_and_re_raised():
    seen: list[Operation] = []
    binding = CaptureBinding()
    binding.fail = True
    client = _client(seen.append, binding=binding)
    with pytest.raises(RuntimeError, match="valve jammed"):
        await client.invoke("valve__open", {})
    (op,) = seen
    assert (op.permitted, op.result, op.error) == (True, None, "valve jammed")


async def test_a_long_running_action_is_reported_at_each_step():
    seen: list[Operation] = []
    client = _client(seen.append)
    handle = await client.invoke("valve__fill", {}, wait=False)
    assert handle.status == "running"
    done = await client.query_action(handle)
    assert done.status == "completed"
    stopped = await client.cancel_action(handle)
    assert stopped.status == "cancelled"
    assert [op.op for op in seen] == ["invokeaction", "queryaction", "cancelaction"]
    assert [op.result.status for op in seen] == ["running", "completed", "cancelled"]
    assert {op.href for op in seen} == {"cap://fill", "urn:uuid:1"}


async def test_a_blocking_invoke_reports_the_final_status_once():
    seen: list[Operation] = []
    client = _client(seen.append)
    final = await client.invoke("valve__fill", {}, wait=True, timeout=2)
    assert final.status == "completed"
    assert [(op.op, op.result.status) for op in seen] == [("invokeaction", "completed")]


async def test_without_a_gate_every_operation_is_still_reported():
    seen: list[Operation] = []
    client = ThingClient(
        tds=[TD], bindings=[CaptureBinding()], approve_when="never", record=seen.append
    )
    await client.invoke("valve__purge", {})
    (op,) = seen
    assert (op.permitted, op.reason, op.identity) == (True, None, None)


async def test_an_async_record_callable_is_awaited():
    seen: list[Operation] = []

    async def record(operation: Operation) -> None:
        seen.append(operation)

    client = _client(record)
    await client.read_property("valve__level")
    assert len(seen) == 1


async def test_guarded_keeps_the_record_callable_of_its_base():
    seen: list[Operation] = []
    base = ThingClient(
        tds=[TD], bindings=[CaptureBinding()], approve_when="never", record=seen.append
    )
    client = base.guarded(_pdp(), identity={"roles": ["operator"]}, authz_raise=False)
    await client.read_property("valve__level")
    assert len(seen) == 1
