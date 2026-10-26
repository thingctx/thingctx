# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A grant can bound the arguments it permits with a JSON Schema: the PDP checks
the action input or the written value, the client refuses a call outside the
bounds before it reaches the device, and the broker rules ignore the bounds."""

from __future__ import annotations

import sys

import pytest

from thingctx import ThingClient
from thingctx.authz import (
    AccessRequest,
    AuthorizationDenied,
    LocalPolicyGrantSource,
    PolicyDecisionPoint,
    build_vocabulary,
    limited,
    to_authzen_request,
)
from thingctx.bindings import LocalBinding
from thingctx.gateways.mosquitto import acl_file
from thingctx.operations import Operation
from thingctx.thing import parse_thing

pytest.importorskip("jsonschema")

ARM = "urn:dev:arm:1"
_AXIS = {"type": "number"}
TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": ARM,
    "title": "Arm",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "properties": {
        "setpoint": {"type": "number", "forms": [{"href": "local://setpoint"}]},
    },
    "actions": {
        "move_to": {
            "input": {
                "type": "object",
                "properties": {
                    "target": {"type": "object", "properties": {"x": _AXIS, "y": _AXIS}},
                    "speed": {"type": "number", "default": 0.1},
                },
            },
            "forms": [{"href": "local://move_to"}],
        },
        "set_speed": {"input": {"type": "number"}, "forms": [{"href": "local://set_speed"}]},
    },
}
_IN = {"type": "number", "minimum": -0.65, "maximum": 0.65}
BOX = {
    "properties": {
        "target": {"properties": {"x": _IN, "y": _IN}},
        "speed": {"type": "number", "maximum": 0.5},
    },
    "required": ["speed"],
}
MOVE = (ARM, "move_to", "invokeaction")


def _pdp(*grants: tuple[str, ...]) -> PolicyDecisionPoint:
    return PolicyDecisionPoint(
        build_vocabulary(parse_thing(TD)),
        LocalPolicyGrantSource({"operator": set(grants)}),
    )


async def _decide(pdp: PolicyDecisionPoint, op: tuple[str, str, str], arguments=None):
    thing, affordance, name = op
    request = AccessRequest(thing, affordance, name, arguments=arguments)
    return await pdp.decide({"roles": ["operator"]}, request)


def _move(x: float) -> dict:
    return {"target": {"x": x, "y": 0.0}, "speed": 0.2}


async def test_arguments_inside_the_limits_are_permitted() -> None:
    decision = await _decide(_pdp(limited(*MOVE, BOX)), MOVE, _move(0.5))
    assert decision.permit


async def test_arguments_outside_the_limits_are_denied_naming_the_violation() -> None:
    decision = await _decide(_pdp(limited(*MOVE, BOX)), MOVE, _move(0.9))
    assert not decision.permit
    assert decision.reason == (
        "arguments outside the grant's limits for "
        "('urn:dev:arm:1', 'move_to', 'invokeaction'): "
        "target.x: 0.9 is greater than the maximum of 0.65"
    )


async def test_an_unlimited_grant_still_permits_any_arguments() -> None:
    assert (await _decide(_pdp(MOVE), MOVE, _move(9.0))).permit
    assert (await _decide(_pdp(MOVE, limited(*MOVE, BOX)), MOVE, _move(9.0))).permit


async def test_arguments_valid_against_any_covering_limit_are_permitted() -> None:
    wide = {"properties": {"target": {"properties": {"x": {"type": "number", "maximum": 2}}}}}
    pdp = _pdp(limited(*MOVE, BOX), limited(*MOVE, wide))
    assert (await _decide(pdp, MOVE, _move(0.9))).permit
    assert not (await _decide(pdp, MOVE, _move(3.0))).permit


async def test_a_grant_that_covers_nothing_keeps_its_reason() -> None:
    decision = await _decide(_pdp(limited(ARM, "set_speed", "invokeaction", {})), MOVE, {})
    assert decision.reason == f"grant does not include {MOVE}"


async def test_a_wildcard_grant_with_limits_bounds_every_affordance_it_covers() -> None:
    pdp = _pdp(limited(ARM, "*", "*", BOX))
    assert (await _decide(pdp, MOVE, _move(0.5))).permit
    assert not (await _decide(pdp, MOVE, _move(0.9))).permit
    read = await _decide(pdp, (ARM, "setpoint", "readproperty"))
    assert not read.permit
    assert "limits arguments; none given" in read.reason


async def test_a_written_value_is_bounded() -> None:
    write = (ARM, "setpoint", "writeproperty")
    pdp = _pdp(limited(*write, {"type": "number", "minimum": 10, "maximum": 40}))
    assert (await _decide(pdp, write, 25)).permit
    denied = await _decide(pdp, write, 41)
    assert not denied.permit
    assert denied.reason.endswith(f"{write}: 41 is greater than the maximum of 40")


@pytest.mark.parametrize("x", ["99", True, {}, [9999]])
async def test_a_value_that_is_not_a_number_is_refused_by_a_typed_bound(x) -> None:
    decision = await _decide(_pdp(limited(*MOVE, BOX)), MOVE, _move(x))
    assert not decision.permit
    assert "outside the grant's limits" in decision.reason


@pytest.mark.parametrize("y", [float("nan"), float("inf"), float("-inf")])
async def test_a_number_that_is_not_finite_is_refused(y) -> None:
    arguments = {"target": {"x": 0.1, "y": y}, "speed": 0.2}
    decision = await _decide(_pdp(limited(*MOVE, BOX)), MOVE, arguments)
    assert not decision.permit
    assert decision.reason.endswith("one is a number that is not finite")


async def test_a_limit_that_cannot_be_applied_denies() -> None:
    bad = {"properties": {"label": {"type": "string", "pattern": "("}}}
    decision = await _decide(_pdp(limited(*MOVE, bad)), MOVE, {"label": "x"})
    assert not decision.permit
    assert "with a schema that fails" in decision.reason


async def test_a_raw_grant_with_a_reference_denies() -> None:
    decision = await _decide(_pdp((*MOVE, '{"$ref":"#/$defs/missing"}')), MOVE, _move(0.1))
    assert not decision.permit
    assert "uses a reference" in decision.reason


@pytest.mark.parametrize(
    "schema",
    [
        {"maximum": 4},
        {"properties": {"x": {"minimum": 0}}},
        {"allOf": [{"type": "object"}, {"multipleOf": 2}]},
        {"items": {"exclusiveMaximum": 3}},
        {"$defs": {"axis": {"exclusiveMinimum": 0}}},
    ],
)
def test_a_numeric_bound_without_a_type_is_refused(schema) -> None:
    with pytest.raises(ValueError, match="without a 'type'"):
        limited(*MOVE, schema)


@pytest.mark.parametrize(
    "schema",
    [{"type": ["number", "string"], "maximum": 4}, {"type": "string", "maximum": 4}],
)
def test_a_numeric_bound_beside_a_type_that_is_not_numeric_is_refused(schema) -> None:
    with pytest.raises(ValueError, match="of number or integer"):
        limited(*MOVE, schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.com/limit.json"},
        {"$defs": {"x": {"maximum": 4}}, "$ref": "#/$defs/x"},
        {"$dynamicRef": "#x"},
    ],
)
def test_a_limit_with_a_reference_is_refused(schema) -> None:
    with pytest.raises(ValueError, match="uses a reference"):
        limited(*MOVE, schema)


async def test_a_grant_built_without_limited_is_still_checked() -> None:
    decision = await _decide(_pdp((*MOVE, '{"maximum":4}')), MOVE, _move(0.1))
    assert not decision.permit
    assert "without a 'type'" in decision.reason


async def test_arguments_nested_too_deep_are_denied_not_raised() -> None:
    deep: dict = {}
    node = deep
    for _ in range(5000):
        node["a"] = {}
        node = node["a"]
    decision = await _decide(_pdp(limited(*MOVE, BOX)), MOVE, deep)
    assert not decision.permit


def test_a_property_named_like_a_bound_is_not_a_bound() -> None:
    limited(*MOVE, {"properties": {"maximum": {"type": "number", "maximum": 3}}})


async def test_without_jsonschema_a_limited_grant_denies(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    decision = await _decide(_pdp(limited(*MOVE, BOX)), MOVE, _move(0.5))
    assert not decision.permit
    assert "validate extra" in decision.reason
    assert (await _decide(_pdp(MOVE), MOVE, _move(0.5))).permit


def test_arguments_take_no_part_in_equality_or_the_hash() -> None:
    with_arguments = AccessRequest(*MOVE, arguments={"target": {"x": 1}})
    assert with_arguments == AccessRequest(*MOVE)
    assert hash(with_arguments) == hash(AccessRequest(*MOVE))


def test_the_authzen_action_carries_the_arguments() -> None:
    body = to_authzen_request({"sub": "alice"}, AccessRequest(*MOVE, arguments=_move(0.5)))
    assert body["action"] == {"name": "invokeaction", "properties": {"arguments": _move(0.5)}}
    read = to_authzen_request({"sub": "alice"}, AccessRequest(ARM, "setpoint", "readproperty"))
    assert read["action"] == {"name": "readproperty"}


def test_the_broker_rules_ignore_limits() -> None:
    plain = acl_file(gateways=["gw"], callers=["alice"], grants={"alice": [MOVE]})
    bounded = acl_file(gateways=["gw"], callers=["alice"], grants={"alice": [limited(*MOVE, BOX)]})
    assert bounded == plain


def _client(moves: list, seen: list) -> ThingClient:
    base = ThingClient(
        tds=[TD], bindings=[LocalBinding({"move_to": lambda **kw: moves.append(kw)})]
    )
    pdp = _pdp(
        limited(*MOVE, BOX),
        limited(ARM, "set_speed", "invokeaction", {"type": "number", "maximum": 10}),
    )
    return base.guarded(pdp, identity={"roles": ["operator"]}, record=seen.append)


async def test_the_client_runs_a_call_inside_the_limits_and_refuses_one_outside() -> None:
    moves: list = []
    seen: list[Operation] = []
    client = _client(moves, seen)
    await client.invoke("arm__move_to", {"target": {"x": 0.5, "y": 0.1}})
    assert moves == [{"target": {"x": 0.5, "y": 0.1}, "speed": 0.1}]
    with pytest.raises(AuthorizationDenied, match=r"target\.x: 0\.9 is greater"):
        await client.invoke("arm__move_to", {"target": {"x": 0.9, "y": 0.1}})
    assert len(moves) == 1
    refused = seen[-1]
    assert (refused.affordance, refused.op, refused.permitted) == ("move_to", "invokeaction", False)
    assert refused.arguments == {"target": {"x": 0.9, "y": 0.1}, "speed": 0.1}
    assert "outside the grant's limits" in str(refused.reason)


async def test_the_client_checks_a_scalar_input_bare() -> None:
    client = _client([], [])
    with pytest.raises(AuthorizationDenied, match=r"\): 50 is greater than the maximum of 10"):
        await client.invoke("arm__set_speed", {"value": 50})


async def test_the_td_schema_refuses_a_string_before_the_limit_check() -> None:
    moves: list = []
    seen: list[Operation] = []
    out = await _client(moves, seen).invoke("arm__move_to", {"target": {"x": "99", "y": 0.0}})
    reason = "target.x: expected number, got string"
    assert out == {"error": "invalid input", "tool": "arm__move_to", "reason": reason}
    assert moves == []
    assert (seen[-1].permitted, seen[-1].reason) == (False, f"invalid input: {reason}")
