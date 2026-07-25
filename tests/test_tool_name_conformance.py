# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Every projected tool name must match the strictest MCP client charset.

The MCP spec does not constrain tool names, so clients enforce their own rule;
the strictest known (Claude Desktop) is ``^[a-zA-Z0-9_-]{1,64}$`` (no dots,
colons, slashes; length <= 64). thingctx targets that intersection so a name is
never rejected by any client. This test is the guard: it would have caught the
dot-separator that failed Claude Desktop on load.
"""

from __future__ import annotations

import re

from thingctx.integrations.mcp import client_from_registry
from thingctx.thing import _tool_name, _tool_slug

MCP_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _reg(tds):
    class R:
        def fetch(self):
            return tds

    return R()


def test_every_projected_tool_name_is_mcp_legal():
    """A TD covering actions, a writable property (-> __set), and an event
    projects only MCP-legal tool names."""
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:pump:v1",
        "title": "Pump",
        "securityDefinitions": {"n": {"scheme": "nosec"}},
        "security": "n",
        "properties": {
            "target_rpm": {
                "type": "number",
                "forms": [{"href": "http://d/t", "op": ["readproperty", "writeproperty"]}],
            }
        },
        "actions": {"set_speed": {"forms": [{"href": "http://d/s"}]}},
        "events": {"overheat": {"forms": [{"href": "http://d/o", "op": ["subscribeevent"]}]}},
    }
    client = client_from_registry(_reg([td]))
    names = [s["function"]["name"] for s in client.list_actions()]
    names += list(client.list_properties())
    names += list(client.list_events())
    assert names, "projection produced no names"
    for n in names:
        assert MCP_NAME.match(
            n
        ), f"tool name {n!r} is not MCP-legal (needs ^[a-zA-Z0-9_-]{{1,64}}$)"


def test_tool_name_uses_double_underscore_and_slug_recovers():
    """The separator is ``__`` and the slug is recoverable from it."""
    name = _tool_name("urn:demo:pump:v1", "set_speed")
    assert name == "pump__set_speed"
    assert "." not in name
    assert _tool_slug(name) == "pump"
    # an action name that itself contains ``_`` survives (split on FIRST ``__``)
    assert _tool_slug(_tool_name("urn:x:cam", "read_frame")) == "cam"
