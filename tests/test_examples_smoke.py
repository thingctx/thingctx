# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Smoke tests over the shipped examples and registry TDs.

Two guarantees a docs-only change can silently break, locked here:

* the authorization example enforces deny (the write is DENIED, the read is
  ALLOWED), so a tool-name or separator drift cannot turn the deny demo into
  a silent allow;
* the bundled time TD projects tools and invokes fully offline against the
  in-process time handler, so the quickstart stays runnable with no network
  and no credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def _run_example(name: str) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    proc = subprocess.run(
        [sys.executable, str(EXAMPLES / name)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO),
        env=env,
        check=False,
    )
    assert proc.returncode == 0, f"{name} failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def test_authz_example_denies_write_and_allows_read():
    out = _run_example("14_authz.py")
    assert "READ  target_rpm  -> ALLOWED" in out
    assert "WRITE target_rpm  -> DENIED" in out
    assert "ALLOWED (unexpected)" not in out
    assert "unchanged: the denied write never ran" in out


def test_authn_to_authz_example_denies_write_and_allows_read():
    jwt = pytest.importorskip("jwt")
    pytest.importorskip("cryptography")
    assert hasattr(jwt, "encode"), "pyjwt (not the jwt package) is required"
    out = _run_example("15_authn_to_authz.py")
    assert "token VALIDATED" in out
    assert "READ  target_rpm  -> ALLOWED" in out
    assert "WRITE target_rpm  -> DENIED" in out
    assert "ALLOWED (unexpected)" not in out


async def test_time_td_projects_and_invokes_offline():
    from thingctx import LocalBinding, ThingClient
    from thingctx.contrib.time import make_time_handler

    td = json.loads((EXAMPLES / "registry" / "time.td.json").read_text())
    client = ThingClient(tds=[td], bindings=[LocalBinding(make_time_handler())])
    tools, invoke = client.as_tools()
    names = [t["function"]["name"] for t in tools]
    assert "time__getCurrentTime" in names
    assert "time__convertTime" in names

    now = await invoke("time__getCurrentTime", {"timezone": "UTC"})
    assert now["timezone"] == "UTC"
    assert "datetime" in now and "utc_offset" in now

    conv = await invoke(
        "time__convertTime",
        {"time": "09:00", "source_timezone": "UTC", "target_timezone": "Asia/Tokyo"},
    )
    assert conv["target"]["timezone"] == "Asia/Tokyo"


def test_time_td_is_schema_valid():
    pytest.importorskip("jsonschema")
    from thingctx import validate_td

    td = json.loads((EXAMPLES / "registry" / "time.td.json").read_text())
    validate_td(td)
