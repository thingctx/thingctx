# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Serve a WoT fleet over a middleware.

thingctx has two sides. A :class:`~thingctx.ProtocolBinding` drives a device:
thingctx speaks one transport outbound to reach a real Thing. A gateway is the
mirror: it re-serves a fleet of Things onto a middleware (an MQTT bus, MCP, DDS)
so any consumer on that middleware drives the fleet uniformly.

A :class:`~thingctx.gateways.engine.Gateway` joins the two: it holds a
``ThingClient`` to reach the devices, and a
:class:`~thingctx.gateways.engine.GatewayBinding` to serve them on the bus. A
driver declares what its transport can do by implementing optional capability
protocols; the engine calls only what a driver advertises (see the engine module
for the full model).

New gateway bindings ship as ``pip install``-able packages and register through
the ``thingctx.gateways`` entry-point group (see :func:`discover_gateway_bindings`),
exactly as consumer bindings register through ``thingctx.bindings``.
"""

from __future__ import annotations

from typing import Any

from thingctx.gateways.engine import (
    INVOKE,
    OBSERVE,
    READ,
    SUBSCRIBE,
    WRITE,
    Announces,
    EventMirroring,
    Gateway,
    GatewayBinding,
    PubSubOnly,
    QoSAware,
    RequestReply,
    ServeRequest,
)
from thingctx.gateways.registry import discover_gateway_bindings

__all__ = [
    "INVOKE",
    "OBSERVE",
    # the neutral operation names
    "READ",
    "SUBSCRIBE",
    "WRITE",
    "Announces",
    "EventMirroring",
    "Gateway",
    "GatewayBinding",
    "PubSubOnly",
    "QoSAware",
    # capability protocols (a driver opts in by implementing them)
    "RequestReply",
    "ServeRequest",
    # discovery
    "discover_gateway_bindings",
]


def __getattr__(name: str) -> Any:
    # The reference drivers pull an extra (paho for MQTT, mcp for MCP); load them
    # lazily so importing thingctx.gateways stays dependency-free.
    if name == "MqttGatewayBinding":
        from thingctx.gateways.builtin.mqtt import MqttGatewayBinding  # noqa: PLC0415

        return MqttGatewayBinding
    if name == "McpGatewayBinding":
        from thingctx.gateways.builtin.mcp import McpGatewayBinding  # noqa: PLC0415

        return McpGatewayBinding
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
