"""add_things registers TDs into a live client: the runtime path a
self-describing binding needs (see docs/DISCOVERY.md). Covers append, the
id-collision replace policy, and that projected tools/routes update."""

from __future__ import annotations

import pytest

from thingctx import ThingClient


def _td(thing_id, action, href="local://x"):
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": thing_id,
        "title": thing_id,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {action: {"forms": [{"href": href}]}},
    }


def _names(client):
    return sorted(s["function"]["name"] for s in client.list_actions())


def test_add_things_appends_and_projects():
    c = ThingClient(tds=[_td("urn:demo:a:v1", "ping")])
    assert _names(c) == ["a__ping"]
    added = c.add_things([_td("urn:demo:b:v1", "halt")])
    assert added == ["urn:demo:b:v1"]
    assert _names(c) == ["a__ping", "b__halt"]
    # the new Thing is routable, not just listed
    assert c.action_for("b__halt") is not None


def test_add_things_replaces_on_id_collision():
    # A device re-describing itself supersedes its old shape: no duplicate, the
    # new action set wins.
    c = ThingClient(tds=[_td("urn:demo:b:v1", "halt")])
    assert _names(c) == ["b__halt"]
    re_td = _td("urn:demo:b:v1", "reset")
    re_td["actions"]["boot"] = {"forms": [{"href": "local://boot"}]}
    c.add_things([re_td])
    # old "halt" is gone, the redescribed actions are present, no stale dupes
    assert _names(c) == ["b__boot", "b__reset"]
    assert c.action_for("b__halt") is None


def test_add_things_preserves_order_for_non_colliding():
    c = ThingClient(tds=[_td("urn:demo:a:v1", "ping")])
    c.add_things([_td("urn:demo:b:v1", "halt"), _td("urn:demo:c:v1", "go")])
    # a stays first; b, c append in order
    assert [t.id for t in c.things] == ["urn:demo:a:v1", "urn:demo:b:v1", "urn:demo:c:v1"]


@pytest.mark.asyncio
async def test_added_thing_is_drivable():
    class LocalShim:
        scheme = "local"

        async def invoke(self, action, form, arguments):
            return {"ran": action.name}

    c = ThingClient(tds=[_td("urn:demo:a:v1", "ping")], bindings=[LocalShim()])
    c.add_things([_td("urn:demo:b:v1", "halt")])
    out = await c.invoke("b__halt", {})
    assert out == {"ran": "halt"}
