# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Discover gateway bindings shipped as installed packages.

A third-party middleware driver ships as ``thingctx-<middleware>-gateway`` and
advertises a factory under the ``thingctx.gateways`` entry-point group. On
``pip install`` it becomes discoverable with no change to thingctx, exactly as a
south binding registers under ``thingctx.bindings``.

Entry-point contract: the factory takes the broker/endpoint string (and optional
keyword config) and returns a ``GatewayBinding`` instance.

    [project.entry-points."thingctx.gateways"]
    mqtt = "thingctx.gateways.builtin.mqtt:MqttGatewayBinding"
"""

from __future__ import annotations

from typing import Any

GATEWAY_GROUP = "thingctx.gateways"


def discover_gateway_bindings() -> dict[str, Any]:
    """Map each installed gateway binding's scheme/name to its factory. Broken or
    unimportable entry points are skipped, not raised, so one bad plugin cannot
    break discovery for the rest (same rule as south-binding discovery)."""
    from importlib.metadata import entry_points

    found: dict[str, Any] = {}
    # Typed Any to span two importlib.metadata shapes: the current keyword form
    # returns EntryPoints; the deprecated fallback returns a mapping whose get()
    # is typed differently. Both are only iterated below.
    eps: Any
    try:
        eps = entry_points(group=GATEWAY_GROUP)
    except TypeError:  # pragma: no cover - older importlib.metadata signature
        eps = entry_points().get(GATEWAY_GROUP, [])
    for ep in eps:
        try:
            found[ep.name] = ep.load()
        except Exception:  # noqa: S112, PERF203  isolate a broken third-party plugin; one bad entry point must not sink discovery
            continue
    return found


def make_gateway_binding(name: str, endpoint: str, **config: Any) -> Any:
    """Build a discovered gateway binding by name. Raises ``KeyError`` if no
    installed package advertises that name under ``thingctx.gateways``."""
    factories = discover_gateway_bindings()
    if name not in factories:
        raise KeyError(f"no gateway binding {name!r} installed; available: {sorted(factories)}")
    return factories[name](endpoint, **config)
