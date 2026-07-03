"""The gateway projection: a constant six-verb surface over any fleet size.

The guarantees under test:
- the tool surface is exactly six verbs, and stays six whether the client holds
  one Thing or a thousand (the whole point);
- search -> describe -> invoke is a working round trip, with the input schema
  returned by describe at call time;
- an unknown thing_id / action / property fails with a readable envelope (with
  near-matches), not a raise;
- auto mode picks flat under the threshold and gateway above it.
"""

from __future__ import annotations

import pytest

from thingctx import GatewayProjection, LocalBinding, ThingClient
from thingctx.gateway import GATEWAY_TOOL_NAMES, keyword_search


def _local_td(slug: str, title: str, desc: str) -> dict:
    """A local:// Thing with one readable action, callable in-process."""
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": f"urn:demo:{slug}:v1",
        "title": title,
        "description": desc,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {
            "status": {
                "description": f"report {slug} status",
                "idempotent": True,
                "input": {"type": "object", "properties": {"verbose": {"type": "boolean"}}},
                "forms": [{"href": "local://status"}],
            }
        },
    }


def _bare_td(slug: str) -> dict:
    """A minimal http Thing, enough to count toward the surface, no handler."""
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": f"urn:demo:{slug}:v1",
        "title": slug,
        "actions": {"ping": {"idempotent": True, "forms": [{"href": f"https://x/{slug}/ping"}]}},
    }


class _Status:
    def status(self, verbose: bool = False) -> dict:
        return {"ok": True, "verbose": verbose}


@pytest.fixture
def local_binding():
    """A LocalBinding with handlers registered per Thing slug, so a
    local://status action resolves to _Status.status()."""
    binding = LocalBinding()
    binding.register_thing("pump", _Status())
    binding.register_thing("boiler", _Status())
    return binding


# --------------------------------------------------------------------------- #
# the constant surface
# --------------------------------------------------------------------------- #


def test_surface_is_exactly_six_verbs():
    client = ThingClient(tds=[_bare_td("a")], bindings=[])
    gw = client.gateway()
    names = [t["function"]["name"] for t in gw.tool_specs]
    assert set(names) == GATEWAY_TOOL_NAMES
    assert len(names) == 6


def test_surface_stays_six_at_fleet_scale():
    # One Thing, then a thousand: the flat surface would be ~1000 tools; the
    # gateway is six either way. That is the property that makes it O(1).
    small = ThingClient(tds=[_bare_td("a")], bindings=[])
    large = ThingClient(tds=[_bare_td(f"t{i}") for i in range(1000)], bindings=[])
    assert len(large.tool_specs) >= 1000  # flat would blow up
    assert len(small.gateway().tool_specs) == 6
    assert len(large.gateway().tool_specs) == 6


# --------------------------------------------------------------------------- #
# search -> describe -> invoke
# --------------------------------------------------------------------------- #


def test_search_finds_by_title_and_description():
    tds = [
        _local_td("pump", "Water Pump", "controls the coolant loop"),
        _local_td("boiler", "Steam Boiler", "heats the vessel"),
    ]
    client = ThingClient(tds=tds, bindings=[])
    gw = client.gateway()
    hits = gw._search({"query": "coolant"})
    assert hits["count"] == 1
    assert hits["results"][0]["thing_id"] == "pump"


def test_describe_returns_input_schema_at_call_time():
    client = ThingClient(tds=[_local_td("pump", "Pump", "x")], bindings=[])
    gw = client.gateway()
    out = gw._describe({"thing_id": "pump", "affordance": "status"})
    assert out["kind"] == "action"
    # the schema the model needs to fill arguments, returned as data
    assert out["input_schema"]["properties"]["verbose"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_invoke_round_trip(local_binding):
    client = ThingClient(tds=[_local_td("pump", "Pump", "x")], bindings=[local_binding])
    gw = client.gateway()
    result = await gw.call_tool(
        "invoke_action", {"thing_id": "pump", "action": "status", "arguments": {"verbose": True}}
    )
    assert result == {"ok": True, "verbose": True}


@pytest.mark.asyncio
async def test_full_flow_search_then_describe_then_invoke(local_binding):
    tds = [_local_td("pump", "Water Pump", "coolant"), _local_td("boiler", "Boiler", "heat")]
    client = ThingClient(tds=tds, bindings=[local_binding])
    gw = client.gateway()
    found = await gw.call_tool("search_things", {"query": "coolant"})
    tid = found["results"][0]["thing_id"]
    schema = await gw.call_tool("describe", {"thing_id": tid, "affordance": "status"})
    assert "verbose" in schema["input_schema"]["properties"]
    out = await gw.call_tool("invoke_action", {"thing_id": tid, "action": "status"})
    assert out["ok"] is True


# --------------------------------------------------------------------------- #
# id discipline: unknown targets fail readably, never raise
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_thing_id_returns_near_matches():
    client = ThingClient(tds=[_local_td("pump", "Pump", "x")], bindings=[])
    gw = client.gateway()
    out = await gw.call_tool("describe", {"thing_id": "pum"})  # substring of "pump"
    assert "error" in out and "pump" in out["error"]  # near-match hint


@pytest.mark.asyncio
async def test_unknown_action_lists_available(local_binding):
    client = ThingClient(tds=[_local_td("pump", "Pump", "x")], bindings=[local_binding])
    gw = client.gateway()
    out = await gw.call_tool("invoke_action", {"thing_id": "pump", "action": "nope"})
    assert "error" in out and out["actions"] == ["status"]


# --------------------------------------------------------------------------- #
# projection mode selection
# --------------------------------------------------------------------------- #


def test_auto_mode_flat_below_threshold_gateway_above():
    small = ThingClient(tds=[_bare_td("a")], bindings=[])
    assert small.projection("auto").tool_specs is small.tool_specs  # flat

    big = ThingClient(tds=[_bare_td(f"t{i}") for i in range(50)], bindings=[])
    picked = big.projection("auto")  # 50 tools > default flat_max=24
    assert len(picked.tool_specs) == 6  # gateway


def test_explicit_modes():
    client = ThingClient(tds=[_bare_td("a")], bindings=[])
    assert len(client.projection("gateway").tool_specs) == 6
    assert client.projection("flat").tool_specs is client.tool_specs
    with pytest.raises(ValueError):
        client.projection("nonsense")


def test_gateway_projection_constructs_directly():
    client = ThingClient(tds=[_bare_td("a")], bindings=[])
    gw = GatewayProjection(client)
    assert len(gw.tool_specs) == 6


# --------------------------------------------------------------------------- #
# search unit
# --------------------------------------------------------------------------- #


def test_keyword_search_ranks_by_term_hits():
    client = ThingClient(
        tds=[
            _local_td("pump", "Coolant Pump", "coolant flow"),
            _local_td("valve", "Coolant Valve", "shutoff"),
            _local_td("light", "Lamp", "brightness"),
        ],
        bindings=[],
    )
    ranked = keyword_search(client.things, "coolant pump", 8)
    assert ranked[0].title == "Coolant Pump"  # matches both terms, ranks first
    assert all(t.title != "Lamp" for t in ranked)  # no term hit, excluded
