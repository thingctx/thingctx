# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The grant vocabulary, derived from the TD.

A grant is a set of ``(thing_id, affordance_name, op)`` tuples, and the universe
of *grantable* tuples for a Thing is not invented; it is read straight off each
affordance's forms and their ``op`` arrays. This module computes that universe.

The one subtlety is the **WoT default-op rule**. A form may omit ``op``; WoT then
implies a fixed default per affordance kind (a property form implies
``readproperty`` + ``writeproperty``, an action form ``invokeaction``, an event
form ``subscribeevent`` + ``unsubscribeevent``). The builder MUST apply that same
defaulting, or a lenient TD (very common in the wild) yields an empty universe
and every call is denied. It must also honor an EXPLICIT ``op`` array as a
*restriction*: a property form that lists only ``["readproperty"]`` is read-only,
and ``writeproperty`` is NOT in its universe even though a property "could" be
written in general. That is what makes the vocabulary TD-closed: you cannot grant
an operation the document does not declare.

thingctx's own parser (:func:`thingctx.thing.parse_thing`) leaves ``WoTForm.op``
as the raw declared tuple (no defaulting), and derives
``WoTProperty.readable/writable/observable`` from ``readOnly``/``writeOnly`` +
the union of form ops. This builder stays in lockstep with that: the property
default-op is filtered by ``prop.readable`` / ``prop.writable`` so an explicit
``readOnly: true`` never yields a ``writeproperty`` grant even under defaulting.
"""

from __future__ import annotations

from collections.abc import Iterable

from thingctx.thing import WoTThing

# A single grantable coordinate. Interned as a plain tuple so it is hashable and
# set membership is O(1): a grant "is valid" iff it is in the vocabulary set.
GrantTuple = tuple[str, str, str]

# The WoT 1.1 default-op rule: what a form with NO ``op`` array implies, per
# affordance kind. These mirror the W3C TD 1.1 spec defaults exactly.
DEFAULT_PROPERTY_OPS: tuple[str, ...] = ("readproperty", "writeproperty")
DEFAULT_ACTION_OPS: tuple[str, ...] = ("invokeaction",)
DEFAULT_EVENT_OPS: tuple[str, ...] = ("subscribeevent", "unsubscribeevent")

# The full closed op universe per affordance kind, used only to reject an op
# token that no affordance of that kind could ever legitimately carry.
_PROPERTY_OPS = frozenset({"readproperty", "writeproperty", "observeproperty", "unobserveproperty"})
_ACTION_OPS = frozenset({"invokeaction", "queryaction", "cancelaction"})
_EVENT_OPS = frozenset({"subscribeevent", "unsubscribeevent"})


def _form_ops(declared: Iterable[str], default: tuple[str, ...]) -> set[str]:
    """The ops one form actually offers: its explicit ``op`` list, or the WoT
    default when it declares none. An explicit list is a RESTRICTION, returned
    verbatim; only an absent/empty list falls back to the default."""
    declared = list(declared)
    return set(declared) if declared else set(default)


def build_vocabulary(things: WoTThing | Iterable[WoTThing]) -> set[GrantTuple]:
    """Compute the closed set of grantable ``(thing_id, affordance, op)`` tuples.

    Walks every affordance of every Thing, unions each form's ops (applying the
    WoT default-op rule for a form with no ``op``), and returns the set. A tuple
    is grantable iff it is in the returned set; nothing outside it can be granted
    or exercised, no matter what a policy names.

    For properties the per-form op set is intersected with the property's own
    ``readable`` / ``writable`` capability flags, so a TD that sets
    ``readOnly: true`` (which thingctx surfaces as ``writable=False``) can never
    yield a ``writeproperty`` grant even if a form defaults to read+write. This
    keeps the vocabulary in lockstep with thingctx's own capability derivation.
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
