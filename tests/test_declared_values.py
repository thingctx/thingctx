# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""An action's input schema may declare ``const`` and ``default`` values (TD 1.1
DataSchema). A constant is not a parameter: the projection leaves it out and the
runtime sets it. A default stays visible and is filled when the invocation omits
it. Both reach through nested objects and array items."""

from __future__ import annotations

from thingctx import ThingClient
from thingctx.thing import _project_input, fill_declared


class CaptureBinding:
    scheme = "cap"

    def __init__(self):
        self.body = "unset"

    def with_things(self, things):
        return self

    async def invoke(self, action, form, arguments):
        self.body = arguments
        return {"ok": True}


IK_INPUT = {
    "type": "object",
    "properties": {
        "group": {"const": "arm"},
        "avoid_collisions": {"const": True},
        "timeout": {"type": "number", "default": 1},
        "pose": {
            "type": "object",
            "properties": {
                "frame_id": {"type": "string", "default": "base_link"},
                "position": {
                    "type": "object",
                    "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                },
                "orientation": {"const": {"x": 1, "y": 0, "z": 0, "w": 0}},
            },
            "required": ["position", "orientation"],
        },
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"t": {"type": "number", "default": 2}, "p": {"type": "number"}},
            },
        },
    },
    "required": ["group", "pose"],
}


def _td(input_schema):
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:solver",
        "title": "Solver",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {"solve": {"input": input_schema, "forms": [{"href": "cap://solve"}]}},
    }


def test_const_properties_leave_the_parameters_and_defaults_stay():
    params = _project_input(IK_INPUT)
    assert "group" not in params["properties"]
    assert "avoid_collisions" not in params["properties"]
    assert params["required"] == ["pose"]
    assert params["properties"]["timeout"] == {"type": "number", "default": 1}
    pose = params["properties"]["pose"]
    assert "orientation" not in pose["properties"]
    assert pose["required"] == ["position"]
    assert params["properties"]["points"]["items"]["properties"]["t"]["default"] == 2
    # the TD's own schema is untouched
    assert "orientation" in IK_INPUT["properties"]["pose"]["properties"]


def test_a_constant_input_projects_to_no_parameters():
    assert _project_input({"const": 5}) == {"type": "object", "properties": {}, "required": []}
    assert _project_input({"type": "object", "const": {"a": 1}}) == {
        "type": "object",
        "properties": {},
    }


def test_fill_sets_constants_and_absent_defaults_through_nesting():
    given = {
        "pose": {"position": {"x": 0.4, "y": 0.1}, "orientation": "ignored"},
        "points": [{"p": 1}, {"p": 2, "t": 5}],
    }
    out = fill_declared(IK_INPUT, given)
    assert out["group"] == "arm"
    assert out["avoid_collisions"] is True
    assert out["timeout"] == 1
    assert out["pose"]["frame_id"] == "base_link"
    assert out["pose"]["orientation"] == {"x": 1, "y": 0, "z": 0, "w": 0}
    assert out["pose"]["position"] == {"x": 0.4, "y": 0.1}
    assert out["points"] == [{"p": 1, "t": 2}, {"p": 2, "t": 5}]
    # the caller's arguments are not mutated
    assert given["points"][0] == {"p": 1}
    assert "group" not in given


def test_an_omitted_object_is_created_only_when_it_has_a_default():
    assert fill_declared(IK_INPUT, {}) == {"group": "arm", "avoid_collisions": True, "timeout": 1}
    with_default = {
        "type": "object",
        "properties": {
            "opts": {"type": "object", "default": {}, "properties": {"v": {"const": 2}}}
        },
    }
    assert fill_declared(with_default, {}) == {"opts": {"v": 2}}


def test_a_filled_value_does_not_alias_the_schema():
    out = fill_declared(IK_INPUT, {"pose": {"position": {}}})
    out["pose"]["orientation"]["x"] = 9
    assert IK_INPUT["properties"]["pose"]["properties"]["orientation"]["const"]["x"] == 1


async def test_the_transport_receives_the_filled_payload():
    cap = CaptureBinding()
    client = ThingClient(tds=[_td(IK_INPUT)], bindings=[cap])
    await client.invoke("solver__solve", {"pose": {"position": {"x": 0.4, "y": 0.1}}})
    assert cap.body["group"] == "arm"
    assert cap.body["pose"]["orientation"] == {"x": 1, "y": 0, "z": 0, "w": 0}
    assert cap.body["pose"]["frame_id"] == "base_link"


async def test_a_constant_scalar_input_reaches_the_transport_bare():
    cap = CaptureBinding()
    client = ThingClient(tds=[_td({"type": "integer", "const": 5})], bindings=[cap])
    assert client.tool_specs[0]["function"]["parameters"]["properties"] == {}
    await client.invoke("solver__solve", {})
    assert cap.body == 5


async def test_the_approver_sees_the_filled_arguments():
    cap = CaptureBinding()
    seen = {}

    def approve(req):
        seen.update(req.arguments)
        return True

    client = ThingClient(tds=[_td(IK_INPUT)], bindings=[cap], approve_when="all").approving(approve)
    await client.invoke("solver__solve", {"pose": {"position": {"x": 0.4, "y": 0.1}}})
    assert seen["group"] == "arm"
    assert seen["pose"]["orientation"] == {"x": 1, "y": 0, "z": 0, "w": 0}
    assert cap.body == seen
