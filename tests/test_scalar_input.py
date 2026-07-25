# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A WoT action may declare a scalar or array ``input`` (legal per TD 1.1). The
OpenAI function format requires object ``parameters``, so such an input is
projected wrapped under a single key and unwrapped before invoke. These tests
prove the projection is callable and the round trip delivers the bare value."""

from __future__ import annotations

from thingctx import ThingClient
from thingctx.thing import SCALAR_INPUT_KEY, _project_input, is_wrapped_input


class CaptureBinding:
    """Records the body the runtime hands the transport, so a test can assert
    the value the TD declared reaches the wire, not the model-facing envelope."""

    scheme = "cap"

    def __init__(self):
        self.body = "unset"

    def with_things(self, things):  # accepted by the client wiring
        return self

    async def invoke(self, action, form, arguments):
        self.body = arguments
        return {"ok": True}


def _scalar_td():
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:calc",
        "title": "Calc",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {
            # legal WoT: a scalar input schema
            "negate": {
                "description": "Negate a number.",
                "input": {"type": "number"},
                "forms": [{"href": "cap://negate"}],
            }
        },
    }


def test_scalar_input_projects_to_a_callable_object_schema():
    # unit: the projection wraps a non-object schema so the tool is callable
    params = _project_input({"type": "number"})
    assert params["type"] == "object"
    assert params["properties"][SCALAR_INPUT_KEY] == {"type": "number"}
    assert params["required"] == [SCALAR_INPUT_KEY]
    assert is_wrapped_input({"type": "number"}) is True
    # an object schema passes through untouched
    obj = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert _project_input(obj) == obj
    assert is_wrapped_input(obj) is False


def test_scalar_tool_spec_is_object_typed():
    cap = CaptureBinding()
    client = ThingClient(tds=[_scalar_td()], bindings=[cap])
    specs, _ = client.as_tools()
    params = specs[0]["function"]["parameters"]
    # a provider rejects a non-object parameters; ours must be object
    assert params["type"] == "object"
    assert SCALAR_INPUT_KEY in params["properties"]


async def test_round_trip_delivers_the_bare_scalar():
    cap = CaptureBinding()
    client = ThingClient(tds=[_scalar_td()], bindings=[cap])
    # the model calls with the wrapped key, as the projected schema dictates
    await client.invoke("calc__negate", {SCALAR_INPUT_KEY: 7})
    # the transport must receive the bare value, not the envelope
    assert cap.body == 7


async def test_object_input_is_unaffected():
    cap = CaptureBinding()
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:obj",
        "title": "Obj",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {
            "set": {
                "description": "Set a value.",
                "input": {"type": "object", "properties": {"v": {"type": "integer"}}},
                "forms": [{"href": "cap://set"}],
            }
        },
    }
    client = ThingClient(tds=[td], bindings=[cap])
    await client.invoke("obj__set", {"v": 3})
    assert cap.body == {"v": 3}  # object body passes through as-is


def test_flat_tool_advertises_its_uri_variables():
    """Regression: a flat tool's parameters must include the uriVariables its form
    needs (action-level AND the Thing-level ones its href references), else a model
    reading only the schema can't supply them. The bare input schema omits them, so
    e.g. mqtt__publish over mqtt://{+broker}/{+topic} must expose broker + topic, not
    only the payload's value/retain."""
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thingctx:mqtt",
        "title": "mqtt",
        "securityDefinitions": {"n": {"scheme": "nosec"}},
        "security": ["n"],
        "uriVariables": {"broker": {"type": "string"}},  # Thing-level
        "actions": {
            "publish": {
                "uriVariables": {"topic": {"type": "string"}},  # action-level
                "input": {
                    "type": "object",
                    "properties": {"value": {}, "retain": {"type": "boolean"}},
                },
                "forms": [{"href": "mqtt://{+broker}/{+topic}", "op": "invokeaction"}],
            }
        },
    }
    client = ThingClient(tds=[td], bindings=[])
    spec = next(s for s in client.list_actions() if s["function"]["name"] == "mqtt__publish")
    params = spec["function"]["parameters"]
    props = params["properties"]
    # the payload fields AND both uriVariables are advertised
    assert set(props) >= {"value", "retain", "broker", "topic"}
    # a uriVariable the href needs is required so the model reliably supplies it
    assert "broker" in params["required"]
    assert "topic" in params["required"]


def test_flat_tool_without_uri_variables_is_unchanged():
    """An action whose form references no uriVariable keeps its plain input schema
    (no spurious required fields injected)."""
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:plain",
        "title": "plain",
        "securityDefinitions": {"n": {"scheme": "nosec"}},
        "security": ["n"],
        "actions": {
            "go": {
                "input": {"type": "object", "properties": {"x": {"type": "integer"}}},
                "forms": [{"href": "https://h/go", "op": "invokeaction"}],
            }
        },
    }
    client = ThingClient(tds=[td], bindings=[])
    spec = next(s for s in client.list_actions() if s["function"]["name"] == "plain__go")
    props = spec["function"]["parameters"]["properties"]
    assert set(props) == {"x"}  # nothing injected
