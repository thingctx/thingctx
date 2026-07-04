# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Output quality: a reserved slot for a capability's verdict on its own output.

An action can return a 2xx with schema-valid output that the *capability* knows
is untrustworthy but the *caller* cannot tell apart: ASR repeating one line on
music or silence, OCR emitting the same row when a scan stalls, a scrape
returning N identical rows when a selector breaks, an LLM in a repeat loop. The
text is valid; it is also garbage.

thingctx reserves one output key, ``tc:quality``, for the capability to report
that verdict, the same move it already made for security (standardize WHERE
credentials go) and trust (standardize HOW approval is gated): it names the slot
and its shape, it does not invent the value.

thingctx does NOT compute the score. The judgment is irreducibly the
capability's: a generic "repetition is bad" rule would false-positive on
legitimately repetitive output and miss domain-specific failure modes. The
capability fills the slot; this module only defines its key and shape so any
consumer (the driver skill today, the chaining engine later) reads a verdict
uniformly.

Shape (only ``verdict`` is required)::

    "tc:quality": {
        "verdict": "ok" | "suspect" | "bad",
        "score": 0.0..1.0,            # optional, higher is better
        "reason": "human-readable why",
        "signals": { ... domain detail ... }
    }
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

# The reserved output key. A capability merges this into its action result.
QUALITY_KEY = "tc:quality"

Verdict = Literal["ok", "suspect", "bad"]

# Verdicts a consumer must not silently continue on: surface the reason and ask.
SUSPECT_VERDICTS: frozenset[str] = frozenset({"suspect", "bad"})


class Quality(TypedDict, total=False):
    """The ``tc:quality`` envelope. ``verdict`` is required; the rest optional."""

    verdict: Verdict
    score: float
    reason: str
    signals: dict[str, Any]


def make_quality(
    verdict: Verdict,
    *,
    score: float | None = None,
    reason: str | None = None,
    signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``{tc:quality: {...}}`` fragment a capability merges into its
    output, so adopters report the verdict in one shape. Only ``verdict`` is
    required; omit a field that was not measured."""
    q: dict[str, Any] = {"verdict": verdict}
    if score is not None:
        q["score"] = score
    if reason is not None:
        q["reason"] = reason
    if signals:
        q["signals"] = signals
    return {QUALITY_KEY: q}


def quality_of(output: Any) -> Quality | None:
    """The ``tc:quality`` envelope an action attached to its output, or None.

    Reads the reserved key (or its bare ``quality`` form) from a result mapping;
    returns None when absent or malformed (no ``verdict`` string), so a caller
    can tell "no signal" from an explicit ``ok``."""
    if not isinstance(output, dict):
        return None
    q = output.get(QUALITY_KEY)
    if q is None:
        q = output.get("quality")
    if not isinstance(q, dict) or not isinstance(q.get("verdict"), str):
        return None
    return q  # type: ignore[return-value]


def is_suspect(output: Any) -> bool:
    """True when the output carries a ``tc:quality`` verdict of ``suspect`` or
    ``bad`` , the signal a consumer should stop on, surface the reason, and ask
    before continuing or publishing."""
    q = quality_of(output)
    return q is not None and q.get("verdict") in SUSPECT_VERDICTS
