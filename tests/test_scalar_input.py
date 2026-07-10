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

    async def invoke(self, action, form, arguments):  # noqa: ANN001
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
    await client.invoke("calc.negate", {SCALAR_INPUT_KEY: 7})
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
    await client.invoke("obj.set", {"v": 3})
    assert cap.body == {"v": 3}  # object body passes through as-is
