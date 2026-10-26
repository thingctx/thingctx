# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""In process Things bound with register_thing keep their event subscribers
apart, even when their events share a name."""

from __future__ import annotations

import asyncio

import pytest

from thingctx import ThingClient
from thingctx.bindings import LocalBinding


def _thermostat(slug: str) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": f"urn:demo:{slug}",
        "title": slug,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "events": {"reading": {"data": {"type": "number"}, "forms": [{"href": "local://reading"}]}},
    }


async def test_each_thing_hears_only_its_own_emits() -> None:
    one, two = LocalBinding(), LocalBinding()
    local = LocalBinding()
    local.register_thing("thermo1", one)
    local.register_thing("thermo2", two)
    client = ThingClient(tds=[_thermostat("thermo1"), _thermostat("thermo2")], bindings=[local])
    first = await client.subscribe("thermo1__reading")
    second = await client.subscribe("thermo2__reading")
    one.emit("reading", 21.5)
    two.emit("reading", 18.0)
    assert await asyncio.wait_for(anext(first), 1) == 21.5
    assert await asyncio.wait_for(anext(second), 1) == 18.0
    one.emit("reading", 22.0)
    assert await asyncio.wait_for(anext(first), 1) == 22.0
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(anext(second), 0.1)
