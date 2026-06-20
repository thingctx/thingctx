"""Shared invoker plumbing: the ``Invoker`` protocol, response decoding, the
per-owner security binding, and transport selection.

An invoker speaks one transport scheme. The binding here resolves a resource's
declared security into neutral credential material via the shared
``resolve_credentials`` primitive; each transport then maps that material with
its own applier, so no auth logic lives in any transport.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from thingctx.auth import (
    DEFAULT_AUTH,
    AuthRegistry,
    AuthStrategy,
    resolve_credentials,
)
from thingctx.thing import WoTAction, WoTForm


def _decode(resp, empty=None):
    """Decode a response by its content type: JSON to a value, text to a str,
    anything else (e.g. an image) to raw bytes. An empty body returns `empty`."""
    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
    if ctype == "application/json" or ctype.endswith("+json"):
        return resp.json()
    if not resp.content:
        return empty
    if ctype.startswith("text/") or ctype == "":
        return resp.text
    return resp.content


@runtime_checkable
class Invoker(Protocol):
    """Speaks one transport. ``scheme`` is the URI scheme it handles
    (``http``, ``mqtt``, ``local``, ...)."""

    scheme: str

    async def invoke(
        self,
        action: WoTAction,
        form: WoTForm,
        arguments: dict[str, Any],
    ) -> Any: ...


class _AuthBinding:
    """Per-owner security binding + credential resolution, shared by every
    transport invoker.

    Holds the auth registry, the runtime secrets, and the schemes each owning
    resource declares; ``resolve_credentials`` (the transport-neutral primitive)
    turns those into :class:`~thingctx.auth.credentials.Credential` material.
    Each invoker then hands the material to its own transport applier
    (``apply_http`` / ``apply_mqtt`` / ...) -- so no auth logic lives in the
    transport. Bind one resource with ``with_security`` or many with
    ``with_things``; supply secrets in ``credentials`` keyed by id, slug, or
    scheme name (looked up in that order). A scheme is only named; you supply
    the secret.
    """

    def _init_auth(
        self,
        *,
        credentials: dict | None,
        auth: AuthRegistry | None,
        extra_auth: list[AuthStrategy] | None,
        timeout: float,
        allow_insecure_oauth: bool = False,
    ) -> None:
        self._credentials = credentials or {}
        self._schemes_by_name: dict = {}  # set by with_security()
        self._active: tuple = ()
        # owner id -> (active security names, schemes_by_name); set by
        # with_security/with_things so auth resolves per owning resource.
        self._things_by_id: dict = {}
        # Shared, invoker-scoped auth state: cached tokens and the learned
        # client-auth method per token endpoint (keyed inside the providers).
        self._auth_cache: dict = {}
        self._timeout = timeout
        # A client secret may be sent to a token endpoint; require https unless
        # the endpoint is loopback or this is explicitly allowed.
        self._allow_insecure_oauth = allow_insecure_oauth
        # Clone the default registry so per-invoker extra providers don't mutate
        # the global one; extras take precedence (registered at the front).
        self._auth_registry = (auth or DEFAULT_AUTH).clone()
        for strat in extra_auth or ():
            self._auth_registry.register(strat, first=True)

    def with_security(self, thing):
        """Bind one resource's declared security schemes so requests carry the
        right auth. Returns self (chainable)."""
        self._schemes_by_name = dict(getattr(thing, "security_schemes", {}) or {})
        self._active = tuple(getattr(thing, "security", ()) or ())
        self._register(thing)
        return self

    def with_things(self, things):
        """Bind many resources so each interaction authenticates as its owner.
        Returns self (chainable)."""
        for thing in things or ():
            self._register(thing)
        return self

    def _register(self, thing) -> None:
        tid = getattr(thing, "id", None)
        if tid is None:
            return
        self._things_by_id[tid] = (
            tuple(getattr(thing, "security", ()) or ()),
            dict(getattr(thing, "security_schemes", {}) or {}),
        )

    @staticmethod
    def _slug(thing_id: str) -> str:
        """Owner id -> short slug, matching the tool-name scheme
        (``urn:thingctx:my-service:v2`` -> ``my-service``)."""
        parts = [p for p in str(thing_id).split(":") if p]
        if len(parts) >= 2 and parts[-1].lower().lstrip("v").isdigit():
            parts = parts[:-1]
        slug = parts[-1] if parts else str(thing_id)
        return "".join(c if (c.isalnum() or c in "._-") else "-" for c in slug)

    def _resolve(self, owner_id: str | None):
        """Return (active scheme names, schemes_by_name, slug) for the owner
        of the interaction."""
        active, schemes = self._active, self._schemes_by_name
        if owner_id is not None and owner_id in self._things_by_id:
            active, schemes = self._things_by_id[owner_id]
        slug = self._slug(owner_id) if owner_id is not None else None
        return active, schemes, slug

    def _credential_for(self, owner_id, slug, sname):
        """The secret for a scheme, looked up by owner id, then slug, then
        scheme name (so a multi-owner client carries one secret per owner)."""
        for key in (owner_id, slug, sname):
            if key is not None and key in self._credentials:
                return self._credentials[key]
        return None

    async def _resolve_credentials(self, owner_id: str | None = None) -> list:
        """Resolve the owner's active schemes into neutral credential material
        via the shared, transport-neutral primitive."""
        active, schemes, slug = self._resolve(owner_id)
        return await resolve_credentials(
            registry=self._auth_registry,
            active=active,
            schemes=schemes,
            credential_for=lambda s: self._credential_for(owner_id, slug, s),
            owner_id=owner_id,
            cache=self._auth_cache,
            timeout=self._timeout,
            allow_insecure_oauth=self._allow_insecure_oauth,
        )


def select_invoker(invokers: list[Invoker], form: WoTForm) -> Invoker | None:
    """Pick the invoker that handles ``form``'s transport scheme."""
    want = form.scheme
    for inv in invokers:
        schemes = getattr(inv, "schemes", None) or (inv.scheme,)
        if want in schemes:
            return inv
    return None
