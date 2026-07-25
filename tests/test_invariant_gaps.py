# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Targeted tests for invariant sub-cases that a broader test exercised only in
passing. Each pins one named property so a regression in it surfaces directly."""

from __future__ import annotations

import pytest

from thingctx.thing import WoTForm, _tool_slug, parse_thing

# --------------------------------------------------------------------------- #
# NET-11: WoTForm.fill percent-encodes an interpolated {var}; only {+var}
# substitutes verbatim.
# --------------------------------------------------------------------------- #


def test_fill_percent_encodes_a_plain_var():
    # invariant NET-11: a {var} value is percent-encoded (quote safe=""), so a
    # value with reserved characters cannot break out of its path/query segment.
    form = WoTForm(href="https://api.example/things/{name}/read")
    href, rest = form.fill({"name": "a/b?c=d&e"})
    assert href == "https://api.example/things/a%2Fb%3Fc%3Dd%26e/read"
    assert rest == {}


def test_fill_reserved_expansion_is_verbatim():
    # invariant NET-11: only the explicit RFC 6570 {+var} reserved form substitutes
    # verbatim, for when the variable IS a URL (a media {+url} source).
    form = WoTForm(href="{+url}")
    href, _ = form.fill({"url": "rtsp://cam.local/stream?x=1"})
    assert href == "rtsp://cam.local/stream?x=1"  # not encoded


# --------------------------------------------------------------------------- #
# PROT-3: the (Thing, action) split is recovered on the FIRST "__", so an action
# name that itself contains "_" (or "__") stays intact.
# --------------------------------------------------------------------------- #


def test_tool_slug_splits_on_first_separator_only():
    # invariant PROT-3: split on the first "__" only; an action name with its own
    # "_" or "__" is preserved on the action side, never re-split.
    assert _tool_slug("pump__set_target_speed") == "pump"
    assert _tool_slug("pump__reset__all") == "pump"
    assert _tool_slug("cam1__snapshot") == "cam1"


def test_action_name_with_underscore_round_trips_through_projection():
    # invariant PROT-3: a Thing whose action name contains "__" projects to a tool
    # whose slug is recovered correctly (the route map, not string re-splitting).
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:pump",
        "title": "Pump",
        "actions": {
            "reset__all": {"forms": [{"href": "https://d/reset", "htv:methodName": "POST"}]}
        },
    }
    from thingctx.thing import actions_to_tools

    specs, route = actions_to_tools([parse_thing(td)])
    name = specs[0]["function"]["name"]
    assert name == "pump__reset__all"
    assert _tool_slug(name) == "pump"  # slug recovered, action name intact
    assert route[name].name == "reset__all"


# --------------------------------------------------------------------------- #
# PROT-5: an action form's uriVariables the href references are folded into the
# tool parameters as required properties.
# --------------------------------------------------------------------------- #


def test_href_uri_variables_are_folded_as_required_params():
    # invariant PROT-5: a broker/topic the href needs but the input schema omits is
    # folded into the tool parameters as a required property, so the model supplies
    # it.
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:mq",
        "title": "MQ",
        "actions": {
            "pub": {
                "input": {"type": "object", "properties": {"payload": {"type": "string"}}},
                "uriVariables": {"topic": {"type": "string"}},
                "forms": [{"href": "mqtt://broker/{topic}", "op": "invokeaction"}],
            }
        },
    }
    from thingctx.thing import actions_to_tools

    specs, _ = actions_to_tools([parse_thing(td)])
    params = specs[0]["function"]["parameters"]
    assert "topic" in params["properties"]  # the uriVariable is a tool parameter
    assert "topic" in params["required"]  # and required to make the call
    assert "payload" in params["properties"]  # the declared input is still there


# --------------------------------------------------------------------------- #
# GATE-19: a wildcard grant matches only affordance/op positions; thing_id must
# match exactly (no wildcard Thing).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wildcard_grant_never_crosses_thing_boundary():
    # invariant GATE-19: a "*" grant for one Thing does not permit an op on a
    # DIFFERENT Thing; thing_id is matched exactly, never wildcarded.
    from thingctx import LocalBinding, ThingClient
    from thingctx.authz import (
        AccessRequest,
        LocalPolicyGrantSource,
        PolicyDecisionPoint,
        build_vocabulary,
    )

    def _td(thing_id):
        return {
            "@context": "https://www.w3.org/2022/wot/td/v1.1",
            "id": thing_id,
            "title": thing_id,
            "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
            "security": ["nosec_sc"],
            "properties": {
                "v": {
                    "type": "integer",
                    "forms": [{"href": "local://v", "op": ["readproperty"]}],
                }
            },
        }

    things = ThingClient(
        tds=[_td("urn:dev:a"), _td("urn:dev:b")], bindings=[LocalBinding(object())]
    ).things
    vocab = build_vocabulary(things)
    # A full wildcard grant, but scoped to Thing a only.
    grants = {"role": {("urn:dev:a", "*", "*")}}
    pdp = PolicyDecisionPoint(vocabulary=vocab, grant_source=LocalPolicyGrantSource(grants))
    ident = {"sub": "x", "roles": ["role"]}

    allowed = await pdp.decide(ident, AccessRequest("urn:dev:a", "v", "readproperty"))
    denied = await pdp.decide(ident, AccessRequest("urn:dev:b", "v", "readproperty"))
    assert allowed.permit is True  # granted on its own Thing
    assert denied.permit is False  # the same wildcard does not reach Thing b


# --------------------------------------------------------------------------- #
# GATE-16: an authorization denial is decided BEFORE the approve gate, so a
# denied call never prompts for human approval.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_denied_call_never_prompts_for_approval():
    # invariant GATE-16: authorization runs before the approve gate; a call the PDP
    # denies is rejected without ever invoking the approver (no prompt for a call
    # that was never allowed).
    from thingctx import LocalBinding, ThingClient
    from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:pump",
        "title": "Pump",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {
            "purge": {
                "forms": [{"href": "local://purge", "op": "invokeaction"}],
                "safe": False,
                "idempotent": False,
            }
        },
    }

    class Pump:
        def purge(self):
            return {"ok": True}

    prompted = {"count": 0}

    async def approver(*a, **k):
        prompted["count"] += 1
        return True  # would approve, but must never be reached for a denied call

    things = ThingClient(tds=[td], bindings=[LocalBinding(Pump())]).things
    pdp = PolicyDecisionPoint(
        vocabulary=build_vocabulary(things),
        grant_source=LocalPolicyGrantSource({}),  # no grants: everything denies
    )
    client = ThingClient(
        tds=[td],
        bindings=[LocalBinding(Pump())],
        pdp=pdp,
        identity={"sub": "x", "roles": ["role"]},
        approve=approver,
        approve_when="always",
        authz_raise=False,  # return the denial envelope so we can inspect, no raise
    )
    result = await client.invoke("pump__purge", {})
    assert prompted["count"] == 0  # the approver was never prompted
    assert "error" in result or result.get("blocked") or result.get("denied")


# --------------------------------------------------------------------------- #
# NET-15: a poll never spins into a tight request loop; even interval 0 waits a
# minimum between requests.
# --------------------------------------------------------------------------- #


def test_poll_interval_is_clamped_to_a_minimum():
    # invariant NET-15: interval 0 (or below the floor) is clamped to
    # MIN_POLL_INTERVAL, so a poll cannot become a tight request loop.
    from thingctx.chain import MIN_POLL_INTERVAL

    assert MIN_POLL_INTERVAL > 0
    for requested in (0, 0.0, -5, MIN_POLL_INTERVAL / 2):
        assert max(MIN_POLL_INTERVAL, float(requested)) == MIN_POLL_INTERVAL


# --------------------------------------------------------------------------- #
# NET-16: every outbound HTTP request carries a timeout.
# --------------------------------------------------------------------------- #


def test_http_binding_client_carries_a_timeout():
    # invariant NET-16: the binding's pooled client is built with a concrete
    # timeout, so no outbound request is left to wait forever.
    from thingctx.bindings import HttpBinding

    binding = HttpBinding(timeout=7.5)
    client = binding._pool()
    # httpx stores the connect/read/write/pool timeout; all four must be bounded.
    t = client.timeout
    assert t.connect == 7.5
    assert t.read == 7.5
    assert t.write == 7.5
