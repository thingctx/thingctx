# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""An action's arguments and a written value are checked against the TD's schema
(TD 1.1 DataSchema) before the call is authorized or sent. A mismatch is refused
naming the first violation, recorded like any refusal, and never reaches the
transport or the approver."""

from __future__ import annotations

from thingctx import ThingClient
from thingctx.operations import Operation
from thingctx.thing import input_errors, value_errors

SPEED = {
    "type": "object",
    "properties": {
        "rpm": {"type": "integer", "minimum": 0, "maximum": 3000},
        "mode": {"type": "string", "enum": ["eco", "boost"]},
        "label": {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^[a-z]+$"},
        "group": {"const": "pumps"},
        "ramp": {
            "type": "object",
            "properties": {"seconds": {"type": "number", "minimum": 0.5}},
            "required": ["seconds"],
        },
        "steps": {"type": "array", "items": {"type": "integer", "maximum": 10}},
    },
    "required": ["rpm"],
}


class CaptureBinding:
    scheme = "cap"

    def __init__(self):
        self.calls = []

    def with_things(self, things):
        return self

    async def invoke(self, action, form, arguments):
        self.calls.append(arguments)
        return {"ok": True}

    async def write(self, prop, form, value):
        self.calls.append((prop.name, value))
        return {"ok": True}

    async def write_all(self, thing, form, values):
        self.calls.append(values)
        return dict.fromkeys(values, "ok")


def _td(input_schema):
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:pump",
        "title": "Pump",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "properties": {
            "rpm": {"type": "integer", "maximum": 3000, "forms": [{"href": "cap://rpm"}]},
            "mode": {"type": "string", "enum": ["eco", "boost"], "forms": [{"href": "cap://mode"}]},
            "hours": {"type": "number", "readOnly": True, "forms": [{"href": "cap://hours"}]},
        },
        "actions": {
            "set_speed": {"input": input_schema, "forms": [{"href": "cap://speed/{zone}"}]},
            "stop": {"forms": [{"href": "cap://stop"}]},
        },
        "forms": [{"href": "cap://all", "op": ["writemultipleproperties"]}],
    }


def test_arguments_that_match_have_no_errors():
    given = {
        "rpm": 1200,
        "mode": "eco",
        "label": "ab",
        "group": "pumps",
        "ramp": {"seconds": 1},
        "steps": [1, 2.0],
    }
    assert input_errors(SPEED, given) == []


def test_each_term_reports_its_own_mismatch():
    cases = [
        ({"rpm": "fast"}, "rpm: expected integer, got string"),
        ({"rpm": 1.5}, "rpm: expected integer, got number"),
        ({"rpm": True}, "rpm: expected integer, got boolean"),
        ({"rpm": -1}, "rpm: -1 is below the minimum 0"),
        ({"rpm": 3001}, "rpm: 3001 is above the maximum 3000"),
        ({"rpm": 1, "mode": "max"}, 'mode: must be one of ["eco", "boost"]'),
        ({"rpm": 1, "label": "a"}, "label: shorter than 2 characters"),
        ({"rpm": 1, "label": "abcde"}, "label: longer than 4 characters"),
        ({"rpm": 1, "label": "AB"}, "label: does not match the pattern ^[a-z]+$"),
        ({"rpm": 1, "group": "fans"}, 'group: must be "pumps"'),
        ({}, "rpm: required, and missing"),
        ({"rpm": 1, "ramp": {}}, "ramp.seconds: required, and missing"),
        ({"rpm": 1, "ramp": {"seconds": 0.1}}, "ramp.seconds: 0.1 is below the minimum 0.5"),
        ({"rpm": 1, "steps": [1, 11]}, "steps[1]: 11 is above the maximum 10"),
    ]
    for given, error in cases:
        assert input_errors(SPEED, given) == [error], given


def test_every_mismatch_is_reported():
    assert input_errors(SPEED, {"rpm": -1, "mode": "max"}) == [
        "rpm: -1 is below the minimum 0",
        'mode: must be one of ["eco", "boost"]',
    ]


def test_json_equality_keeps_true_apart_from_one():
    schema = {"type": "object", "properties": {"on": {"enum": [1, "x"]}}}
    assert input_errors(schema, {"on": 1.0}) == []
    assert input_errors(schema, {"on": True}) == ['on: must be one of [1, "x"]']


def test_a_scalar_input_is_checked_under_its_wrapping_key():
    schema = {"type": "number", "maximum": 5}
    assert input_errors(schema, {"value": 4}) == []
    assert input_errors(schema, {"value": 6}) == ["value: 6 is above the maximum 5"]
    assert input_errors(schema, {}) == ["value: required, and missing"]


def test_a_number_that_is_not_finite_is_not_a_json_number():
    assert input_errors({"type": "number"}, {"value": float("nan")}) != []
    assert input_errors({"type": "number"}, {"value": float("inf")}) != []


def test_unchecked_terms_and_unnamed_arguments_pass():
    schema = {
        "type": "object",
        "properties": {"s": {"type": "string", "pattern": "(", "format": "email"}},
    }
    assert input_errors(schema, {"s": "not an email", "zone": "north"}) == []
    assert input_errors({}, {"anything": 1}) == []


async def test_a_mismatch_is_refused_and_recorded_without_calling_the_binding():
    cap = CaptureBinding()
    seen: list[Operation] = []
    client = ThingClient(tds=[_td(SPEED)], bindings=[cap], record=seen.append)
    out = await client.invoke("pump__set_speed", {"rpm": 9000, "zone": "north"})
    assert out == {
        "error": "invalid input",
        "tool": "pump__set_speed",
        "reason": "rpm: 9000 is above the maximum 3000",
    }
    assert cap.calls == []
    (op,) = seen
    assert (op.op, op.permitted) == ("invokeaction", False)
    assert op.reason == "invalid input: rpm: 9000 is above the maximum 3000"
    assert op.result == out
    assert op.arguments == {"rpm": 9000, "zone": "north", "group": "pumps"}
    assert op.href == "cap://speed/{zone}"


async def test_a_match_reaches_the_binding_with_its_uri_variable_filled():
    cap = CaptureBinding()
    client = ThingClient(tds=[_td(SPEED)], bindings=[cap])
    assert await client.invoke("pump__set_speed", {"rpm": 1200, "zone": "north"}) == {"ok": True}
    assert cap.calls == [{"rpm": 1200, "group": "pumps"}]
    assert await client.invoke("pump__stop") == {"ok": True}


async def test_the_approver_is_not_asked_about_a_mismatch():
    cap = CaptureBinding()
    asked = []
    client = ThingClient(tds=[_td(SPEED)], bindings=[cap], approve_when="all").approving(
        lambda req: asked.append(req) or True
    )
    out = await client.invoke("pump__set_speed", {"rpm": "fast"})
    assert out["error"] == "invalid input"
    assert (asked, cap.calls) == ([], [])


async def test_a_written_value_is_checked_against_the_property_schema():
    cap = CaptureBinding()
    seen: list[Operation] = []
    client = ThingClient(tds=[_td(SPEED)], bindings=[cap], record=seen.append)
    out = await client.write_property("pump__rpm", "fast")
    reason = "pump__rpm: expected integer, got string"
    assert out == {"error": "invalid input", "tool": "pump__rpm", "reason": reason}
    assert cap.calls == []
    (op,) = seen
    assert (op.op, op.permitted, op.reason) == ("writeproperty", False, f"invalid input: {reason}")
    assert await client.write_property("pump__rpm", 1200) == {"ok": True}
    assert cap.calls == [("rpm", 1200)]


def test_a_value_names_the_property():
    assert value_errors({"type": "number", "maximum": 5}, 6, "level") == [
        "level: 6 is above the maximum 5"
    ]
    assert value_errors({"type": "object", "required": ["a"]}, {}, "box") == [
        "box.a: required, and missing"
    ]


async def test_a_read_only_property_is_refused_as_before():
    client = ThingClient(tds=[_td(SPEED)], bindings=[CaptureBinding()])
    assert await client.write_property("pump__hours", "x") == {
        "error": "property pump__hours is read-only"
    }


async def test_a_bulk_write_with_an_invalid_value_writes_each_on_its_own():
    cap = CaptureBinding()
    client = ThingClient(tds=[_td(SPEED)], bindings=[cap])
    assert await client.write_properties({"rpm": 1200, "mode": "eco"}) == {
        "rpm": "ok",
        "mode": "ok",
    }
    assert cap.calls == [{"rpm": 1200, "mode": "eco"}]
    out = await client.write_properties({"rpm": 1200, "mode": "max"})
    assert out["rpm"] == {"ok": True}
    assert out["mode"]["error"] == "invalid input"
    assert cap.calls[1:] == [("rpm", 1200)]


async def test_a_call_refused_for_its_input_names_only_the_first_mismatch():
    client = ThingClient(tds=[_td(SPEED)], bindings=[CaptureBinding()])
    out = await client.invoke("pump__set_speed", {"rpm": -1, "mode": "max"})
    assert out["reason"] == "rpm: -1 is below the minimum 0"


async def test_a_bare_scalar_from_a_gateway_is_checked_and_sent_bare():
    td = _td({"type": "number", "maximum": 5})
    td["actions"]["set_speed"]["forms"] = [{"href": "cap://speed"}]
    cap = CaptureBinding()
    client = ThingClient(tds=[td], bindings=[cap])
    assert await client.invoke("pump__set_speed", 4) == {"ok": True}
    assert cap.calls == [4]
    refused = await client.invoke("pump__set_speed", 6)
    assert refused["error"] == "invalid input"
    assert cap.calls == [4]
