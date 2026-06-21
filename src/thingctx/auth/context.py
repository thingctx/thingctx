"""The context a provider needs to resolve one owner's credential.

Transport-neutral on purpose: no headers, no request object, nothing that ties
a credential to how it will eventually be attached. A provider that mints a
token may still call its IdP over HTTPS -- that is the auth layer talking to an
identity provider, not the owner's transport leaking in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["AuthContext"]


@dataclass
class AuthContext:
    """Everything a provider needs to resolve credential material for one owner."""

    scheme: Any  # a security scheme (.scheme, .token, .scopes, .in_, .key_name, .raw)
    credential: Any  # the runtime secret(s) for this owner/scheme
    owner_id: str | None = None
    timeout: float = 30.0
    cache: dict = field(default_factory=dict)  # invoker-scoped token/state cache
    allow_insecure_oauth: bool = False
