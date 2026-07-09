# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Serve a WoT fleet over a middleware: the north side of a binding.

A binding has two directions. The SOUTH side (:class:`~thingctx.ProtocolBinding`,
formerly ``ProtocolBinding``) drives a device: thingctx speaks one transport
outbound to reach a real Thing. The NORTH side, here, is the mirror: it re-serves
a fleet of Things onto a middleware (an MQTT bus, a CoAP server, MCP, DDS) so any
consumer on that middleware drives the fleet uniformly.

A :class:`~thingctx.gateways.north.Gateway` joins the two: it holds a
``ThingClient`` (south, to reach devices) and a
:class:`~thingctx.gateways.north.GatewayBinding` (north, to serve the bus).

A north binding declares what its transport can do by IMPLEMENTING optional
capability protocols (``RequestReply``, ``EventMirroring``, ``PubSubOnly``,
``Announces``, ``QoSAware``); the engine calls only what a driver advertises, so a
protocol's specific features are used, never flattened. Protocol-specific options
ride in the projected TD form as namespaced vocabulary (``mqv:``/``covv:``/
``mcpv:``), which the engine passes through opaquely.

New north bindings ship as ``pip install``-able packages and register through the
``thingctx.gateways`` entry-point group (see :func:`discover_gateway_bindings`),
exactly as south bindings register through ``thingctx.bindings``.
"""

from __future__ import annotations

from thingctx.gateways.north import (
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
    "Gateway",
    "GatewayBinding",
    "ServeRequest",
    # capability protocols (a driver opts in by implementing them)
    "RequestReply",
    "EventMirroring",
    "PubSubOnly",
    "Announces",
    "QoSAware",
    # the neutral operation names
    "READ",
    "WRITE",
    "OBSERVE",
    "INVOKE",
    "SUBSCRIBE",
    # discovery
    "discover_gateway_bindings",
]


def __getattr__(name: str):
    # The MQTT reference driver needs paho (the ``mqtt`` extra); load it lazily so
    # importing thingctx.gateways stays dependency-free.
    if name == "MqttGatewayBinding":
        from thingctx.gateways.builtin.mqtt import MqttGatewayBinding

        return MqttGatewayBinding
    # The MCP driver needs the ``mcp`` extra; load it lazily too.
    if name == "McpGatewayBinding":
        from thingctx.gateways.builtin.mcp import McpGatewayBinding

        return McpGatewayBinding
    # The CoAP driver needs the ``coap`` extra (aiocoap); load it lazily too.
    if name == "CoapGatewayBinding":
        from thingctx.gateways.builtin.coap import CoapGatewayBinding

        return CoapGatewayBinding
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
