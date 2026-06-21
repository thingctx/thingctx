"""Invokers: one per transport scheme. ``invoke()`` picks the invoker whose
scheme matches the form's href.

* LocalInvoker: ``local://`` (or no scheme), an in-process callable.
* HttpInvoker:  ``http``/``https``, needs httpx.
* MqttInvoker:  ``mqtt``, publish + await a reply, needs paho-mqtt.

A new transport is one more invoker file; the resource description is unchanged.
Each invoker authenticates through the shared, transport-neutral auth layer, so
adding one carries no auth logic.
"""

from __future__ import annotations

from thingctx.invokers.base import Invoker, select_invoker
from thingctx.invokers.http import HttpInvoker
from thingctx.invokers.local import LocalInvoker
from thingctx.invokers.mqtt import MqttInvoker

__all__ = [
    "Invoker",
    "LocalInvoker",
    "HttpInvoker",
    "MqttInvoker",
    "select_invoker",
]
