# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""THINGCTX_POLICY: the no-code per-operation posture for the MCP bridge.

A single local user picks read-only / full via one env var; the bridge wires a PDP so a
denied op is refused before the service is touched. read-only permits reads plus safe
(no-change) actions and denies property writes and unsafe actions. No policy set = no
PDP (unchanged). Proves the coarse control a marketplace user can actually set.
"""

from __future__ import annotations

import pytest

from thingctx.integrations.mcp import client_from_registry

LAMP = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:lamp",
    "title": "Lamp",
    "securityDefinitions": {"n": {"scheme": "nosec"}},
    "security": ["n"],
    "properties": {
        "level": {
            "type": "number",
            "forms": [{"href": "local://l", "op": ["readproperty", "writeproperty"]}],
        }
    },
    "actions": {
        "reset": {"forms": [{"href": "local://l/reset"}]},
        "status": {"safe": True, "forms": [{"href": "local://l/status"}]},
    },
}


class _Reg:
    def fetch(self):
        return [LAMP]


async def _verdict(coro) -> str:
    from thingctx.authz.pdp import AuthorizationDenied

    try:
        await coro
        return "allow"
    except AuthorizationDenied:
        return "deny"
    except Exception:  # a transport error past the authz gate still means "allowed"
        return "allow"


@pytest.mark.parametrize(
    "policy,read,write,action",
    [
        ("read-only", "allow", "deny", "deny"),
        ("full", "allow", "allow", "allow"),
    ],
)
async def test_policy_presets(monkeypatch, policy, read, write, action):
    monkeypatch.setenv("THINGCTX_POLICY", policy)
    c = client_from_registry(_Reg())
    assert await _verdict(c.read_property("lamp__level")) == read
    assert await _verdict(c.write_property("lamp__level", 5)) == write
    assert await _verdict(c.invoke("lamp__reset")) == action
    await c.aclose()


async def test_read_only_permits_safe_action_denies_unsafe(monkeypatch):
    # read-only grants invokeaction on SAFE actions only. An unsafe action (no safe
    # flag, so safe=false) must be denied, even though both are invokeaction. This is
    # the boundary a coarse "read-only" posture promises: run a status query, never a
    # destructive action, on the same Thing.
    monkeypatch.setenv("THINGCTX_POLICY", "read-only")
    c = client_from_registry(_Reg())
    assert await _verdict(c.invoke("lamp__status")) == "allow"  # safe
    assert await _verdict(c.invoke("lamp__reset")) == "deny"  # unsafe
    await c.aclose()


async def test_no_policy_means_no_pdp(monkeypatch):
    """Unset THINGCTX_POLICY leaves the client with no PDP (backward compatible)."""
    monkeypatch.delenv("THINGCTX_POLICY", raising=False)
    c = client_from_registry(_Reg())
    assert c._pdp is None
    assert await _verdict(c.write_property("lamp__level", 5)) == "allow"
    await c.aclose()


def test_unknown_preset_is_a_clear_error():
    from thingctx.authz.pdp import StaticGrantSource

    with pytest.raises(ValueError, match="unknown policy preset"):
        StaticGrantSource("bogus", thing_ids=["urn:x"])
