# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Protocol bindings: thingctx's transport extension point.

A binding speaks one transport scheme and answers the runtime through the
:class:`ProtocolBinding` contract (a ``scheme`` plus an async ``invoke``, with
optional read / write / subscribe / media capabilities). http, mqtt, media, local,
and exec are the built-in bindings, shipped under :mod:`thingctx.bindings.builtin`;
each is only an implementation of the contract. Register your own binding to add
a protocol thingctx has never heard of, or to replace a built-in, with no fork.

    reg = BindingRegistry.default()   # http + local
    reg.register(OpcUaBinding())      # add a protocol
    client = ThingClient(tds=[...], bindings=reg)

The term follows W3C WoT: a TD ``form`` is the protocol binding for an
interaction; this is the client that executes it.
"""

from __future__ import annotations

from thingctx.bindings.base import (
    AsyncAction,
    AuthMixin,
    BulkProperties,
    Closeable,
    ContentRouted,
    MediaConsumer,
    MediaPublisher,
    ProtocolBinding,
    Readable,
    SecurityAware,
    Subscribable,
    Writable,
    binding_schemes,
    select_binding,
)
from thingctx.bindings.builtin.exec import ExecBinding
from thingctx.bindings.builtin.http import HttpBinding
from thingctx.bindings.builtin.local import LocalBinding
from thingctx.bindings.builtin.media import Frame, MediaBackend, MediaBinding, is_media_form
from thingctx.bindings.builtin.mqtt import MqttBinding
from thingctx.bindings.registry import (
    BUILTIN_BINDINGS,
    CONTRACT_VERSION,
    BindingRegistry,
    build_builtin,
    default_bindings,
    discover_bindings,
    discover_local_handlers,
)

__all__ = [
    "BUILTIN_BINDINGS",
    "CONTRACT_VERSION",
    "AsyncAction",
    # Auth helper shared by built-in and custom bindings
    "AuthMixin",
    # Registry + discovery
    "BindingRegistry",
    "BulkProperties",
    "Closeable",
    # Capabilities
    "ContentRouted",
    "ExecBinding",
    "Frame",
    # Built-in bindings
    "HttpBinding",
    "LocalBinding",
    # Media backend sub-contract (pluggable engines behind MediaBinding)
    "MediaBackend",
    "MediaBinding",
    "MediaConsumer",
    "MediaPublisher",
    "MqttBinding",
    # Contract
    "ProtocolBinding",
    "Readable",
    "SecurityAware",
    "Subscribable",
    "Writable",
    "binding_schemes",
    "build_builtin",
    "default_bindings",
    "discover_bindings",
    "discover_local_handlers",
    "is_media_form",
    "select_binding",
]
