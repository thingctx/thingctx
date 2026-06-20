"""HTTP applier: map neutral credential material onto an HTTP request.

This is the only place that knows how each :class:`Credential` kind attaches to
HTTP -- bearer/basic to ``Authorization``, apikey to a header or query param,
mTLS to the client ``cert``, a ``SignatureCredential`` to a request signer chosen
by its ``algorithm``, a custom ``RequestSigner`` to a signer. The invoker just
executes the returned plan; it holds no auth logic. Kinds that have no HTTP
meaning (``EnhancedAuth``) are ignored.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from thingctx.auth.credentials import (
    ApiKeyCredential,
    BasicCredential,
    BearerToken,
    ClientCertificate,
    Credential,
    RequestSigner,
    SignatureCredential,
)
from thingctx.auth.sigv4 import _region_service, sigv4_sign

__all__ = ["HttpAuthPlan", "apply_http", "register_signer"]


@dataclass
class HttpAuthPlan:
    """How to authenticate one HTTP request: headers/params merged before the
    request is built, signers run on the assembled request, and an optional
    client-level ``cert`` (mutual TLS)."""

    headers: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    signers: list = field(default_factory=list)  # Callable[[request], Any]
    cert: Any = None  # httpx 'cert=' value


def _httpx_cert(c: ClientCertificate) -> Any:
    """httpx accepts a cert path, a (cert, key) pair, or (cert, key, password)."""
    if c.keyfile and c.password:
        return (c.certfile, c.keyfile, c.password.get_secret_value())
    if c.keyfile:
        return (c.certfile, c.keyfile)
    return c.certfile


def _aws_signer(cred: SignatureCredential):
    """A request-signer closure for the SigV4 algorithm: resolve region/service
    from the request host (when not given in ``params``) and sign in place."""

    def _sign(request: Any) -> None:
        host = urlparse(str(request.url)).netloc
        region, service = _region_service(
            cred.params.get("region"), cred.params.get("service"), host
        )
        signed = sigv4_sign(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            body=request.content or b"",
            access_key=cred.key_id.get_secret_value(),
            secret_key=cred.secret_key.get_secret_value(),
            region=region,
            service=service,
            session_token=cred.token.get_secret_value() if cred.token else None,
        )
        for k, v in signed.items():
            request.headers[k] = v

    return _sign


# algorithm -> factory(SignatureCredential) -> signer(request).
_SIGNERS: dict[str, Callable[[SignatureCredential], Callable[[Any], Any]]] = {
    "aws-sigv4": _aws_signer,
}


def register_signer(
    algorithm: str, factory: Callable[[SignatureCredential], Callable[[Any], Any]]
) -> None:
    """Teach the HTTP applier a new request-signing algorithm. ``factory`` takes
    the :class:`SignatureCredential` and returns a signer that mutates the
    assembled request in place."""
    _SIGNERS[algorithm] = factory


def apply_http(creds: list[Credential], *, base_headers: dict | None = None) -> HttpAuthPlan:
    """Build an :class:`HttpAuthPlan` from neutral credential material."""
    plan = HttpAuthPlan(headers=dict(base_headers or {}))
    for c in creds:
        if isinstance(c, BearerToken):
            plan.headers["Authorization"] = f"{c.scheme} {c.token.get_secret_value()}"
        elif isinstance(c, BasicCredential):
            raw = f"{c.username.get_secret_value()}:{c.password.get_secret_value()}".encode()
            plan.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        elif isinstance(c, ApiKeyCredential):
            if c.location == "query":
                plan.params[c.name] = c.value.get_secret_value()
            else:
                plan.headers[c.name] = c.value.get_secret_value()
        elif isinstance(c, ClientCertificate):
            plan.cert = _httpx_cert(c)
        elif isinstance(c, SignatureCredential):
            factory = _SIGNERS.get(c.algorithm)
            if factory is not None:
                plan.signers.append(factory(c))
        elif isinstance(c, RequestSigner):
            plan.signers.append(c.sign)
        # EnhancedAuth and unknown kinds have no HTTP mapping -> ignored.
    return plan
