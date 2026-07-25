# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The guard registry: how identity-provider guards are discovered and added.

The inbound gateway guard mirrors thingctx's other pluggability: a
provider-neutral contract (:class:`~thingctx.identity.jwt_guard.JwtGatewayGuard`)
plus concrete providers that register through an entry-point group, discovered
opt-in. A new IdP is one provider class + one entry point:

    # in thingctx-cognito's pyproject.toml
    [project.entry-points."thingctx.guards"]
    cognito = "thingctx_cognito:make_cognito_guard"

    # then, in a gateway
    guards = discover_guards(register=True)
    guard = guards["cognito"](user_pool_id=..., audience=...)

The one difference from ``discover_auth``: a guard needs per-deployment config
(a tenant, a team domain, an audience), so an entry point cannot return a
ready-to-use instance the way an outbound provider does. Each factory returns
the guard CLASS instead; the registry keys it by the class's ``provider``
attribute and the gateway constructs it with its own config.
"""

from __future__ import annotations

from thingctx.identity.providers.cloudflare import CloudflareAccessGuard
from thingctx.identity.providers.entra import EntraGatewayGuard

__all__ = [
    "GUARD_ENTRY_POINT_GROUP",
    "GuardRegistry",
    "DEFAULT_GUARDS",
    "register_guard",
    "discover_guards",
]

# The entry-point group a third-party ``thingctx-<idp>`` package advertises its
# guard under, the inbound counterpart to ``thingctx.bindings`` /
# ``thingctx.auth``.
GUARD_ENTRY_POINT_GROUP = "thingctx.guards"


class GuardRegistry:
    """A name->guard-class registry. Last registration for a name wins.

    A guard class is anything with a ``provider`` name and a ``validate``
    coroutine (the :class:`~thingctx.identity.jwt_guard.JwtGatewayGuard` contract).
    The registry does not instantiate guards; it hands back the class so a
    gateway constructs it with its own per-deployment config.
    """

    def __init__(self, guards: dict[str, type] | None = None) -> None:
        self._guards: dict[str, type] = dict(guards or {})

    def register(self, guard_cls: type, *, name: str | None = None) -> type:
        """Register ``guard_cls`` under ``name`` (default: its ``provider``).

        A later registration for the same name replaces the earlier one, so an
        adopter can override a bundled provider by re-registering its name."""
        key = name or getattr(guard_cls, "provider", None)
        if not key:
            raise ValueError("a guard class needs a 'provider' name (or pass name=...)")
        self._guards[str(key)] = guard_cls
        return guard_cls

    def get(self, name: str) -> type | None:
        return self._guards.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._guards)

    def clone(self) -> GuardRegistry:
        return GuardRegistry(dict(self._guards))

    def __contains__(self, name: object) -> bool:
        return name in self._guards

    def __getitem__(self, name: str) -> type:
        return self._guards[name]

    def __iter__(self):
        return iter(self._guards)

    def __len__(self) -> int:
        return len(self._guards)


def _default_registry() -> GuardRegistry:
    """The two reference providers bundled with this package."""

    reg = GuardRegistry()
    reg.register(EntraGatewayGuard)
    reg.register(CloudflareAccessGuard)
    return reg


DEFAULT_GUARDS = _default_registry()


def register_guard(guard_cls: type, *, name: str | None = None) -> type:
    """Register a custom guard class on the default registry."""
    return DEFAULT_GUARDS.register(guard_cls, name=name)


def discover_guards(
    *,
    group: str = GUARD_ENTRY_POINT_GROUP,
    register: bool = False,
    include_builtins: bool = True,
) -> dict[str, type]:
    """Load gateway guards advertised by installed packages through entry
    points, the inbound counterpart to ``discover_bindings`` / ``discover_auth``.

    Opt in: nothing here runs unless you call it, because importing a provider
    runs third-party code in process. Each entry point names a zero-argument
    callable that returns a guard CLASS (not an instance: a guard needs
    per-deployment config). The class is keyed by its ``provider`` attribute,
    falling back to the entry-point name.

    Args:
        group: the entry-point group to read (default ``thingctx.guards``).
        register: also register each discovered guard on the default registry.
        include_builtins: seed the result with this package's own Entra and
            Cloudflare providers (they are also advertised as entry points, so
            this is belt-and-braces for an editable / not-yet-installed tree).

    Returns:
        a ``{name: guard_class}`` mapping.
    """
    from importlib.metadata import entry_points

    result: dict[str, type] = {}
    if include_builtins:
        result.update({name: DEFAULT_GUARDS[name] for name in DEFAULT_GUARDS})

    try:
        eps = entry_points(group=group)
    except TypeError:  # older selection API
        eps = entry_points().get(group, [])  # type: ignore[attr-defined]

    for ep in eps:
        factory = ep.load()
        guard_cls = factory()
        name = getattr(guard_cls, "provider", None) or ep.name
        result[str(name)] = guard_cls

    if register:
        for name, guard_cls in result.items():
            register_guard(guard_cls, name=name)
    return result
