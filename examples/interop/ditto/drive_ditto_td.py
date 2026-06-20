"""Drive an Eclipse Ditto digital twin with thingctx, using only the TD that
Ditto generated from a W3C WoT Thing Model.

Ditto is the *producer*: you hand it a Thing Model URL, it stores a twin and,
on request, emits a conformant W3C TD describing how to talk to that twin over
its HTTP API (forms, methods, security). thingctx is the *consumer*: it reads
that TD and turns it into callable properties/actions + LLM tool specs. No
glue code, no Ditto-specific SDK, no MCP server -- just the description.

Run (with Ditto up on :8080 and the TD captured to ditto-generated-td.json):

    python drive_ditto_td.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib

from thingctx.invokers import HttpInvoker
from thingctx.runtime import ThingClient

TD_PATH = pathlib.Path(__file__).with_name("ditto-generated-td.json")
# Ditto's default docker-compose stack: nginx basic auth, user:pass = ditto:ditto.
CREDS = os.environ.get("DITTO_CREDS", "ditto:ditto")


async def main() -> None:
    td = json.loads(TD_PATH.read_text())
    print(f"Ditto-generated TD: id={td['id']}  base={td['base']}")
    print(f"  security={td['security']}  scheme={td['securityDefinitions']}")

    # The TD names the security scheme ("basic_sc"); we supply the secret.
    http = HttpInvoker(credentials={"basic_sc": CREDS})
    client = ThingClient(tds=[td], invokers=[http])

    # 1. The Ditto TD -> LLM tool specs, for free.
    print("\nactions exposed as tools:", [t["function"]["name"] for t in client.list_actions()])
    print("properties:", client.list_properties())

    # 2. Read -> write -> read a property, straight through to the live twin.
    before = await client.read_property("lamp-1.dimmer-level")
    print(f"\nread  dimmer-level -> {before}")

    new_value = 0.42
    await client.write_property("lamp-1.dimmer-level", new_value)
    print(f"write dimmer-level <- {new_value}")

    after = await client.read_property("lamp-1.dimmer-level")
    print(f"read  dimmer-level -> {after}")

    assert after == new_value, f"expected {new_value}, got {after}"
    print("\nOK: thingctx drove a Ditto twin using only the generated TD.")


if __name__ == "__main__":
    asyncio.run(main())
