"""from_openapi: compile an OpenAPI 3.x spec into a drivable WoT TD."""

from __future__ import annotations

import json

import pytest

from thingctx import from_openapi, parse_thing
from thingctx.cli import main as cli_main

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Widget API", "description": "widgets"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "components": {
        "securitySchemes": {
            "oauth": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "https://auth.example.com/token",
                        "scopes": {"widgets:read": "read", "widgets:write": "write"},
                    }
                },
            },
            "key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
        }
    },
    "security": [{"oauth": ["widgets:read"]}],
    "paths": {
        "/widgets": {
            "get": {
                "operationId": "listWidgets",
                "summary": "List widgets",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
            },
            "post": {
                "operationId": "createWidget",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "color": {"type": "string", "enum": ["red", "blue"]},
                                },
                                "required": ["name"],
                            }
                        }
                    }
                },
            },
        },
        "/widgets/{id}": {
            "delete": {
                "operationId": "deleteWidget",
                "parameters": [{"name": "id", "in": "path", "schema": {"type": "string"}}],
            }
        },
    },
}


def test_actions_cover_every_operation():
    td = from_openapi(SPEC)
    assert set(td["actions"]) == {"listWidgets", "createWidget", "deleteWidget"}


def test_forms_carry_method_and_absolute_url():
    td = from_openapi(SPEC)
    create = td["actions"]["createWidget"]["forms"][0]
    assert create["href"] == "https://api.example.com/v1/widgets"
    assert create["htv:methodName"] == "POST"


def test_safety_hints_follow_http_method():
    td = from_openapi(SPEC)
    assert td["actions"]["listWidgets"]["safe"] is True
    assert td["actions"]["createWidget"]["safe"] is False
    assert td["actions"]["deleteWidget"]["idempotent"] is True


def test_input_schema_from_query_path_and_body():
    td = from_openapi(SPEC)
    create_in = td["actions"]["createWidget"]["input"]
    assert create_in["properties"]["color"]["enum"] == ["red", "blue"]
    assert create_in["required"] == ["name"]
    assert td["actions"]["deleteWidget"]["input"]["required"] == ["id"]
    assert "limit" in td["actions"]["listWidgets"]["input"]["properties"]


def test_security_maps_and_parses():
    td = from_openapi(SPEC)
    assert td["security"] == ["oauth"]
    oauth = td["securityDefinitions"]["oauth"]
    assert oauth["scheme"] == "oauth2"
    assert oauth["token"] == "https://auth.example.com/token"
    # Round-trips through the parser into a usable scheme.
    thing = parse_thing(td)
    assert thing.security_schemes["oauth"].flow == "client_credentials"


def test_combined_security_keeps_every_scheme():
    # A requirement object listing two schemes means both apply (AND); neither
    # should be dropped.
    spec = dict(SPEC, security=[{"oauth": ["widgets:read"], "key": []}])
    td = from_openapi(spec)
    assert set(td["security"]) == {"oauth", "key"}


def test_per_operation_security_override_on_form():
    spec = json.loads(json.dumps(SPEC))
    spec["paths"]["/widgets"]["get"]["security"] = [{"key": []}]
    spec["paths"]["/widgets"]["post"]["security"] = []  # explicitly public
    td = from_openapi(spec)
    assert td["actions"]["listWidgets"]["forms"][0]["security"] == ["key"]
    assert td["actions"]["createWidget"]["forms"][0]["security"] == ["nosec_sc"]
    # An operation that inherits the Thing-level security carries no override.
    assert "security" not in td["actions"]["deleteWidget"]["forms"][0]
    assert "nosec_sc" in td["securityDefinitions"]
    validate_td = pytest.importorskip("thingctx.validate").validate_td
    assert validate_td(td) == []


def test_base_url_override():
    td = from_openapi(SPEC, base_url="https://staging.example.com")
    assert td["actions"]["listWidgets"]["forms"][0]["href"] == "https://staging.example.com/widgets"


def test_include_filter_keeps_subset():
    td = from_openapi(SPEC, include=["listWidgets"])
    assert set(td["actions"]) == {"listWidgets"}


def test_nosec_when_spec_has_no_security():
    td = from_openapi({"info": {"title": "X"}, "paths": {}})
    assert td["securityDefinitions"] == {"nosec_sc": {"scheme": "nosec"}}


def test_cli_writes_td(tmp_path):
    spec_file = tmp_path / "api.json"
    spec_file.write_text(json.dumps(SPEC))
    out = tmp_path / "out.td.json"
    rc = cli_main(["import", "openapi", str(spec_file), "--out", str(out)])
    assert rc == 0
    td = json.loads(out.read_text())
    assert set(td["actions"]) == {"listWidgets", "createWidget", "deleteWidget"}


def test_parse_yaml_requires_yaml_or_falls_back():
    pytest.importorskip("yaml")
    from thingctx.openapi import _parse_spec

    parsed = _parse_spec("openapi: '3.0.0'\ninfo:\n  title: Y\npaths: {}\n")
    assert parsed["info"]["title"] == "Y"


# Producer-side guarantee: what we generate must itself be conformant. thingctx
# is a strict producer (and a lenient consumer), so from_openapi output has to
# pass W3C TD 1.1 validation.


def test_generated_td_is_w3c_valid():
    validate_td = pytest.importorskip("thingctx.validate").validate_td
    # oauth2 + apikey + body/query/path params (the full SPEC).
    assert validate_td(from_openapi(SPEC)) == []
    # a no-security spec falls back to nosec, which must also validate.
    assert validate_td(from_openapi({"info": {"title": "X"}, "paths": {}})) == []


def test_generated_apikey_td_is_w3c_valid():
    validate_td = pytest.importorskip("thingctx.validate").validate_td
    spec = {
        "info": {"title": "Keyed API"},
        "servers": [{"url": "https://api.example.com"}],
        "components": {"securitySchemes": {"k": {"type": "apiKey", "in": "header", "name": "X-K"}}},
        "security": [{"k": []}],
        "paths": {"/ping": {"get": {"operationId": "ping"}}},
    }
    assert validate_td(from_openapi(spec)) == []
