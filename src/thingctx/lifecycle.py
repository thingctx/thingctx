# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Async action lifecycle: a small handle for a long-running invocation.

WoT TD 1.1 models a long-running action with ``synchronous: false`` and the
``queryaction`` / ``cancelaction`` form operations. ``invoke`` returns an
``ActionStatus`` handle for such an action; the runtime polls it with
``query_action`` and stops it with ``cancel_action``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TERMINAL = ("completed", "failed", "cancelled")


@dataclass
class ActionStatus:
    """The state of a long-running action invocation. ``href`` is the status
    resource the transport polls; ``form`` is the affordance form (so the
    runtime can resolve the binding to poll/cancel with); ``thing_id`` is the
    owning Thing, carried so a poll/cancel authenticates as that Thing (the form
    alone does not name its owner)."""

    status: str = "running"
    output: Any = None
    error: Any = None
    href: str | None = None
    form: Any = field(default=None, repr=False)
    thing_id: str | None = None
    # The affordance name, carried so a later poll/cancel authorizes the
    # queryaction/cancelaction op on the right action (thing_id names the owner
    # but not which affordance).
    name: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "href": self.href,
        }


def status_from_body(
    body: Any,
    form: Any,
    href: str | None = None,
    thing_id: str | None = None,
    name: str | None = None,
) -> ActionStatus:
    """Map a transport's status payload to an ``ActionStatus``. A non-dict body
    is treated as the completed output of a synchronous-style response.
    ``thing_id`` names the owning Thing so a later poll/cancel re-authenticates
    as it; ``name`` names the affordance so the same poll/cancel authorizes the
    right action."""
    if not isinstance(body, dict):
        return ActionStatus(
            status="completed", output=body, href=href, form=form, thing_id=thing_id, name=name
        )
    return ActionStatus(
        status=body.get("status", "completed"),
        output=body.get("output"),
        error=body.get("error"),
        href=body.get("href", href),
        form=form,
        thing_id=thing_id,
        name=name,
    )
