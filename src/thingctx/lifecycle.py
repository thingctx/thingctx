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
    runtime can resolve the binding to poll/cancel with)."""

    status: str = "running"
    output: Any = None
    error: Any = None
    href: str | None = None
    form: Any = field(default=None, repr=False)

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


def status_from_body(body: Any, form: Any, href: str | None = None) -> ActionStatus:
    """Map a transport's status payload to an ``ActionStatus``. A non-dict body
    is treated as the completed output of a synchronous-style response."""
    if not isinstance(body, dict):
        return ActionStatus(status="completed", output=body, href=href, form=form)
    return ActionStatus(
        status=body.get("status", "completed"),
        output=body.get("output"),
        error=body.get("error"),
        href=body.get("href", href),
        form=form,
    )
