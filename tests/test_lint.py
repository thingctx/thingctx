# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""lint_td flags the ways a schema-valid TD still fails an agent, and never
rejects a clean one. All offline: TDs are built inline."""

from __future__ import annotations

import pytest

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
        "id": "urn:demo:myapp",
        "title": "MyApp",
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
        "id": "urn:demo:myapp",
        "title": "MyApp",
        # a slash-shaped name (GitHub-style) is outside the accepted charset
        "actions": {"repos/get": {"description": "Get a repo.", "forms": [{"href": "https://d"}]}},
    }
    findings = lint_td(td)
    assert any(f.rule == "invalid_tool_name" and f.severity == "error" for f in findings)


def test_empty_parameters_flagged():
    td = {
        "id": "urn:demo:myapp",
        "title": "MyApp",
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
        "id": "urn:demo:myapp",
        "title": "MyApp",
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
        "id": "urn:demo:myapp",
        "title": "MyApp",
        "actions": {
            # no safe, no idempotent, no tc: marking
            "reboot": {"description": "Reboot the device.", "forms": [{"href": "https://d"}]}
        },
    }
    assert "unmarked_risk" in _rules(td)


def test_thin_namespace_flagged_for_short_id():
    td = {
        "id": "x",
        "title": "X",
        "actions": {
            "do_it": {
                "description": "Do the thing.",
                "safe": True,
                "forms": [{"href": "https://d"}],
            }
        },
    }
    rules = _rules(td)
    assert "thin_namespace" in rules


def test_thin_namespace_clean_for_normal_id():
    td = {
        "id": "urn:demo:pump:v1",
        "title": "Pump",
        "actions": {
            "set_speed": {
                "description": "Set the speed.",
                "safe": True,
                "forms": [{"href": "https://d"}],
            }
        },
    }
    assert "thin_namespace" not in _rules(td)


def test_thin_namespace_flagged_for_two_char_id():
    td = {
        "id": "urn:t1",
        "title": "T1",
        "actions": {
            "read": {
                "description": "Read value.",
                "safe": True,
                "forms": [{"href": "https://d"}],
            }
        },
    }
    assert "thin_namespace" in _rules(td)


def test_thin_namespace_does_not_flag_meaningful_short_name():
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "@type": "saref:Pump",
        "id": "urn:demo:db:v1",
        "title": "Database",
        "actions": {
            "query": {
                "description": "Run a query.",
                "safe": True,
                "forms": [{"href": "https://d/query"}],
            }
        },
    }
    assert "thin_namespace" not in _rules(td)


def test_findings_are_advice_never_an_exception():
    # a sparse but well-formed dict lints without raising
    assert isinstance(lint_td({"id": "urn:myapp", "title": "MyApp"}), list)


def _thin_td(thing_id: str | None, title: str) -> dict:
    td: dict = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "@type": "saref:Pump",
        "title": title,
        "actions": {
            "go": {"description": "Do the thing.", "safe": True, "forms": [{"href": "https://d"}]}
        },
    }
    if thing_id is not None:
        td["id"] = thing_id
    return td


@pytest.mark.parametrize(
    "thing_id,title",
    [
        ("urn:demo:s3", "S3 Bucket"),
        ("urn:demo:k8", "Kubernetes"),
        ("urn:demo:k8s", "Kubernetes"),
        ("urn:demo:i18n", "Internationalization"),
        ("urn:demo:py", "Python"),
        ("urn:demo:gh", "GitHub"),
        ("urn:demo:ec2", "EC2 Instance"),
        ("urn:demo:pump", "Water Pump"),
        ("urn:thingctx:cam:sample", "Sample camera"),
    ],
)
def test_namespace_the_title_abbreviates_is_not_thin(thing_id, title):
    assert "thin_namespace" not in _rules(_thin_td(thing_id, title))


@pytest.mark.parametrize(
    "thing_id",
    [
        "urn:demo:x",
        "urn:demo:t1",
        "urn:demo:n1",
        "urn:demo:zz",
        "urn:demo:x999",
        "urn:demo:thing1",
        "urn:demo:test",
        "urn:demo:foo",
        "urn:demo:xz",
    ],
)
def test_namespace_the_title_does_not_explain_is_thin(thing_id):
    assert "thin_namespace" in _rules(_thin_td(thing_id, "Water Pump"))


def test_thin_namespace_falls_back_to_title_when_there_is_no_id():
    assert "thin_namespace" in _rules(_thin_td(None, "X"))
    assert "thin_namespace" not in _rules(_thin_td(None, "Water Pump"))


def test_thin_namespace_is_reported_once_per_thing():
    td = _thin_td("urn:demo:x", "Water Pump")
    td["properties"] = {"p": {"description": "A value.", "forms": [{"href": "https://d"}]}}
    td["events"] = {"e": {"description": "A signal.", "forms": [{"href": "https://d"}]}}
    assert [f.rule for f in lint_td(td)].count("thin_namespace") == 1


def test_thin_namespace_reads_the_projected_namespace_not_the_raw_id():
    # Pins the rule to thing_slug, so a change to the tool-name separator cannot
    # silently stop it firing.
    assert "thin_namespace" in _rules(_thin_td("urn:demo:x", "Water Pump"))
    assert "thin_namespace" not in _rules(_thin_td("urn:demo:pump:v1", "Water Pump"))
