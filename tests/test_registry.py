# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The registry: load TDs from a folder."""

from __future__ import annotations

import json

from thingctx import FileRegistry, ThingClient


def test_file_registry_loads_a_folder(tmp_path):
    (tmp_path / "a.td.json").write_text(
        json.dumps(
            {
                "@context": "https://www.w3.org/2022/wot/td/v1.1",
                "id": "urn:x:a:v1",
                "title": "A",
                "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
                "security": ["nosec_sc"],
                "actions": {"ping": {"forms": [{"href": "local://ping"}]}},
            }
        )
    )
    tds = FileRegistry(str(tmp_path)).fetch()
    assert len(tds) == 1 and tds[0]["id"] == "urn:x:a:v1"
    client = ThingClient.from_registry(FileRegistry(str(tmp_path)))
    assert any("ping" in s["function"]["name"] for s in client.list_actions())


def _td(id_suffix):
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": f"urn:x:{id_suffix}:v1",
        "title": id_suffix.upper(),
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {"ping": {"forms": [{"href": "local://ping"}]}},
    }


def test_catalog_payload_resolves_references():
    from thingctx.registry import _tds_from_payload

    served = {
        "https://reg.example/v0/things/a.td.json": _td("a"),
        "https://reg.example/v0/things/b.td.json": _td("b"),
    }
    catalog = {
        "version": "v0",
        "things": [
            {"id": "urn:x:a:v1", "served_at": "https://reg.example/v0/things/a.td.json"},
            {"id": "urn:x:b:v1", "file": "v0/things/b.td.json"},
        ],
    }
    tds = _tds_from_payload(catalog, "https://reg.example/index.json", served.__getitem__)
    assert [t["id"] for t in tds] == ["urn:x:a:v1", "urn:x:b:v1"]


def test_catalog_payload_keeps_inline_tds():
    from thingctx.registry import _tds_from_payload

    wrapper = {"things": [_td("a"), _td("b")]}
    tds = _tds_from_payload(wrapper, "https://tdd.example/things", None)
    assert [t["id"] for t in tds] == ["urn:x:a:v1", "urn:x:b:v1"]


def test_single_td_payload_passes_through():
    from thingctx.registry import _tds_from_payload

    td = _td("solo")
    assert _tds_from_payload(td, "https://reg.example/solo.td.json", None) == [td]


def test_td_list_payload_passes_through():
    from thingctx.registry import _tds_from_payload

    tds = _tds_from_payload([_td("a"), _td("b")], "https://reg.example/all.json", None)
    assert len(tds) == 2


def test_directory_url_reads_its_index(monkeypatch):
    from thingctx import registry as reg

    asked = []

    def fake_get_json(url, timeout):
        asked.append(url)
        if url.endswith("index.json"):
            return {"things": [{"id": "urn:x:a:v1", "served_at": "things/a.td.json"}]}
        return _td("a")

    monkeypatch.setattr(reg, "_get_json", fake_get_json)
    tds = reg.FileRegistry("https://reg.example/v0/").fetch()
    assert asked[0] == "https://reg.example/v0/index.json"
    assert asked[1] == "https://reg.example/v0/things/a.td.json"
    assert [t["id"] for t in tds] == ["urn:x:a:v1"]
