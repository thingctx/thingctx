# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The declared dependency floors have to match what the code actually calls.

A lower bound is a promise. thingctx carried ``mcp>=1.0`` for a long time while
``mcp.server.lowlevel`` did not exist before 1.2 and ``instructions`` arrived in
1.3, so the package promised a version the bridge could not even import. Nothing
caught it because the floor is only exercised when someone installs the oldest
allowed release, which CI never does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _declared_floors(package: str) -> set[str]:
    """Every lower bound pinned for ``package`` across the extras.

    Read with a regex rather than tomllib, which is 3.11+ and this suite runs on
    3.10.
    """
    # Capture only the version, so adding an upper bound later ("mcp>=2,<3")
    # narrows the pin rather than turning this into a confusing failure.
    text = _PYPROJECT.read_text()
    return set(re.findall(rf'"{re.escape(package)}>=([0-9][0-9.]*)', text))


@pytest.mark.skipif(not _PYPROJECT.exists(), reason="runs against the source tree")
def test_every_mcp_pin_declares_the_same_floor():
    """Reading the pins needs no SDK, so this must not hide behind an importorskip:
    the guard is worth least on the machine that happens to have mcp installed."""
    assert _declared_floors("mcp") == {"2"}, (
        "every mcp pin must declare the floor the bridge actually builds on"
    )


def test_installed_mcp_accepts_what_the_bridge_passes():
    pytest.importorskip("mcp")
    import inspect

    from mcp.server.lowlevel import Server

    # These are passed by build_mcp_server; the callback registration API is
    # the MCP 2 compatibility boundary guarded by the floor above.
    accepted = set(inspect.signature(Server.__init__).parameters)
    required = {
        "version",
        "instructions",
        "on_list_tools",
        "on_call_tool",
        "on_list_resources",
        "on_list_resource_templates",
        "on_read_resource",
        "on_subscribe_resource",
        "on_unsubscribe_resource",
        "on_list_prompts",
        "on_get_prompt",
    }
    assert required <= accepted, (
        f"the installed mcp is missing {sorted(required - accepted)}"
    )
