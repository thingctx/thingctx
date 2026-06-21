"""The registry: load TDs from a folder."""

from __future__ import annotations

import json
from pathlib import Path

from thingctx import FileRegistry, ThingClient, parse_thing


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


def test_xquik_registry_td_is_parseable():
    root = Path(__file__).resolve().parents[1]
    td = json.loads((root / "examples/registry/xquik.td.json").read_text())
    thing = parse_thing(td)

    assert thing.id == "urn:thingctx:xquik:v1"
    assert set(thing.actions) == {
        "getUser",
        "getXTrends",
        "lookupTweet",
        "searchTweets",
        "searchUsers",
    }
    assert thing.security == ("apiKey",)
