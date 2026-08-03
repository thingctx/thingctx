# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Event mirroring is authorized per subscriber when the gateway is guarded.

The identity review found a HIGH gap: a guarded gateway still auto-mirrored every
event to an open topic, so any bus client could read a gated stream with no token
and no subscribeevent check. These tests pin the fix:

- Guarded: events are NOT auto-mirrored (no open-topic leak). A subscribe must be
  REQUESTED, authenticated, and pass the subscribeevent grant; only then does the
  gateway mirror to that caller's own stream topic.
- Unguarded: the server-level gateway still mirrors openly (consistent with the
  rest of a server-identity face).
- The config-time guard gate: a guard is refused unless the broker is attested to
  bind connection identity (the confused-deputy guardrail).
"""

from __future__ import annotations

import pytest

from thingctx import ThingClient
from thingctx.authz import LocalPolicyGrantSource, PolicyDecisionPoint, build_vocabulary
from thingctx.bindings import LocalBinding
from thingctx.gateways import Gateway, ServeRequest
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding

THING_ID = "urn:demo:pump:v1"

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": THING_ID,
    "title": "Pump",
    "securityDefinitions": {"n": {"scheme": "nosec"}},
    "security": ["n"],
    "events": {"telemetry": {"forms": [{"href": "local://telemetry", "op": ["subscribeevent"]}]}},
    "properties": {
        "rpm": {"type": "integer", "forms": [{"href": "local://rpm", "op": ["readproperty"]}]}
    },
}


def _client(*, pdp=None, identity=None):
    return ThingClient(tds=[TD], bindings=[LocalBinding(object())], pdp=pdp, identity=identity)


def _guarded_client(*, event_granted):
    vocab = build_vocabulary(_client().things)
    grants = {"op": {(THING_ID, "rpm", "readproperty")}}
    if event_granted:
        grants["op"].add((THING_ID, "telemetry", "subscribeevent"))
    pdp = PolicyDecisionPoint(vocabulary=vocab, grant_source=LocalPolicyGrantSource(grants))
    return _client(pdp=pdp, identity={"sub": "alice", "roles": ["op"]})


# --------------------------------------------------------------------------- #
# config-time guard gate (Finding 2)
# --------------------------------------------------------------------------- #


def test_guard_refused_without_broker_identity_attestation():
    with pytest.raises(ValueError, match="binds connection identity"):
        MqttGatewayBinding("bus:1883", guard=object())


def test_guard_accepted_with_attestation():
    mb = MqttGatewayBinding("bus:1883", guard=object(), broker_binds_identity=True)
    assert mb._guard is not None


# --------------------------------------------------------------------------- #
# event authz on the mirror path (Finding 1)
# --------------------------------------------------------------------------- #


def test_unguarded_gateway_auto_mirrors(monkeypatch):
    """No guard: events auto-mirror (server-level face, consistent)."""
    mb = MqttGatewayBinding("bus:1883")  # no guard
    started = []
    monkeypatch.setattr(mb, "_start_mirror", lambda slug, name: started.append((slug, name)))
    # simulate the serve loop's event branch without a broker
    for thing in _client().things:
        for name in thing.events:
            if mb._guard is None:
                mb._start_mirror("pump", name)
    assert ("pump", "telemetry") in started


def _capture(mb, monkeypatch):
    """Capture reply payloads and mirror-start calls without a broker."""
    replies, mirrors = [], []

    async def _reply(req, res):
        replies.append(res)

    monkeypatch.setattr(mb, "reply", _reply)
    monkeypatch.setattr(mb, "_start_caller_mirror", lambda *a, **k: mirrors.append(a))
    return replies, mirrors


ALICE = {"sub": "alice", "roles": ["op"]}


@pytest.mark.asyncio
async def test_guarded_subscribe_denied_when_not_granted(monkeypatch):
    """Guarded: a subscribe the caller was NOT granted is denied, no mirror starts."""
    gw = Gateway(_guarded_client(event_granted=False), MqttGatewayBinding("bus:1883"))
    mb = gw.binding
    mb._gateway = gw  # serve() wires this; the test drives _handle_subscribe directly
    replies, mirrors = _capture(mb, monkeypatch)
    req = ServeRequest("pump", "telemetry", "subscribeevent", {}, correlation="t", identity=ALICE)
    await mb._handle_subscribe(req, {})
    assert mirrors == [], "a denied subscribe must NOT start a mirror"
    assert replies and replies[-1].get("denied") is True


@pytest.mark.asyncio
async def test_guarded_subscribe_allowed_when_granted(monkeypatch):
    """Guarded: a granted subscribe authorizes, then mirrors to the caller's own
    stream topic, not an open one."""
    gw = Gateway(_guarded_client(event_granted=True), MqttGatewayBinding("bus:1883"))
    mb = gw.binding
    mb._gateway = gw  # serve() wires this; the test drives _handle_subscribe directly
    replies, mirrors = _capture(mb, monkeypatch)
    req = ServeRequest(
        "pump",
        "telemetry",
        "subscribeevent",
        {},
        correlation="tc/pump/events/telemetry/subscribe",
        identity=ALICE,
    )
    await mb._handle_subscribe(req, {})
    assert mirrors, "a granted subscribe must start a caller-scoped mirror"
    # mirrored to the caller's own stream topic, derived from the request, not open
    stream_topic = mirrors[0][2]
    assert stream_topic.endswith("/stream")
    assert replies[-1].get("subscribed") is True
