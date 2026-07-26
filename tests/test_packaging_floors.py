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
    text = _PYPROJECT.read_text()
    return set(re.findall(rf'"{re.escape(package)}>=([0-9][^"]*)"', text))


@pytest.mark.skipif(not _PYPROJECT.exists(), reason="runs against the source tree")
def test_mcp_floor_matches_what_the_bridge_calls():
    pytest.importorskip("mcp")
    import inspect

    from mcp.server.lowlevel import Server

    # Both are passed by build_mcp_server. instructions is the later of the two,
    # so the floor is the release that has it.
    accepted = set(inspect.signature(Server.__init__).parameters)
    assert {"version", "instructions"} <= accepted, (
        f"the installed mcp is missing {sorted({'version', 'instructions'} - accepted)}"
    )

    assert _declared_floors("mcp") == {"1.3"}, (
        "every mcp pin must declare the floor the bridge actually builds on"
    )
