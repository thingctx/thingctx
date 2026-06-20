# Ditto-generated TD → thingctx

A small interop demo: **Eclipse Ditto produces a W3C Thing Description, and
thingctx consumes it** — turning a real digital twin into callable
properties/actions and LLM tool specs, with no Ditto-specific code, no SDK, and
no MCP server. Just the description.

A conformance check: it proves thingctx consumes a real, machine-generated TD
from a standards-compliant producer, not only hand-written ones.

## What it shows

```
Thing Model (WoT)  ──put──►  Eclipse Ditto  ──Accept: application/td+json──►  TD
                                                                              │
                                                              thingctx.ThingClient(tds=[td])
                                                                              │
                                       read/write twin state · LLM tool specs ▼
```

Ditto is the **producer**: hand it a Thing Model URL, it stores a twin and, on
request, emits a conformant TD whose `forms` describe how to reach that twin
over its HTTP API (hrefs, methods, `basic` security). thingctx is the
**consumer**: it reads that TD and exposes the twin. They never knew about each
other — the TD is the only contract.

## Files

- `ditto-generated-td.json` — the TD **Ditto generated** (the fixture). Note
  `base`, the `basic_sc` security scheme, the `attributes/*` property forms with
  `htv:methodName`, and the `inbox/messages/*` action forms.
- `drive_ditto_td.py` — consumes that TD with thingctx and drives the live twin.
- `capture_td.sh` — reproduces the fixture from scratch (Docker → Ditto → TD).

## Result (live run)

thingctx, given only the generated TD, projected the twin to tools and
round-tripped a property straight through to Ditto:

```
actions exposed as tools: ['lamp-1.toggle', 'lamp-1.switch-on-for-duration']
properties: ['lamp-1.on', 'lamp-1.color', 'lamp-1.dimmer-level']

read  dimmer-level -> 0.0
write dimmer-level <- 0.42
read  dimmer-level -> 0.42

OK: thingctx drove a Ditto twin using only the generated TD.
```

Confirmed independently against Ditto's API — the twin's
`attributes.dimmer-level` became `0.42`. The property read/write path is fully
server-side (twin state), so it works without a physical device attached. The
`toggle`/`switch-on-for-duration` actions map to Ditto *messages*
(`inbox/messages/*`) and would need a device consuming them to complete.

## Reproduce

```bash
# 1. Bring up Ditto and capture the TD (needs Docker)
./capture_td.sh

# 2. Drive the twin with thingctx
python3 -m venv .venv && . .venv/bin/activate
pip install "thingctx[http]==0.1.3"
python drive_ditto_td.py

# 3. Tear down
( cd .ditto-src/deployment/docker && docker compose down -v )
```

Credentials default to Ditto's compose defaults (`ditto:ditto`); override with
`DITTO_CREDS=user:pass`.

## Takeaway

Ditto (and Bosch IoT Things behind it) is a credible, standards-compliant TD
producer for industrial digital twins. That a twin it generated drops straight
into thingctx with zero glue is the point: **any conformant TD producer →
thingctx**. thingctx is a consumer, complementary to Ditto, td-registry, and
thingdir — not a competitor to any of them.
