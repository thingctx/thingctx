# node-wot-produced TD → thingctx

node-wot (the W3C WoT reference implementation) produces a Thing Description;
thingctx consumes it and drives the Thing using only that TD — no node-wot
client, SDK, or MCP server.

A conformance check (same shape as [`../ditto`](../ditto)): thingctx consumes
the reference implementation's own TD, with no node-wot client in the loop.

## What it shows

```
node-wot Servient ──exposes──► http://localhost:8080/counter (its TD)
                                           │
                            thingctx.from_url(...) / ThingClient(tds=[td])
                                           │
                    read count · invokeAction increment · read count ▼
```

node-wot is the producer (a Servient exposing a `counter` Thing and its TD at
`/counter`); thingctx is the consumer (fetches the TD, turns its
properties/actions into callable methods and LLM tool specs). The TD is the only
contract.

## Files

- `producer.js` — minimal node-wot Servient exposing the `counter` Thing.
- `drive_nodewot_td.py` — consumes the served TD with thingctx and drives it.

## Reproduce

```bash
# 1. Start the node-wot producer (needs Node 18+)
npm init -y && npm install @node-wot/core @node-wot/binding-http
node producer.js          # serves http://localhost:8080/counter

# 2. In another shell, drive it with thingctx
python3 -m venv .venv && . .venv/bin/activate
pip install "thingctx[http]"
python drive_nodewot_td.py
```

## Result (verified live)

```
counter TD: id=urn:dev:counter  title=counter
actions exposed as tools: ['counter.increment', 'counter.decrement', 'counter.reset']
properties: ['counter.count']

read  count -> 0
invoke increment
read  count -> 1
```

## Note: Thing id and slug

node-wot defaults a Thing's `id` to a random `urn:uuid:...`, and thingctx derives
the address prefix (slug) from that id. Either set a stable `id` in the producer
(this demo uses `urn:dev:counter`) or derive the prefix at runtime (the driver
does `client.list_properties()[0].split(".")[0]`), which works for any producer.
