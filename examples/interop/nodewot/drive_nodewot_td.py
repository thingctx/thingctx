"""Drive a node-wot Thing with thingctx, using only the TD that node-wot (the
W3C WoT reference implementation) serves.

node-wot is the producer (see producer.js); thingctx is the consumer: it fetches
the served TD and turns it into callable properties/actions + LLM tool specs. No
node-wot client, no SDK, no MCP server.

Run (with producer.js running on :8080):

    python drive_nodewot_td.py
"""

from __future__ import annotations

import asyncio
import os

import httpx

from thingctx.invokers import HttpInvoker
from thingctx.runtime import ThingClient

TD_URL = os.environ.get("NODEWOT_TD_URL", "http://localhost:8080/counter")


async def main() -> None:
    async with httpx.AsyncClient() as http:
        td = (await http.get(TD_URL)).json()
    print(f"counter TD: id={td.get('id')}  title={td.get('title')}")

    client = ThingClient(tds=[td], invokers=[HttpInvoker()])
    print("actions exposed as tools:", [t["function"]["name"] for t in client.list_actions()])
    print("properties:", client.list_properties())

    # Address by the Thing's slug, which thingctx derives from the TD id.
    # node-wot defaults to a random urn:uuid id, so derive it rather than
    # hard-coding -- the demo then works for any producer.
    slug = client.list_properties()[0].split(".", 1)[0]

    before = await client.read_property(f"{slug}.count")
    print(f"\nread  count -> {before}")

    await client.invoke(f"{slug}.increment", {})
    print("invoke increment")

    after = await client.read_property(f"{slug}.count")
    print(f"read  count -> {after}")

    assert after == before + 1, f"expected {before + 1}, got {after}"
    print("\nOK: thingctx drove a node-wot Thing using only its served TD.")


if __name__ == "__main__":
    asyncio.run(main())
