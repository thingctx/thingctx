# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""lint_td flags the ways a schema-valid TD still fails an agent, and never
rejects a clean one. All offline: TDs are built inline."""

from __future__ import annotations

from thingctx.lint import lint_td


def _rules(td: dict) -> set[str]:
    return {f.rule for f in lint_td(td)}


def test_a_clean_td_produces_no_findings():
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "@type": "saref:Pump",
        "id": "urn:demo:pump:v1",
        "title": "Pump",
        "properties": {
            "rpm": {
                "@type": "saref:Speed",
                "title": "Speed",
                "description": "Current rotational speed.",
                "type": "number",
                "unit": "rpm",
                "readOnly": True,
                "forms": [{"href": "https://d/rpm"}],
            }
        },
        "actions": {
            "set_speed": {
                "@type": "saref:SetLevelCommand",
                "description": "Set the target rotational speed of the pump.",
                "safe": False,
                "idempotent": True,
                "input": {"type": "object", "properties": {"rpm": {"type": "number"}}},
                "forms": [{"href": "https://d/set", "htv:methodName": "POST"}],
            }
        },
    }
    assert lint_td(td) == []


def test_thin_description_and_generated_name():
    td = {
        "id": "urn:demo:x",
        "title": "X",
        "actions": {
            # no description, no title -> thin; name looks importer-generated
            "get_users_id": {"forms": [{"href": "https://d/u"}]},
        },
    }
    rules = _rules(td)
    assert "thin_description" in rules
    assert "generated_name" in rules


def test_invalid_tool_name_is_an_error():
    td = {
        "id": "urn:demo:x",
        "title": "X",
        # a slash-shaped name (GitHub-style) is outside the accepted charset
        "actions": {"repos/get": {"description": "Get a repo.", "forms": [{"href": "https://d"}]}},
    }
    findings = lint_td(td)
    assert any(f.rule == "invalid_tool_name" and f.severity == "error" for f in findings)


def test_empty_parameters_flagged():
    td = {
        "id": "urn:demo:x",
        "title": "X",
        "actions": {
            "do_it": {
                "description": "Do the thing now.",
                "safe": True,
                "input": {"type": "object"},  # object, no properties
                "forms": [{"href": "https://d"}],
            }
        },
    }
    assert "empty_parameters" in _rules(td)


def test_credential_shaped_header_is_an_error():
    td = {
        "id": "urn:demo:x",
        "title": "X",
        "actions": {
            "call": {
                "description": "Call the endpoint.",
                "safe": True,
                "forms": [
                    {
                        "href": "https://d",
                        "htv:headers": [
                            {"htv:fieldName": "Authorization", "htv:fieldValue": "Bearer x"}
                        ],
                    }
                ],
            }
        },
    }
    findings = lint_td(td)
    assert any(f.rule == "credential_in_td" and f.severity == "error" for f in findings)


def test_url_shaped_id_and_missing_types():
    td = {
        "id": "https://example.com/things/pump",  # url-shaped
        "title": "Pump",  # no @type on the Thing
        "properties": {
            "temp": {
                "description": "Temperature.",
                "type": "number",
                "forms": [{"href": "https://d"}],
            }
        },
    }
    rules = _rules(td)
    assert "url_shaped_id" in rules
    assert "missing_thing_type" in rules
    assert "missing_units" in rules  # numeric, no unit, no @type
    assert "missing_affordance_type" in rules


def test_unmarked_risk_notice():
    td = {
        "id": "urn:demo:x",
        "title": "X",
        "actions": {
            # no safe, no idempotent, no tc: marking
            "reboot": {"description": "Reboot the device.", "forms": [{"href": "https://d"}]}
        },
    }
    assert "unmarked_risk" in _rules(td)


def test_findings_are_advice_never_an_exception():
    # a sparse but well-formed dict lints without raising
    assert isinstance(lint_td({"id": "urn:x", "title": "X"}), list)
