# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""from_openapi widening: contentType from the body media key, header emission,
per-operation overrides, kept constraint keys, and gated output schemas."""

from __future__ import annotations

from thingctx import from_openapi

FORM_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Pay API"},
    "servers": [{"url": "https://api.pay.test"}],
    "paths": {
        "/charges": {
            "post": {
                "operationId": "createCharge",
                "summary": "Create a charge",
                "parameters": [
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "Authorization",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                ],
                "requestBody": {
                    "content": {
                        "application/x-www-form-urlencoded": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "amount": {"type": "integer", "minimum": 1, "maximum": 999999},
                                    "currency": {"type": "string", "pattern": "^[a-z]{3}$"},
                                    "internal_note": {"type": "string"},
                                },
                                "required": ["amount", "currency"],
                            }
                        }
                    }
                },
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "paid": {"type": "boolean"},
                                    },
                                }
                            }
                        }
                    }
                },
            }
        }
    },
}


def _form(td, action):
    return td["actions"][action]["forms"][0]


def test_content_type_follows_the_body_media_key():
    td = from_openapi(FORM_SPEC)
    # the body is form-encoded; the form must say so, not default to JSON
    assert _form(td, "createCharge")["contentType"] == "application/x-www-form-urlencoded"


def test_json_body_still_yields_json_content_type():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "J"},
        "servers": [{"url": "https://j.test"}],
        "paths": {
            "/x": {
                "post": {
                    "operationId": "doX",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"a": {"type": "string"}},
                                }
                            }
                        }
                    },
                }
            }
        },
    }
    td = from_openapi(spec)
    assert _form(td, "doX")["contentType"] == "application/json"


def test_required_header_emitted_but_credential_header_refused():
    td = from_openapi(FORM_SPEC)
    headers = _form(td, "createCharge").get("htv:headers", [])
    names = {h["htv:fieldName"] for h in headers}
    assert "Idempotency-Key" in names  # required, non-credential -> declared
    assert "Authorization" not in names  # credential-shaped -> refused


def test_fixed_headers_applied_and_credential_names_dropped():
    td = from_openapi(FORM_SPEC, headers={"X-Api-Version": "2026-07-05", "X-Api-Token": "nope"})
    headers = {
        h["htv:fieldName"]: h.get("htv:fieldValue")
        for h in _form(td, "createCharge")["htv:headers"]
    }
    assert headers.get("X-Api-Version") == "2026-07-05"
    assert "X-Api-Token" not in headers  # credential-shaped fixed header refused


def test_constraint_keys_are_kept():
    td = from_openapi(FORM_SPEC)
    props = td["actions"]["createCharge"]["input"]["properties"]
    assert props["amount"]["minimum"] == 1
    assert props["amount"]["maximum"] == 999999
    assert props["currency"]["pattern"] == "^[a-z]{3}$"


def test_overrides_rename_redescribe_and_limit_body_fields():
    td = from_openapi(
        FORM_SPEC,
        overrides={
            "POST /charges": {
                "name": "charge",
                "description": "Charge a card.",
                "body_fields": ["amount", "currency"],  # drop internal_note
            }
        },
    )
    assert "charge" in td["actions"]
    assert td["actions"]["charge"]["description"] == "Charge a card."
    props = td["actions"]["charge"]["input"]["properties"]
    assert set(props) >= {"amount", "currency"}
    assert "internal_note" not in props


def test_outputs_are_gated_off_by_default_and_on_by_flag():
    off = from_openapi(FORM_SPEC)
    assert "output" not in off["actions"]["createCharge"]
    on = from_openapi(FORM_SPEC, outputs=True)
    out = on["actions"]["createCharge"]["output"]
    assert out["properties"]["id"]["type"] == "string"
