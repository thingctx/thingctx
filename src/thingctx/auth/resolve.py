"""The shared resolution primitive: turn an owner's active schemes + secrets into
neutral credential material, transport-agnostically.

Both HttpInvoker and MqttInvoker (and any future transport) call this -- it is
the single place that walks an owner's declared security, looks up the matching
provider, and resolves each into a :class:`Credential`. The caller then hands
the list to its transport applier (``apply_http`` / ``apply_mqtt`` / ...). No
transport specifics live here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from thingctx.auth.context import AuthContext
from thingctx.auth.credentials import Credential
from thingctx.auth.registry import AuthRegistry

__all__ = ["resolve_credentials"]


async def resolve_credentials(
    *,
    registry: AuthRegistry,
    active: tuple,
    schemes: dict,
    credential_for: Callable[[str], Any],
    owner_id: str | None = None,
    cache: dict | None = None,
    timeout: float = 30.0,
    allow_insecure_oauth: bool = False,
) -> list[Credential]:
    """Resolve every active scheme of one owner into credential material.

    ``active`` are the scheme names declared as active; ``schemes`` maps name ->
    a security-scheme object; ``credential_for(name)`` returns the runtime secret
    for that scheme (the invoker decides how it is keyed). Returns the list of
    non-empty :class:`Credential` material, in declaration order.
    """
    cache = cache if cache is not None else {}
    creds: list[Credential] = []
    for sname in active:
        scheme = schemes.get(sname)
        if scheme is None:
            continue
        secret = credential_for(sname)
        provider = registry.resolve(scheme, secret)
        if provider is None:
            continue
        ctx = AuthContext(
            scheme=scheme,
            credential=secret,
            owner_id=owner_id,
            timeout=timeout,
            cache=cache,
            allow_insecure_oauth=allow_insecure_oauth,
        )
        cred = await provider.resolve(ctx)
        if cred is not None:
            creds.append(cred)
    return creds
