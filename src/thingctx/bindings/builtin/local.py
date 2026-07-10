# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""LocalBinding: drive an in-process callable (or object) as a transport."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from thingctx.bindings.base import ProtocolBinding
from thingctx.contracts import implements


@implements(ProtocolBinding)
class LocalBinding:
    """Invoke actions whose form points at an in-process callable.

    Pass the callables in one expression, keyed by action name, or an
    object whose methods match the action names::

        LocalBinding({"set_speed": fn, "status": fn})   # a mapping
        LocalBinding(pump_device)                        # an object

    Sync and async callables both work (sync ones are awaited for you).
    A form with no scheme (or ``local://name``) routes here. You can also
    ``.register(name, fn)`` after construction.

    Capability coverage: ``invoke``, property ``read`` / ``write``, and
    ``subscribe`` (the device pushes via :meth:`emit`). No authentication: this
    is in-process, so security is a transport concern that does not apply. No
    dedicated bulk or async-lifecycle methods, but both stay functional through
    the runtime's fallbacks: bulk read/write loops over per-property ``read`` /
    ``write``, and a long-running action falls back to a synchronous ``invoke``
    (the handler runs to completion and the result is returned directly, with no
    separate ``query`` / ``cancel`` handle).
    """

    scheme = "local"

    def __init__(self, handlers: Any = None) -> None:
        self._fns: dict[str, Callable[..., Any]] = {}
        self._obj = None
        # name -> list of subscriber queues (events / observable props).
        self._subs: dict[str, list] = {}
        # slug -> a sub-binding owning one Thing's handlers, so several
        # in-process Things in one process keep distinct handlers even when
        # their action names collide. Empty for the single-Thing case, which
        # resolves against this instance directly.
        self._by_slug: dict[str, LocalBinding] = {}
        if isinstance(handlers, dict):
            for k, fn in handlers.items():
                self.register(k, fn)
        elif handlers is not None:
            # An object: resolve action names to its methods on demand.
            self._obj = handlers

    def register(self, key: str, fn: Callable[..., Any]) -> None:
        self._fns[key] = fn

    def register_thing(self, slug: str, handlers: Any) -> None:
        """Bind one Thing's in-process handlers under its slug. An affordance
        whose Thing matches ``slug`` resolves against these handlers, isolated
        from other Things' handlers in the same process."""
        self._by_slug[slug] = (
            handlers if isinstance(handlers, LocalBinding) else LocalBinding(handlers)
        )

    def _sub_for(self, thing_id: Any):
        if not self._by_slug or thing_id is None:
            return None
        from thingctx.thing import thing_slug

        return self._by_slug.get(thing_slug(thing_id))

    def _resolve(self, name: str) -> Callable[..., Any] | None:
        if name in self._fns:
            return self._fns[name]
        if self._obj is not None:
            fn = getattr(self._obj, name, None)
            if callable(fn):
                return fn
        return None

    async def invoke(self, action, form, arguments):  # noqa: ANN001
        sub = self._sub_for(action.thing_id)
        if sub is not None:
            return await sub.invoke(action, form, arguments)
        # Try the form href tail, then the action name.
        key = (form.href.split("://", 1)[-1] if form.href else "") or action.name
        fn = self._resolve(key) or self._resolve(action.name)
        if fn is None:
            return {"error": f"no local callable registered for {key!r}"}
        import inspect

        try:
            result = fn(**arguments)
        except TypeError as e:
            # Bad or extra args; return the error as data so the caller
            # can correct, rather than crashing.
            return {"error": f"invalid arguments for {action.name}: {e}"}
        if inspect.isawaitable(result):
            return await result
        return result

    # Telemetry: read a property, subscribe to a stream
    async def read(self, prop, form):  # noqa: ANN001
        """Read a property's current value. Resolves ``get_<name>`` /
        ``<name>`` on the device object, else a same-named attribute."""
        sub = self._sub_for(prop.thing_id)
        if sub is not None:
            return await sub.read(prop, form)
        key = (form.href.split("://", 1)[-1] if form.href else "") or prop.name
        fn = self._resolve(f"get_{prop.name}") or self._resolve(key) or self._resolve(prop.name)
        if fn is not None:
            import inspect

            r = fn()
            return await r if inspect.isawaitable(r) else r
        # Fall back to a plain attribute on the device object.
        if self._obj is not None and hasattr(self._obj, prop.name):
            return getattr(self._obj, prop.name)
        return {"error": f"no readable source for property {prop.name!r}"}

    async def write(self, prop, form, value):  # noqa: ANN001
        """Write a property: call ``set_<name>(value)`` on the device,
        else set a same-named attribute."""
        import inspect

        sub = self._sub_for(prop.thing_id)
        if sub is not None:
            return await sub.write(prop, form, value)
        fn = self._resolve(f"set_{prop.name}")
        if fn is not None:
            r = fn(value)
            return await r if inspect.isawaitable(r) else r
        if self._obj is not None and hasattr(self._obj, prop.name):
            setattr(self._obj, prop.name, value)
            return {"ok": True, prop.name: value}
        return {"error": f"no writable target for property {prop.name!r}"}

    async def subscribe(self, target, form, args=None):  # noqa: ANN001
        """Subscribe to an event / observable property. Returns an async
        iterator yielding pushed values. The device pushes with :meth:`emit`.
        ``target`` is the affordance (its ``name`` keys the subscriber list);
        ``args`` are ignored locally (an in-process device sees every emit)."""
        import asyncio

        name = target if isinstance(target, str) else target.name
        queue: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(name, []).append(queue)

        async def _stream():
            try:
                while True:
                    yield await queue.get()
            finally:
                subs = self._subs.get(name, [])
                if queue in subs:
                    subs.remove(queue)

        return _stream()

    def emit(self, name: str, value: Any) -> None:
        """Device side: push a value to everyone subscribed to ``name``
        (an event or an observable property)."""
        for q in self._subs.get(name, []):
            q.put_nowait(value)
