# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The binding registry: how the built-in bindings are assembled and how an
adopter replaces one or adds a new protocol.

http, mqtt, media, and local are the built-in bindings; each is only an
implementation of the :class:`~thingctx.bindings.base.ProtocolBinding` contract.

    reg = BindingRegistry.default()   # http + local, the safe default
    reg.register(OpcUaBinding())      # add a new protocol
    reg.register(MyHttpBinding())     # replace the built-in http binding
    client = ThingClient(tds=[...], bindings=reg)
"""

from __future__ import annotations

from collections.abc import Iterator
from importlib import metadata
from typing import Any

from thingctx.bindings.base import ProtocolBinding, binding_schemes, select_binding
from thingctx.thing import WoTForm

# Bump when the contract changes in a way a binding author must react to. A
# binding may read this to refuse an incompatible runtime.
CONTRACT_VERSION = "1"

# Names of the bindings thingctx ships. Each is just an implementation of the
# contract, privileged only by being bundled.
BUILTIN_BINDINGS: tuple[str, ...] = ("http", "local", "mqtt", "media", "exec")


def build_builtin(name: str, **kwargs: Any) -> ProtocolBinding:
    """Construct a built-in binding by name. Raises ``KeyError`` for an unknown
    name and the relevant ``ImportError`` if an optional dependency is absent."""
    # Each builtin is imported only when built, so a caller pulls one binding's
    # extra (httpx, paho, av) without importing the others'.
    if name == "http":
        from thingctx.bindings.builtin.http import HttpBinding  # noqa: PLC0415

        return HttpBinding(**kwargs)
    if name == "local":
        from thingctx.bindings.builtin.local import LocalBinding  # noqa: PLC0415

        return LocalBinding(**kwargs)
    if name == "mqtt":
        from thingctx.bindings.builtin.mqtt import MqttBinding  # noqa: PLC0415

        return MqttBinding(**kwargs)
    if name == "media":
        from thingctx.bindings.builtin.media import MediaBinding  # noqa: PLC0415

        return MediaBinding(**kwargs)
    if name == "exec":
        from thingctx.bindings.builtin.exec import ExecBinding  # noqa: PLC0415

        return ExecBinding(**kwargs)
    raise KeyError(f"unknown built-in binding: {name!r}")


class BindingRegistry:
    """An ordered set of bindings with scheme-aware resolution.

    A form routes to exactly one binding. :meth:`register` makes a binding the
    one that serves its scheme(s), replacing any it covers, so an adopter
    overrides a built-in or adds a new protocol with one call. Use
    :meth:`append` to add a binding at lowest precedence without replacing
    anything.
    """

    def __init__(self, bindings: list[ProtocolBinding] | None = None) -> None:
        self._bindings: list[ProtocolBinding] = list(bindings or [])

    @classmethod
    def default(
        cls,
        *,
        http: bool = True,
        local: bool = True,
        mqtt: bool = False,
        media: bool = False,
    ) -> BindingRegistry:
        """The default registry of built-in bindings. http and local match the
        default client (the documented quickstart); mqtt and media are opt in
        because they pull optional dependencies."""
        reg = cls()
        for name, want in (
            ("http", http),
            ("local", local),
            ("mqtt", mqtt),
            ("media", media),
        ):
            if want:
                reg.append(build_builtin(name))
        return reg

    def register(self, binding: ProtocolBinding) -> BindingRegistry:
        """Add a binding as the one that serves its scheme(s).

        A form routes to exactly one binding, so a binding registered for a
        scheme an existing binding already serves replaces it: any binding whose
        schemes this one fully covers is removed, and the new binding goes to the
        front. A content-routed binding (one with ``handles``) can still sit
        beside a plain binding on the same scheme, because routing picks it by
        the form's content first. Returns self (chainable).
        """
        covered = set(binding_schemes(binding))
        self._bindings = [b for b in self._bindings if not set(binding_schemes(b)) <= covered]
        self._bindings.insert(0, binding)
        return self

    def append(self, binding: ProtocolBinding) -> BindingRegistry:
        """Add a binding at lowest precedence. Returns self (chainable)."""
        self._bindings.append(binding)
        return self

    def extend(self, bindings: Any) -> BindingRegistry:
        """Append several bindings, lowest precedence first. Returns self."""
        for b in bindings or ():
            self.append(b)
        return self

    def resolve(self, form: WoTForm) -> ProtocolBinding | None:
        """The binding for ``form``: content routed first, then by scheme."""
        return select_binding(self._bindings, form)

    @property
    def bindings(self) -> list[ProtocolBinding]:
        return self._bindings

    def schemes(self) -> tuple[str, ...]:
        """Every scheme the registered bindings claim, in precedence order."""
        return tuple(s for b in self._bindings for s in binding_schemes(b))

    def __iter__(self) -> Iterator[ProtocolBinding]:
        return iter(self._bindings)

    def __len__(self) -> int:
        return len(self._bindings)


def default_bindings() -> list[ProtocolBinding]:
    """The bindings a client uses when none are supplied: http and local.
    Matches the documented quickstart; enable mqtt or media explicitly."""
    return BindingRegistry.default().bindings


def discover_bindings(*, group: str = "thingctx.bindings") -> list[ProtocolBinding]:
    """Load bindings advertised by installed packages through entry points.

    Opt in: nothing here runs unless you call it, because importing a binding
    runs third-party code in process. Each entry point names a zero-argument
    callable that returns a binding instance.
    """

    try:
        eps = metadata.entry_points(group=group)
    except TypeError:  # older selection API
        # Pre-3.10 dict-style API; typed against the modern EntryPoints, so the
        # empty-list default reads as a bad arg to the deprecated .get shim.
        eps = metadata.entry_points().get(group, [])  # type: ignore[arg-type]
    return [ep.load()() for ep in eps]


def discover_local_handlers(
    slugs: set[str] | None = None, *, group: str = "thingctx.local_handlers"
) -> dict[str, Any]:
    """Load in-process handlers advertised by installed packages, keyed by
    Thing slug (the entry point name). Each entry point names a zero-argument
    callable returning the handler for that Thing: an object whose methods are
    its actions, or a mapping of action name to callable.

    Opt in, and bounded: only entry points whose name is in ``slugs`` are
    imported (so importing third-party code is limited to the Things actually
    present), and nothing runs unless a caller invokes this. ``slugs=None``
    loads every advertised handler.
    """

    try:
        eps = metadata.entry_points(group=group)
    except TypeError:  # older selection API
        # Pre-3.10 dict-style API; typed against the modern EntryPoints, so the
        # empty-list default reads as a bad arg to the deprecated .get shim.
        eps = metadata.entry_points().get(group, [])  # type: ignore[arg-type]
    out: dict[str, Any] = {}
    for ep in eps:
        if slugs is not None and ep.name not in slugs:
            continue
        out[ep.name] = ep.load()()
    return out
