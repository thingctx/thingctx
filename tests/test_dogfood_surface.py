# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Tests for the form-op-closed writable surface (dogfood P3) and FS handler."""

from __future__ import annotations

import pytest

from thingctx import ThingClient, parse_thing
from thingctx.contrib.filesystem import FilesystemHandler
from thingctx.netpolicy import PolicyError
from thingctx.thing import TOOL_SEP


def test_observe_only_property_is_not_writable():
    """Forms that list read/observe but omit writeproperty must not project .set."""
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thingctx:mqtt-demo",
        "title": "MQTT demo",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "properties": {
            "reading": {
                "type": "string",
                "observable": True,
                "forms": [
                    {
                        "href": "mqtt://broker/reading",
                        "op": ["observeproperty", "readproperty"],
                    }
                ],
            },
            "command": {
                "type": "string",
                "forms": [{"href": "mqtt://broker/command", "op": ["writeproperty"]}],
            },
        },
    }
    thing = parse_thing(td)
    assert thing.properties["reading"].readable is True
    assert thing.properties["reading"].writable is False
    assert thing.properties["reading"].observable is True
    assert thing.properties["command"].writable is True
    assert thing.properties["command"].readable is False

    client = ThingClient(tds=[td])
    names = {t["name"] for t in client.tool_surface()}
    assert f"mqtt-demo{TOOL_SEP}reading{TOOL_SEP}get" in names
    assert f"mqtt-demo{TOOL_SEP}reading{TOOL_SEP}set" not in names
    assert f"mqtt-demo{TOOL_SEP}command{TOOL_SEP}set" in names
    assert f"mqtt-demo{TOOL_SEP}command{TOOL_SEP}get" not in names


def test_readOnly_flag_still_closes_write_when_forms_default():
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:sensor",
        "title": "sensor",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "properties": {
            "temp": {
                "type": "number",
                "readOnly": True,
                "forms": [{"href": "http://x/temp"}],
            }
        },
    }
    assert parse_thing(td).properties["temp"].writable is False


def test_filesystem_handler_fail_closed(monkeypatch):
    monkeypatch.delenv("THINGCTX_FS_ROOT", raising=False)
    h = FilesystemHandler()
    with pytest.raises(PolicyError, match="THINGCTX_FS_ROOT"):
        h.readFile("x.txt")


def test_filesystem_handler_confines_and_caps(monkeypatch, tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "ok.txt").write_text("hello")
    monkeypatch.setenv("THINGCTX_FS_ROOT", str(root))
    monkeypatch.setenv("THINGCTX_FS_MAX_BYTES", "8")
    h = FilesystemHandler()
    assert h.readFile("ok.txt")["contents"] == "hello"
    assert h.listDir(".")["entries"] == ["ok.txt"]
    with pytest.raises(PolicyError, match="escapes|destination"):
        h.readFile("../outside.txt")
    with pytest.raises(PolicyError, match="cap"):
        h.writeFile("big.txt", "0123456789")  # 10 > 8
    assert h.writeFile("small.txt", "hi")["written"] == 2


def test_filesystem_handler_refuses_symlink_escape(monkeypatch, tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    link = root / "leak.txt"
    link.symlink_to(outside)
    monkeypatch.setenv("THINGCTX_FS_ROOT", str(root))
    h = FilesystemHandler()
    with pytest.raises(PolicyError, match="escapes|destination"):
        h.readFile("leak.txt")
