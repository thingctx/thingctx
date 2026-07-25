# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""SEC-6: credential material never reaches the tool specs shown to the model.

The projection reads only the security-scheme declaration (locations, endpoint
URLs), never the secret; secrets flow into the binding at call time. A regression
that folded a resolved credential into a tool description or a gateway describe
would hand the agent a live secret, so these assert the secret string is absent
from everything the model can see."""

from __future__ import annotations

import json

from thingctx.bindings import HttpBinding
from thingctx.runtime import ThingClient

# A distinctive fixed token to grep for. Deliberately not shaped like a real
# vendor key prefix, so the secret scanner does not flag the test fixture itself.
_SECRET = "THINGCTX-TEST-CREDENTIAL-MUST-NEVER-LEAK-0123456789"

_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:dev:paid-svc",
    "title": "Paid Service",
    "securityDefinitions": {
        "bearer_sc": {"scheme": "bearer"},
        "apikey_sc": {"scheme": "apikey", "in": "header", "name": "X-API-Key"},
    },
    "security": ["bearer_sc"],
    "properties": {
        "balance": {
            "type": "integer",
            "readOnly": True,
            "forms": [{"href": "https://api.paid.example/balance"}],
        }
    },
    "actions": {
        "charge": {
            "input": {"type": "object", "properties": {"amount": {"type": "integer"}}},
            "forms": [{"href": "https://api.paid.example/charge", "htv:methodName": "POST"}],
        }
    },
    "events": {
        "settled": {
            "data": {"type": "object", "properties": {"id": {"type": "string"}}},
            "forms": [{"href": "https://api.paid.example/events", "subprotocol": "sse"}],
        }
    },
}


def _client() -> ThingClient:
    # The runtime secret is supplied exactly as a real caller would (on the
    # binding, keyed to the Thing); the point is that it must not appear in
    # anything projected to the model.
    return ThingClient(
        tds=[_TD],
        bindings=[HttpBinding(credentials={"urn:dev:paid-svc": _SECRET})],
    )


def test_secret_absent_from_flat_tool_specs():
    # invariant SEC-6: the secret never appears in the flat tool specs the model
    # is handed (names, descriptions, parameter schemas).
    client = _client()
    blob = json.dumps(client.tool_specs)
    assert _SECRET not in blob


def test_secret_absent_from_gateway_tool_specs_and_describe():
    # invariant SEC-6: the same holds for the gateway projection surface and every
    # describe response (action, property, event), which return schemas as data.
    client = _client()
    gw = client.gateway()
    assert _SECRET not in json.dumps(gw.tool_specs)
    for affordance in ("charge", "balance", "settled"):
        described = gw._describe({"thing_id": "paid-svc", "affordance": affordance})
        assert _SECRET not in json.dumps(described), affordance
    # the fleet-level describe (no affordance) must also carry no secret
    listing = gw._describe({"thing_id": "paid-svc"})
    assert _SECRET not in json.dumps(listing)


def test_parsed_thing_carries_no_secret():
    # invariant PROT-11 / SEC-6: the TD parse itself holds only the scheme
    # declaration (endpoints, locations), never the runtime secret, so the secret
    # cannot leak through the parsed model either.
    client = _client()
    (thing,) = client.things
    for scheme in thing.security_schemes.values():
        assert _SECRET not in json.dumps(scheme.raw)
