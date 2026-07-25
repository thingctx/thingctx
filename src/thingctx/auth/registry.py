# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The provider registry: an ordered set of providers, first match wins."""

from __future__ import annotations

from collections.abc import Iterator
from importlib import metadata
from typing import Any

from thingctx.auth.providers import (
    ApiKeyAuth,
    AuthStrategy,
    AwsSigV4Auth,
    BasicAuth,
    DirectCredentialAuth,
    NoSecAuth,
    OAuth2AuthorizationCodeAuth,
    OAuth2ClientCredentialsAuth,
    OAuth2JwtBearerAuth,
    StaticBearerAuth,
)

__all__ = ["DEFAULT_AUTH", "AuthRegistry", "discover_auth", "register_auth"]


class AuthRegistry:
    """An ordered set of credential providers. First match wins.

    Built-ins register at the end; user providers register at the front
    (``first=True``) so they can override built-in behavior."""

    def __init__(self, strategies: list[AuthStrategy] | None = None) -> None:
        self._strategies: list[AuthStrategy] = list(strategies or [])

    def register(self, strategy: AuthStrategy, *, first: bool = True) -> AuthStrategy:
        if first:
            self._strategies.insert(0, strategy)
        else:
            self._strategies.append(strategy)
        return strategy

    def resolve(self, scheme: Any, credential: Any) -> AuthStrategy | None:
        """Find the provider that handles ``scheme`` (not the credential itself)."""
        for s in self._strategies:
            # A third-party provider's matches() must not sink the whole lookup;
            # isolate a broken plugin and try the next.
            try:
                if s.matches(scheme, credential):
                    return s
            except Exception:  # noqa: S112, PERF203 (deliberate per-provider isolation)
                continue
        return None

    def clone(self) -> AuthRegistry:
        return AuthRegistry(list(self._strategies))

    def __iter__(self) -> Iterator[AuthStrategy]:
        return iter(self._strategies)


# Order matters only among providers that match the same scheme: JWT-bearer and
# authorization-code are tried before client-credentials so a private-key or a
# user-consent (flow=code) credential routes correctly.
DEFAULT_AUTH = AuthRegistry(
    [
        DirectCredentialAuth(),  # caller-supplied Credential material wins
        NoSecAuth(),
        OAuth2JwtBearerAuth(),
        OAuth2AuthorizationCodeAuth(),
        OAuth2ClientCredentialsAuth(),
        StaticBearerAuth(),
        BasicAuth(),
        ApiKeyAuth(),
        AwsSigV4Auth(),
    ]
)


def register_auth(strategy: AuthStrategy, *, first: bool = True) -> AuthStrategy:
    """Register a custom provider on the default registry.

    By default it is inserted at the front, so it takes precedence over the
    built-ins (letting you override how an existing scheme is handled)."""
    return DEFAULT_AUTH.register(strategy, first=first)


def discover_auth(*, group: str = "thingctx.auth", register: bool = False) -> list[AuthStrategy]:
    """Load credential providers advertised by installed packages through entry
    points, the auth counterpart to ``discover_bindings``.

    Opt in: nothing here runs unless you call it, because importing a provider
    runs third-party code in process. Each entry point names a zero-argument
    callable that returns a provider instance. With ``register=True`` each
    discovered provider is also registered on the default registry, at the front
    so it can override a built-in scheme; otherwise they are only returned.
    """

    try:
        eps = metadata.entry_points(group=group)
    except TypeError:  # older selection API (Python < 3.10): dict-style select
        # On the old API entry_points() returns a dict whose .get takes a list
        # default; typeshed models only the new EntryPoints.get, so it flags the
        # list default. The runtime shim is correct on the version this runs on.
        eps = metadata.entry_points().get(group, [])  # type: ignore[arg-type]
    providers = [ep.load()() for ep in eps]
    if register:
        for p in providers:
            register_auth(p)
    return providers
