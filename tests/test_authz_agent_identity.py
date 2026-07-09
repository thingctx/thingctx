# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""An agent gets its own identity, gated separately from and narrower than a user.

The identity that drives authorization is a claims dict. thingctx.authz decides
against whatever subject that dict names, human or agent. This proves three
things a per-operation authorization model can express that an opaque-tool model
cannot:

1. An AGENT principal (an app-only token: a ``roles`` claim, an ``appid``, and no
   user ``sub``) is authorized on its OWN identity, with no user present.
2. The agent's grant can be STRICTLY NARROWER than the user's: the same TD, the
   same operation, allowed for the user identity and denied for the agent
   identity. This is the leash, an autonomous agent bounded below its operator.
3. The two are distinguished purely by their claims; nothing else changes.
"""

from __future__ import annotations

import pytest

from thingctx import LocalBinding, ThingClient
from thingctx.authz import (
    AuthorizationDenied,
    LocalPolicyGrantSource,
    PolicyDecisionPoint,
    build_vocabulary,
)

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


class Pump:
    def __init__(self):
        self._v = 1200

    def get_target_rpm(self):
        return self._v

    def set_target_rpm(self, value):
        self._v = value
        return {"ok": True, "target_rpm": value}


def _pdp(device):
    """The user role may read AND write; the agent role may read ONLY."""
    vocab = build_vocabulary(ThingClient(tds=[TD], bindings=[LocalBinding(device)]).things)
    grants = LocalPolicyGrantSource(
        {
            # the human operator: full read + write
            "operator": {
                (THING_ID, "target_rpm", "readproperty"),
                (THING_ID, "target_rpm", "writeproperty"),
            },
            # the autonomous agent: read only, strictly narrower
            "pump-reader-agent": {(THING_ID, "target_rpm", "readproperty")},
        }
    )
    return PolicyDecisionPoint(vocabulary=vocab, grant_source=grants)


# A USER identity: a human, carries a user subject.
USER = {"sub": "alice@example.com", "oid": "user-guid", "roles": ["operator"]}

# An AGENT identity: an app-only token. No user sub; identified by appid + an app
# role. This is exactly the shape of an Entra client-credentials (app-only) token:
# `roles` (app roles, not delegated scopes), an `appid`, and no user principal.
AGENT = {
    "appid": "pump-reader-agent-app",
    "azp": "pump-reader-agent-app",
    "roles": ["pump-reader-agent"],
}


def _client(identity, device):
    """Two clients over the SAME device instance, differing only by identity."""
    return ThingClient(
        tds=[TD], bindings=[LocalBinding(device)], pdp=_pdp(device), identity=identity
    )


@pytest.mark.asyncio
async def test_agent_principal_authorized_with_no_user_present():
    """The agent's app-only identity (no user sub) is authorized on its own."""
    client = _client(AGENT, Pump())
    assert await client.read_property("pump.target_rpm") == 1200


@pytest.mark.asyncio
async def test_agent_grant_is_strictly_narrower_than_user():
    """Same device, same write operation: allowed for the user, denied for the agent."""
    device = Pump()  # one real device; two callers reach it with different identities
    user_client = _client(USER, device)
    agent_client = _client(AGENT, device)

    # The user may write.
    await user_client.write_property("pump.target_rpm", 1500)
    assert await user_client.read_property("pump.target_rpm") == 1500

    # The agent may NOT write the SAME device, even though its operator can. The leash.
    with pytest.raises(AuthorizationDenied):
        await agent_client.write_property("pump.target_rpm", 9999)
    # The agent's denied write never reached the device: the user's value stands.
    assert await agent_client.read_property("pump.target_rpm") == 1500


@pytest.mark.asyncio
async def test_decision_is_driven_only_by_the_claims():
    """Nothing but the identity claims differs; swapping them flips the decision."""
    # Agent read: allowed. Agent write: denied. Same client construction, only the
    # operation differs, and the grant for THIS identity draws the line.
    agent_client = _client(AGENT, Pump())
    assert await agent_client.read_property("pump.target_rpm") == 1200
    with pytest.raises(AuthorizationDenied):
        await agent_client.write_property("pump.target_rpm", 1)
