# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The built-in bindings thingctx ships: http, mqtt, media, and local.

Each is an implementation of the :class:`~thingctx.bindings.base.ProtocolBinding`
contract, privileged only by being bundled. An adopter can replace any of
them or add a new protocol by registering their own binding; nothing here is
reached except through :class:`~thingctx.bindings.registry.BindingRegistry`.
Each could live in its own ``thingctx-<protocol>`` distribution without the
runtime noticing.
"""

from __future__ import annotations

from thingctx.bindings.builtin.exec import ExecBinding
from thingctx.bindings.builtin.http import HttpBinding
from thingctx.bindings.builtin.local import LocalBinding
from thingctx.bindings.builtin.media import MediaBinding
from thingctx.bindings.builtin.mqtt import MqttBinding

__all__ = ["HttpBinding", "LocalBinding", "MqttBinding", "MediaBinding", "ExecBinding"]
