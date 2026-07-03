"""In-process handlers for the binary: a local:// Thing becomes callable when
its handler is advertised through the thingctx.local_handlers entry point group,
and several in-process Things keep distinct handlers despite colliding names."""

from __future__ import annotations

import importlib.metadata

import pytest

from thingctx import LocalBinding, ThingClient
from thingctx.bindings import discover_local_handlers


def _td(slug: str, value: str) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": f"urn:demo:{slug}:v1",
        "title": slug,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {
            "status": {"idempotent": True, "forms": [{"href": "local://status"}]},
        },
        "_value": value,
    }


class _Dev:
    def __init__(self, value: str) -> None:
        self.value = value

    def status(self) -> dict:
        return {"who": self.value}


class _EP:
    """A stand-in importlib.metadata entry point: a name and a zero-arg
    loader, the only surface discover_local_handlers touches."""

    def __init__(self, name: str, factory) -> None:
        self.name = name
        self.group = "thingctx.local_handlers"
        self._factory = factory

    def load(self):
        return self._factory


@pytest.fixture
def fake_entry_points(monkeypatch):
    """Install a set of fake entry points for the local_handlers group."""

    def install(eps: list[_EP]) -> None:
        def fake(group=None):  # the modern entry_points(group=...) selection API
            return [ep for ep in eps if group is None or ep.group == group]

        monkeypatch.setattr(importlib.metadata, "entry_points", fake)

    return install


def test_discover_filters_to_requested_slugs(fake_entry_points):
    loaded: list[str] = []

    def factory(name):
        def make():
            loaded.append(name)
            return _Dev(name)

        return make

    fake_entry_points([_EP("pump", factory("pump")), _EP("fan", factory("fan"))])

    # Only the present slug is imported; the other handler is never loaded.
    handlers = discover_local_handlers({"pump"})
    assert set(handlers) == {"pump"}
    assert loaded == ["pump"]
    assert handlers["pump"].status() == {"who": "pump"}


@pytest.mark.asyncio
async def test_client_from_registry_binds_discovered_handler(fake_entry_points):
    from thingctx.integrations.mcp import client_from_registry

    fake_entry_points([_EP("pump", lambda: _Dev("pump"))])

    class _Reg:
        def fetch(self):
            return [_td("pump", "pump")]

    client = client_from_registry(_Reg())
    # The local:// action is reachable through the binary's client, with no
    # bridge shim and no TD edit.
    assert await client.invoke("pump.status") == {"who": "pump"}


@pytest.mark.asyncio
async def test_register_thing_isolates_colliding_action_names():
    binding = LocalBinding()
    binding.register_thing("pump", _Dev("pump"))
    binding.register_thing("fan", _Dev("fan"))

    client = ThingClient(tds=[_td("pump", "pump"), _td("fan", "fan")], bindings=[binding])
    # Same action name on both Things resolves to each Thing's own handler.
    assert await client.invoke("pump.status") == {"who": "pump"}
    assert await client.invoke("fan.status") == {"who": "fan"}
