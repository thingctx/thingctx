# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""``@implements``: an opt-in, readable way to bind a class to one or more
thingctx contracts and fail loudly at import time if it does not satisfy them.

    from thingctx import ProtocolBinding, implements

    @implements(ProtocolBinding)
    class SimBinding:
        scheme = "sim"

        async def invoke(self, action, form, arguments): ...

The contract itself stays a :class:`typing.Protocol`, so static type checkers
verify signatures with no decorator and a third party never subclasses anything.
This decorator adds the one thing a Protocol does not: a definition-time presence
check, the early failure an abstract base class gives, without inheritance. It
checks that every contract member exists and that methods are callable; it does
not re-check behaviour. Pair it with the conformance kit in
:mod:`thingctx.testing` (``assert_binding_contract`` /
``assert_media_backend_contract``) for async shape, generator shape, and
behaviour.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, cast

_T = TypeVar("_T", bound=type)


def _contract_members(proto: type) -> set[str]:
    """The public member names a Protocol declares: its methods plus annotated
    attributes, across its bases (``Protocol``/``Generic``/``object`` excluded)."""
    members: set[str] = set()
    for base in proto.__mro__:
        if base.__name__ in ("Protocol", "Generic", "object"):
            continue
        members |= {n for n in vars(base) if not n.startswith("_")}
        members |= {n for n in getattr(base, "__annotations__", {}) if not n.startswith("_")}
    return members


def _class_annotations(cls: type) -> set[str]:
    names: set[str] = set()
    for base in cls.__mro__:
        names |= set(getattr(base, "__annotations__", {}))
    return names


def implements(*protocols: type) -> Callable[[_T], _T]:
    """Class decorator asserting the class provides every member of each given
    contract, raising ``TypeError`` at definition time if any are missing.

    Methods must be present and callable. A non-callable member (such as a
    binding's ``scheme``) must be present as a class attribute or a class-level
    annotation (``scheme: str``). The decorator records the contracts on
    ``__thingctx_implements__`` and returns the class unchanged.
    """

    def decorate(cls: _T) -> _T:
        annotated = _class_annotations(cls)
        missing: list[str] = []
        for proto in protocols:
            for member in sorted(_contract_members(proto)):
                present = hasattr(cls, member) or member in annotated
                if not present:
                    missing.append(f"{proto.__name__}.{member}")
                    continue
                # A member the protocol defines as a method must be callable here.
                proto_value = getattr(proto, member, None)
                impl_value = getattr(cls, member, None)
                if callable(proto_value) and impl_value is not None and not callable(impl_value):
                    missing.append(f"{proto.__name__}.{member} (must be callable)")
        if missing:
            raise TypeError(
                f"{cls.__name__} does not implement: "
                + ", ".join(missing)
                + ". Add the missing members; annotate non-callable ones "
                "(e.g. 'scheme: str') at class level."
            )
        # Only this class's own declarations (a base may carry its own marker).
        prior = cast("tuple[Any, ...]", cls.__dict__.get("__thingctx_implements__", ()))
        cls.__thingctx_implements__ = (*prior, *protocols)  # type: ignore[attr-defined]  # dynamic marker attr set on the decorated class
        return cls

    return decorate
