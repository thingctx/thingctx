# Interoperability demos

thingctx is a *consumer*: point it at any conformant Thing Description and it
drives the Thing, whoever produced the TD. These demos prove that against real,
machine-generating producers — not hand-written fixtures.

- [`nodewot/`](nodewot/) — the **W3C WoT reference implementation** (node-wot)
  exposes a Thing; thingctx consumes its served TD and drives it.
- [`ditto/`](ditto/) — **Eclipse Ditto** generates a TD for a digital twin;
  thingctx consumes it and round-trips twin state.

Each demo stands alone: a producer (or a captured TD), a `drive_*.py` consumer,
and a README with reproduction steps and a verified run. The TD is the only
contract between producer and consumer.
