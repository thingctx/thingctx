# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The grant vocabulary, derived from the TD.

The grantable ``(thing_id, affordance, op)`` tuples for a Thing are read off each
affordance's forms and their ``op`` arrays; nothing is invented. That is what
makes the vocabulary TD-closed: you cannot grant an op the document never
declares.

Two WoT rules matter. A form that omits ``op`` implies a per-kind default
(property: read + write, action: invoke, event: subscribe + unsubscribe); the
builder must apply it, or a lenient TD (common in the wild) yields an empty
universe and denies everything. A form that lists ``op`` explicitly is a
restriction: ``["readproperty"]`` is read-only, so ``writeproperty`` is not in its
universe.

thingctx's parser leaves ``WoTForm.op`` raw (no defaulting) and derives
``readable/writable/observable`` from ``readOnly``/``writeOnly`` plus the form
ops. This builder filters the property default-op by ``prop.readable`` /
``prop.writable``, so ``readOnly: true`` never yields a ``writeproperty`` grant.
"""

from __future__ import annotations

from collections.abc import Iterable

from thingctx.thing import WoTThing

# A plain tuple so it is hashable and set membership is O(1): a grant is valid iff
# it is in the vocabulary set.
GrantTuple = tuple[str, str, str]

# What a form with no ``op`` implies, per affordance kind (W3C TD 1.1 defaults).
DEFAULT_PROPERTY_OPS: tuple[str, ...] = ("readproperty", "writeproperty")
DEFAULT_ACTION_OPS: tuple[str, ...] = ("invokeaction",)
DEFAULT_EVENT_OPS: tuple[str, ...] = ("subscribeevent", "unsubscribeevent")

# Every op a kind can carry; used to reject a stray token no such affordance
# could legitimately declare.
_PROPERTY_OPS = frozenset({"readproperty", "writeproperty", "observeproperty", "unobserveproperty"})
_ACTION_OPS = frozenset({"invokeaction", "queryaction", "cancelaction"})
_EVENT_OPS = frozenset({"subscribeevent", "unsubscribeevent"})


def _form_ops(declared: Iterable[str], default: tuple[str, ...]) -> set[str]:
    """The ops one form offers: its explicit ``op`` list (a restriction, returned
    verbatim), or the WoT default when the list is absent or empty."""
    declared = list(declared)
    return set(declared) if declared else set(default)


def build_vocabulary(things: WoTThing | Iterable[WoTThing]) -> set[GrantTuple]:
    """Compute the closed set of grantable ``(thing_id, affordance, op)`` tuples.

    Walks every affordance, unions each form's ops (applying the WoT default-op
    rule when a form has no ``op``), and returns the set. Nothing outside it can be
    granted, whatever a policy names.

    Property ops are gated by the property's ``readable`` / ``writable`` flags, so
    ``readOnly: true`` never yields a ``writeproperty`` grant even when a form
    defaults to read+write.
    """
    if isinstance(things, WoTThing):
        things = [things]

    universe: set[GrantTuple] = set()
    for thing in things:
        tid = thing.id

        for prop in thing.properties.values():
            for form in prop.forms:
                for op in _form_ops(form.op, DEFAULT_PROPERTY_OPS):
                    if op not in _PROPERTY_OPS:
                        continue  # a stray/unknown token is not a property op
                    # Capability gate: readOnly/writeOnly in the TD wins over a
                    # form default, matching WoTProperty.readable/writable.
                    if op == "readproperty" and not prop.readable:
                        continue
                    if op == "writeproperty" and not prop.writable:
                        continue
                    universe.add((tid, prop.name, op))

        for action in thing.actions.values():
            for form in action.forms:
                for op in _form_ops(form.op, DEFAULT_ACTION_OPS):
                    if op in _ACTION_OPS:
                        universe.add((tid, action.name, op))

        for event in thing.events.values():
            for form in event.forms:
                for op in _form_ops(form.op, DEFAULT_EVENT_OPS):
                    if op in _EVENT_OPS:
                        universe.add((tid, event.name, op))

    return universe
