"""The provider registry: an ordered set of providers, first match wins."""

from __future__ import annotations

from typing import Any

from thingctx.auth.providers import (
    ApiKeyAuth,
    AuthStrategy,
    AwsSigV4Auth,
    BasicAuth,
    DirectCredentialAuth,
    NoSecAuth,
    OAuth2ClientCredentialsAuth,
    OAuth2JwtBearerAuth,
    StaticBearerAuth,
)

__all__ = ["AuthRegistry", "DEFAULT_AUTH", "register_auth"]


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
            try:
                if s.matches(scheme, credential):
                    return s
            except Exception:  # noqa: BLE001 - a misbehaving provider must not break others
                continue
        return None

    def clone(self) -> AuthRegistry:
        return AuthRegistry(list(self._strategies))

    def __iter__(self):
        return iter(self._strategies)


# Order matters only among providers that match the same scheme: JWT-bearer is
# tried before client-credentials so a private-key credential routes correctly.
DEFAULT_AUTH = AuthRegistry(
    [
        DirectCredentialAuth(),  # caller-supplied Credential material wins
        NoSecAuth(),
        OAuth2JwtBearerAuth(),
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
