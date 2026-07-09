# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Bring-your-own-PDP is real: the same decision through the local PDP and through
an external AuthZEN PDP agree, and the identity survives the external hop.

The seam speaks OpenID AuthZEN 1.0. This proves two things a "pluggable, no
lock-in" claim needs:

1. PARITY: for the same (identity, operation), the lean local PDP and an external
   AuthZEN PDP return the SAME allow/deny. Swapping the decider does not change
   the answer, so an adopter can move to their own OPA/Cedar without surprises.
2. IDENTITY SURVIVES THE HOP: the caller's full claims reach the external PDP
   intact in the AuthZEN request body, so an external policy can read roles,
   appid, tenant, whatever it needs. The identity is not flattened at the seam.

The external PDP here is an in-process stub that implements the SAME policy as the
local grant source, reading the claims from the AuthZEN request. No network: the
stub replaces the one httpx call AuthZenPDP makes.
"""

from __future__ import annotations

import pytest

from thingctx import LocalBinding, ThingClient
from thingctx.authz import (
    AccessRequest,
    LocalPolicyGrantSource,
    PolicyDecisionPoint,
    build_vocabulary,
)
from thingctx.authz.authzen import AuthZenPDP

THING_ID = "urn:demo:pump"

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": THING_ID,
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "properties": {
        "target_rpm": {
            "type": "integer",
            "forms": [{"href": "local://target_rpm", "op": ["readproperty", "writeproperty"]}],
        }
    },
}

# One policy, expressed once: operator may read, not write.
GRANTS = {"operator": {(THING_ID, "target_rpm", "readproperty")}}


def _local_pdp():
    vocab = build_vocabulary(ThingClient(tds=[TD], bindings=[LocalBinding(object())]).things)
    return PolicyDecisionPoint(vocabulary=vocab, grant_source=LocalPolicyGrantSource(GRANTS))


class _StubExternalPolicy:
    """An external AuthZEN PDP implementing the SAME policy, reading the AuthZEN
    request body. Stands in for OPA/Cedar; captures the request so the test can
    assert the identity survived the hop."""

    def __init__(self):
        self.last_request = None

    def evaluate(self, body: dict) -> dict:
        self.last_request = body
        roles = (body.get("subject", {}).get("properties", {}) or {}).get("roles", [])
        action = body.get("action", {}).get("name")
        resource_id = body.get("resource", {}).get("id", "")
        thing, _, affordance = resource_id.partition("/")
        allow = any((thing, affordance, action) in GRANTS.get(r, set()) for r in roles)
        return {"decision": bool(allow)}


def _authzen_pdp(stub):
    """An AuthZenPDP whose single httpx POST is redirected to the stub, so the
    real request/response mapping runs with no network."""
    pdp = AuthZenPDP("https://pdp.internal")

    async def decide(identity, request):
        from thingctx.authz.authzen import from_authzen_response, to_authzen_request

        body = to_authzen_request(identity, request)
        payload = stub.evaluate(body)
        return from_authzen_response(payload, request)

    pdp.decide = decide  # replace the transport-bound decide with the stubbed one
    return pdp


OPERATOR = {"sub": "alice", "roles": ["operator"]}


@pytest.mark.asyncio
async def test_local_and_external_pdp_agree_on_every_case():
    """Parity: local PDP and external AuthZEN PDP return the same decision."""
    local = _local_pdp()
    stub = _StubExternalPolicy()
    external = _authzen_pdp(stub)

    cases = [
        ("target_rpm", "readproperty", True),
        ("target_rpm", "writeproperty", False),
    ]
    for affordance, op, expected in cases:
        req = AccessRequest(thing_id=THING_ID, affordance=affordance, op=op)
        local_decision = await local.decide(OPERATOR, req)
        external_decision = await external.decide(OPERATOR, req)
        assert local_decision.permit is expected, (affordance, op, "local")
        assert external_decision.permit is expected, (affordance, op, "external")
        assert local_decision.permit == external_decision.permit, (affordance, op, "parity")


@pytest.mark.asyncio
async def test_identity_survives_the_authzen_hop():
    """The caller's full claims reach the external PDP in the AuthZEN request."""
    stub = _StubExternalPolicy()
    external = _authzen_pdp(stub)
    req = AccessRequest(thing_id=THING_ID, affordance="target_rpm", op="readproperty")

    await external.decide(OPERATOR, req)

    body = stub.last_request
    assert body is not None
    # subject id + the full claims survived as subject.properties
    assert body["subject"]["properties"]["roles"] == ["operator"]
    assert body["subject"]["properties"]["sub"] == "alice"
    # the operation and resource are the WoT op and the thing/affordance
    assert body["action"]["name"] == "readproperty"
    assert body["resource"]["id"] == f"{THING_ID}/target_rpm"


@pytest.mark.asyncio
async def test_external_pdp_drives_the_client_the_same_as_local():
    """End to end: a ThingClient wired to the external PDP enforces identically."""
    from thingctx.authz import AuthorizationDenied

    class Pump:
        def __init__(self):
            self._v = 1200

        def get_target_rpm(self):
            return self._v

        def set_target_rpm(self, value):
            self._v = value
            return {"ok": True}

    stub = _StubExternalPolicy()
    client = ThingClient(
        tds=[TD], bindings=[LocalBinding(Pump())], pdp=_authzen_pdp(stub), identity=OPERATOR
    )
    assert await client.read_property("pump.target_rpm") == 1200
    with pytest.raises(AuthorizationDenied):
        await client.write_property("pump.target_rpm", 3000)
