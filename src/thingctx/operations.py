# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""One operation on a Thing, as the runtime ran it.

The runtime hands an ``Operation`` to a client's ``record`` callable for every read,
write, invoke, query and cancel, whether the gate let it run or refused it. A record
model turns it into its own records (``thingctx.models.sosa`` writes SOSA). This
module imports nothing, so a model and the runtime share it without sharing each
other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class Operation:
    """What one operation was, who asked, what the gate said, and how it went.

    ``op`` is the Thing Description form operation: ``readproperty``,
    ``writeproperty``, ``invokeaction``, ``queryaction`` or ``cancelaction``.
    ``permitted`` is False when a gate refused it, with the gate's ``reason``; a
    permitted call carries the reason the gate gave for allowing it, if any.
    ``arguments`` is the payload sent, after the Thing Description's ``const`` and
    ``default`` values are filled (None for a read). ``result`` is what the
    transport returned (an ``ActionStatus`` for a long running action) and ``error``
    the text of the exception when it raised instead. ``href`` is the form the call
    used. ``thing_version`` is the Thing Description's ``version.instance``.
    """

    thing_id: str
    affordance: str
    op: str
    permitted: bool
    reason: str | None
    identity: Any
    arguments: Any
    result: Any
    error: str | None
    started: datetime
    ended: datetime
    href: str | None = None
    thing_version: str | None = None


# A record callable. Sync or async; the runtime awaits an awaitable return.
Recorder = Callable[[Operation], "Awaitable[None] | None"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
