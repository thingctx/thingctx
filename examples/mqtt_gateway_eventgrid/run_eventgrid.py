#!/usr/bin/env python3
# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Live test harness: drive a fleet through MqttGateway on Azure Event Grid's
MQTT broker.

This is a RUNNABLE SCRIPT, not a pytest. It stands up an in-process demo Thing (a
pump with a read+write property and an action) behind a LocalBinding, puts it on
the Event Grid MQTT broker with an ``MqttGateway``, then acts as a plain consumer
with a stock ``MqttBinding`` pointed at the SAME broker and drives the Thing over
the bus, asserting the round-trip.

It configures the two hard parts of an Event Grid connection for you:

* MQTT v5 over TLS on port 8883,
* an X.509 client certificate (``tls_set(certfile=, keyfile=)``), and
* a ``client_id`` that EQUALS the registered client authentication name (Event
  Grid rejects a mismatch).

Everything real (hostname, client ids, cert/key paths) is passed at RUN TIME via
flags or environment variables. Nothing real is embedded, and no secret is read
or printed. See ``AZURE_EVENTGRID_RUNBOOK.md`` for the broker-side setup.

Dry run (no network, smoke-testable offline)::

    python examples/mqtt_gateway_eventgrid/run_eventgrid.py --dry-run

Live run (against a real Event Grid namespace on a PERSONAL subscription)::

    python examples/mqtt_gateway_eventgrid/run_eventgrid.py \
        --host <ns>.<region>-1.ts.eventgrid.azure.net \
        --gateway-client-id gateway-01 \
        --consumer-client-id consumer-01 \
        --cert ./certs/client.pem \
        --key  ./certs/client.key
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# This harness lives inside the mqtt-gateway worktree. The installed thingctx in
# the active venv may be an editable checkout of a DIFFERENT worktree that lacks
# the gateway, so import thingctx from THIS tree's src/ first. Harmless when the
# installed copy already has the gateway (this src/ simply wins).
_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from thingctx import LocalBinding, ThingClient, parse_thing  # noqa: E402
from thingctx.bindings import MqttBinding  # noqa: E402
from thingctx.integrations.mqtt_gateway import MqttGateway  # noqa: E402

EVENT_GRID_PORT = 8883  # Event Grid MQTT is TLS-only on 8883; there is no 1883.
THING_ID = "urn:demo:pump:v1"

# The demo Thing. A LocalBinding routes its forms to the in-process Pump below.
# The gateway re-projects these onto mqtt:// topics; the consumer never sees this
# native (local://) face.
DEMO_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": THING_ID,
    "title": "Demo Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "properties": {
        "target_rpm": {
            "type": "integer",
            "description": "The pump's target speed. Readable and writable.",
            "forms": [{"href": "local://target_rpm", "op": ["readproperty", "writeproperty"]}],
        }
    },
    "actions": {
        "set_speed": {
            "description": "Set the pump speed and report the new state.",
            "input": {
                "type": "object",
                "properties": {"rpm": {"type": "integer"}},
                "required": ["rpm"],
            },
            "forms": [{"href": "local://set_speed", "op": ["invokeaction"]}],
        }
    },
}


class Pump:
    """The in-process device the LocalBinding drives. ``get_``/``set_`` map to
    the property read/write; ``set_speed`` is the action."""

    def __init__(self) -> None:
        self._target_rpm = 1200

    def get_target_rpm(self) -> int:
        return self._target_rpm

    def set_target_rpm(self, value: int) -> dict:
        self._target_rpm = int(value)
        return {"ok": True, "target_rpm": self._target_rpm}

    def set_speed(self, rpm: int) -> dict:
        self._target_rpm = int(rpm)
        return {"ok": True, "target_rpm": self._target_rpm}


class EventGridGateway(MqttGateway):
    """MqttGateway that configures its paho client for Event Grid: a client_id
    equal to the registered auth name, TLS with system roots, and an X.509 client
    certificate. The base class calls this hook after building the paho client and
    before ``connect``, which is exactly where these must be set."""

    def __init__(self, *args, client_id: str, certfile: str, keyfile: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._eg_client_id = client_id
        self._eg_certfile = certfile
        self._eg_keyfile = keyfile

    def _configure_hook(self, paho) -> None:  # noqa: ANN001
        # Event Grid requires the MQTT client_id to equal the registered client
        # authentication name. paho fixes client_id at construction; reinitialise
        # rewrites it on the existing MQTTv5 client without a clean_session arg.
        paho.reinitialise(client_id=self._eg_client_id)
        # reinitialise resets the callbacks the base class already wired; restore
        # the on_message handler so inbound bus messages still reach the gateway.
        paho.on_message = self._on_message
        # TLS to the broker (system CA roots) + mutual TLS with the client cert.
        paho.tls_set(certfile=self._eg_certfile, keyfile=self._eg_keyfile)


def _tls_client_factory(client_id: str, certfile: str, keyfile: str):
    """A client_factory for the consumer's MqttBinding: a paho MQTTv5 client
    carrying the same TLS + client-cert setup, with its OWN client_id (a separate
    registered Event Grid client from the gateway, so both can connect at once)."""

    def _factory():
        import paho.mqtt.client as mqtt

        version = getattr(mqtt, "CallbackAPIVersion", None)
        args = (version.VERSION1,) if version is not None else ()
        client = mqtt.Client(*args, client_id=client_id, protocol=mqtt.MQTTv5)
        client.tls_set(certfile=certfile, keyfile=keyfile)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        return client

    return _factory


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_eventgrid.py",
        description="Live-test MqttGateway against Azure Event Grid's MQTT broker.",
    )
    # Every real value is a flag with an env-var fallback; none has a default that
    # could silently point at a real endpoint. Fail-closed is enforced below.
    p.add_argument(
        "--host",
        default=os.environ.get("EG_MQTT_HOST"),
        help="Event Grid namespace MQTT hostname (env EG_MQTT_HOST).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("EG_MQTT_PORT", EVENT_GRID_PORT)),
        help=f"MQTT TLS port (env EG_MQTT_PORT, default {EVENT_GRID_PORT}).",
    )
    p.add_argument(
        "--gateway-client-id",
        default=os.environ.get("EG_GATEWAY_CLIENT_ID"),
        help="Registered client auth name for the GATEWAY connection (env EG_GATEWAY_CLIENT_ID).",
    )
    p.add_argument(
        "--consumer-client-id",
        default=os.environ.get("EG_CONSUMER_CLIENT_ID"),
        help="Registered client auth name for the CONSUMER connection (env EG_CONSUMER_CLIENT_ID).",
    )
    p.add_argument(
        "--cert",
        default=os.environ.get("EG_CLIENT_CERT"),
        help="Path to the X.509 client certificate PEM (env EG_CLIENT_CERT).",
    )
    p.add_argument(
        "--key",
        default=os.environ.get("EG_CLIENT_KEY"),
        help="Path to the client private key PEM (env EG_CLIENT_KEY).",
    )
    p.add_argument(
        "--prefix",
        default=os.environ.get("EG_TOPIC_PREFIX", "tc"),
        help="Topic prefix; must match the Event Grid topic space (env "
        "EG_TOPIC_PREFIX, default tc).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Per-operation round-trip timeout in seconds (default 15).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the gateway + consumer and print the plan without "
        "connecting. No network, no cert files required.",
    )
    return p.parse_args(argv)


def _require(args: argparse.Namespace) -> list[str]:
    """Return the list of missing required inputs (fail-closed, no real default)."""
    missing = []
    if not args.host:
        missing.append("--host / EG_MQTT_HOST")
    if not args.gateway_client_id:
        missing.append("--gateway-client-id / EG_GATEWAY_CLIENT_ID")
    if not args.consumer_client_id:
        missing.append("--consumer-client-id / EG_CONSUMER_CLIENT_ID")
    if not args.cert:
        missing.append("--cert / EG_CLIENT_CERT")
    if not args.key:
        missing.append("--key / EG_CLIENT_KEY")
    return missing


def _build_gateway(args: argparse.Namespace, *, live: bool) -> EventGridGateway:
    """Construct the fleet's native ThingClient and the Event Grid gateway over
    it. Identical for dry-run and live; only ``start`` differs."""
    broker = f"{args.host or 'DRY-RUN-NO-HOST'}:{args.port}"
    native = ThingClient(tds=[DEMO_TD], bindings=[LocalBinding(Pump())])
    # For dry-run, cert/key may be absent; pass through whatever we have so the
    # object builds. start() is never called on the dry-run path, so no file is
    # ever opened.
    return EventGridGateway(
        native,
        broker=broker,
        prefix=args.prefix,
        client_id=args.gateway_client_id or "DRY-RUN-GATEWAY",
        certfile=args.cert or "",
        keyfile=args.key or "",
    )


def _build_consumer(args: argparse.Namespace, projected_td: dict) -> ThingClient:
    """A stock consumer: a ThingClient over the gateway's PROJECTED (mqtt-faced)
    TD, driven by a stock MqttBinding whose paho client carries the consumer's own
    TLS + client cert. This is exactly what any third party on the bus would do."""
    binding = MqttBinding(
        timeout=args.timeout,
        connect_timeout=args.timeout,
        client_id=args.consumer_client_id or "DRY-RUN-CONSUMER",
        client_factory=_tls_client_factory(
            args.consumer_client_id or "DRY-RUN-CONSUMER",
            args.cert or "",
            args.key or "",
        ),
    )
    return ThingClient(tds=[projected_td], bindings=[binding])


def _dry_projected(args: argparse.Namespace) -> dict:
    """Project the mqtt-faced TD offline, exactly as the gateway's start() would,
    so both the plan print and the dry-run consumer use the real projected shape
    without any network."""
    from thingctx.integrations.mqtt_gateway import project_mqtt_td

    broker = f"{args.host or '<host>'}:{args.port}"
    return project_mqtt_td(parse_thing(DEMO_TD), broker=broker, prefix=args.prefix)


def _print_plan(args: argparse.Namespace) -> None:
    broker = f"{args.host or '<host>'}:{args.port}"
    projected = _dry_projected(args)
    print("=" * 68)
    print("PLAN (dry run) - nothing below connects to a network")
    print("=" * 68)
    print(f"  broker (host:port)   : {broker}")
    print("  protocol / transport : MQTT v5 over TLS (mutual TLS, X.509 client cert)")
    print(f"  topic prefix         : {args.prefix}")
    print(f"  gateway client_id    : {args.gateway_client_id or '<gateway-client-id>'}")
    print(f"  consumer client_id   : {args.consumer_client_id or '<consumer-client-id>'}")
    print(f"  client cert (path)   : {args.cert or '<cert path>'}")
    print(f"  client key  (path)   : {args.key or '<key path>'}")
    print()
    print("  Fleet on the bus (projected mqtt-faced TD):")
    for name, adef in projected.get("actions", {}).items():
        print(f"    action   {name:<12} -> {adef['forms'][0]['href']}")
    for name, pdef in projected.get("properties", {}).items():
        print(f"    property {name:<12} -> {pdef['forms'][0]['href']}  op={pdef['forms'][0]['op']}")
    print()
    print("  Round-trip the live run would assert:")
    print("    1. write  target_rpm = 1800  through the bus")
    print("    2. read   target_rpm         -> expect 1800")
    print("    3. invoke set_speed rpm=2400 -> expect target_rpm == 2400")
    print()
    print("  Event Grid requirements (see AZURE_EVENTGRID_RUNBOOK.md):")
    print(f"    - a topic space covering '{args.prefix}/#'")
    print("    - a permission binding granting publisher+subscriber on it")
    print("    - two registered clients (auth names == the two client_ids above),")
    print("      each bound to the client certificate's thumbprint/CA")
    print("=" * 68)
    print("DRY RUN OK: gateway + consumer built, plan printed, no network touched.")


async def _run_live(args: argparse.Namespace) -> int:
    gateway = _build_gateway(args, live=True)
    consumer = None
    try:
        print(
            f"Connecting gateway (client_id={args.gateway_client_id}) to "
            f"{args.host}:{args.port} over TLS ..."
        )
        await gateway.start(host=args.host, port=args.port)
        # The projected TD the gateway now serves; the consumer drives THIS.
        slug = next(iter(gateway.projected_tds))
        projected_td = gateway.projected_tds[slug]
        print(f"Gateway serving fleet on prefix '{args.prefix}'. Projected slug: {slug}")

        consumer = _build_consumer(args, projected_td)

        print("[1/3] write  target_rpm = 1800 through the bus ...")
        await consumer.write_property("pump.target_rpm", 1800)

        print("[2/3] read   target_rpm through the bus ...")
        got = await consumer.read_property("pump.target_rpm")
        got_rpm = got.get("target_rpm") if isinstance(got, dict) else got
        assert got_rpm == 1800, f"read-after-write mismatch: {got!r}"

        print("[3/3] invoke set_speed rpm=2400 through the bus ...")
        res = await consumer.invoke("pump.set_speed", {"rpm": 2400})
        assert isinstance(res, dict) and res.get("target_rpm") == 2400, f"action result: {res!r}"

        print()
        print("PASS: write/read/invoke round-tripped through Event Grid.")
        return 0
    except Exception as exc:  # noqa: BLE001 - a harness reports, it does not traceback-dump
        print()
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if consumer is not None:
            await consumer.aclose()
        await gateway.aclose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    if args.dry_run:
        # Build the gateway + consumer to prove they construct, then print the
        # plan. Neither object opens a socket or a file until start()/invoke().
        _build_gateway(args, live=False)
        _build_consumer(args, _dry_projected(args))
        _print_plan(args)
        return 0

    missing = _require(args)
    if missing:
        # Fail closed: never fall back to a real endpoint or a bundled secret.
        print(
            "ERROR: missing required inputs (nothing connects until these are set):",
            file=sys.stderr,
        )
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print("\nRe-run with --dry-run to smoke-test the plan without them.", file=sys.stderr)
        return 2

    for label, path in (("--cert", args.cert), ("--key", args.key)):
        if not Path(path).is_file():
            print(f"ERROR: {label} path does not exist: {path}", file=sys.stderr)
            return 2

    return asyncio.run(_run_live(args))


if __name__ == "__main__":
    raise SystemExit(main())
