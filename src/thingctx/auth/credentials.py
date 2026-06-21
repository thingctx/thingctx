"""Neutral, transport-agnostic credential material.

The only contract between the auth layer and the transports: a provider resolves
a security scheme + runtime secret into one of these, and each applier maps the
kinds it understands onto its protocol. Nothing here is transport-specific.

Each ``secret(...)`` field is held in a :class:`Secret`; the base
:class:`Credential` masks secret fields in ``repr`` and :meth:`Credential.wipe`
zeroes them. Non-secret fields stay visible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import MISSING, dataclass, field, fields
from typing import Any

from thingctx.auth.secret import Secret

__all__ = [
    "Credential",
    "Secret",
    "BearerToken",
    "BasicCredential",
    "ApiKeyCredential",
    "SignatureCredential",
    "ClientCertificate",
    "EnhancedAuth",
    "RequestSigner",
]

_REDACTED = "***"


def secret(default: Any = MISSING) -> Any:
    """Mark a dataclass field as a secret: coerced to a :class:`Secret` and
    masked in ``repr``."""
    if default is MISSING:
        return field(metadata={"secret": True})
    return field(default=default, metadata={"secret": True})


@dataclass
class Credential:
    """Base for resolved credential material. Pure data, no transport.

    Coerces every ``secret(...)`` field to a :class:`Secret`, masks those fields
    in ``repr``, and :meth:`wipe` zeroes them.
    """

    def __post_init__(self) -> None:
        for f in fields(self):
            if not f.metadata.get("secret"):
                continue
            value = getattr(self, f.name)
            if value is not None and not isinstance(value, Secret):
                object.__setattr__(self, f.name, Secret(value))

    def wipe(self) -> None:
        """Zero every secret field's buffer. The credential is unusable after."""
        for f in fields(self):
            if f.metadata.get("secret"):
                value = getattr(self, f.name)
                if isinstance(value, Secret):
                    value.wipe()

    def __repr__(self) -> str:
        parts = []
        for f in fields(self):
            if not f.repr:
                continue
            value = getattr(self, f.name)
            if f.metadata.get("secret"):
                shown = repr(value) if value is None else _REDACTED
            else:
                shown = repr(value)
            parts.append(f"{f.name}={shown}")
        return f"{type(self).__name__}({', '.join(parts)})"


@dataclass(repr=False)
class BearerToken(Credential):
    """A bearer token. ``scheme`` is the word used in an HTTP ``Authorization``
    header; a transport with no header uses ``token`` directly."""

    token: str = secret()
    scheme: str = "Bearer"


@dataclass(repr=False)
class BasicCredential(Credential):
    """A username/password pair."""

    username: str = secret()
    password: str = secret()


@dataclass(repr=False)
class ApiKeyCredential(Credential):
    """An API key. ``location`` (``"header"`` or ``"query"``) tells a transport
    where it belongs."""

    name: str
    value: str = secret()
    location: str = "header"


@dataclass(repr=False)
class SignatureCredential(Credential):
    """Key material for a request-*signing* scheme. ``algorithm`` selects the
    signer an applier uses; ``params`` carries signer hints. A transport that
    cannot sign a request ignores it."""

    algorithm: str
    key_id: str = secret()
    secret_key: str = secret()
    token: str | None = secret(default=None)
    params: dict = field(default_factory=dict)


@dataclass(repr=False)
class ClientCertificate(Credential):
    """A client certificate for mutual TLS, reusable by any TLS transport."""

    certfile: str
    keyfile: str | None = None
    ca_certs: str | None = None
    password: str | None = secret(default=None)


@dataclass(repr=False)
class EnhancedAuth(Credential):
    """Connection-level challenge/response auth material. ``method`` is the
    mechanism name; ``data`` is the initial auth data. Only a transport with a
    connect-time auth exchange consumes this."""

    method: str
    data: bytes = secret(default=b"")


@dataclass(repr=False)
class RequestSigner(Credential):
    """A transport-specific signer expressed as a callable. The applier appends
    ``sign`` to its signer list; ``sign`` may be sync or async and receives the
    transport's native request object."""

    sign: Callable[[Any], Any]
