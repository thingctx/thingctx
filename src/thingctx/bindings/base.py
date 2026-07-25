# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Shared binding plumbing: the ``ProtocolBinding`` contract and its capability
protocols, response decoding, the per-owner security mixin, and selection.

A binding speaks one transport scheme. The auth mixin here resolves a resource's
declared security into neutral credential material via the shared
``resolve_credentials`` primitive; each binding then maps that material onto its
own wire with its own applier, so no auth logic lives in any transport.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from thingctx.auth import (
    DEFAULT_AUTH,
    AuthRegistry,
    AuthStrategy,
    resolve_credentials,
)
from thingctx.thing import WoTAction, WoTForm

if TYPE_CHECKING:
    from thingctx.auth.credentials import Credential

# NOTE: this module holds the transport-NEUTRAL binding contract only. Response
# decoding is transport-specific and lives with each binding (HTTP's content-type
# decoder is in builtin/http.py). Do not add a shared decoder here; a binding
# returns its own native result shape. The HTTP decoder is re-exported lazily
# below (import-on-access, no module-level cycle).


def __getattr__(name: str) -> Any:
    if name == "_decode":
        # builtin.http imports this module at load, so a module-level import back
        # would cycle; resolve _decode on access instead.
        from thingctx.bindings.builtin.http import _decode  # noqa: PLC0415

        return _decode
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@runtime_checkable
class ProtocolBinding(Protocol):
    """Speaks one transport. ``scheme`` is the URI scheme it handles
    (``http``, ``mqtt``, ``local``, ...)."""

    scheme: str

    async def invoke(
        self,
        action: WoTAction,
        form: WoTForm,
        arguments: dict[str, Any],
    ) -> Any:
        pass


class AuthMixin:
    """Per-owner security binding + credential resolution, shared by the built-in
    bindings and available to a custom one on the same terms.

    A binding that needs auth inherits this, calls ``self._init_auth(...)`` in its
    constructor, and ``await self._resolve_credentials(owner_id)`` per call, then
    hands the material to its own transport applier (``apply_http`` /
    ``apply_mqtt`` / ...), so no auth logic lives in the transport. A binding that
    needs none (like the local one) does not inherit it.

    Bind one resource with ``with_security`` or many with ``with_things``; supply
    secrets in ``credentials`` keyed by id, slug, or scheme name (looked up in
    that order). A scheme is only named; you supply the secret.
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
        # Shared, binding-scoped auth state: cached tokens and the learned
        # client-auth method per token endpoint (keyed inside the providers).
        self._auth_cache: dict = {}
        self._timeout = timeout
        # A client secret may be sent to a token endpoint; require https unless
        # the endpoint is loopback or this is explicitly allowed.
        self._allow_insecure_oauth = allow_insecure_oauth
        # Clone the default registry so per-binding extra providers don't mutate
        # the global one; extras take precedence (registered at the front).
        self._auth_registry = (auth or DEFAULT_AUTH).clone()
        for strat in extra_auth or ():
            self._auth_registry.register(strat, first=True)

    def with_security(self, thing: Any) -> AuthMixin:
        """Bind one resource's declared security schemes so requests carry the
        right auth. Returns self (chainable)."""
        self._schemes_by_name = dict(getattr(thing, "security_schemes", {}) or {})
        self._active = tuple(getattr(thing, "security", ()) or ())
        self._register(thing)
        return self

    def with_things(self, things: Any) -> AuthMixin:
        """Bind many resources so each interaction authenticates as its owner.
        Returns self (chainable)."""
        for thing in things or ():
            self._register(thing)
        return self

    def _register(self, thing: Any) -> None:
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

    @staticmethod
    def _form_security(form: Any) -> tuple | None:
        """A form's own declared security (WoT form-level security), or ``None``
        if it does not override the Thing's. This lets one Thing use a different
        scheme per affordance, e.g. a control action and a media stream."""
        if form is None:
            return None
        sec = (getattr(form, "raw", {}) or {}).get("security")
        if sec is None:
            return None
        return tuple(sec) if isinstance(sec, list | tuple) else (sec,)

    def _resolve(self, owner_id: str | None, form: Any = None) -> tuple[tuple, dict, str | None]:
        """Return (active scheme names, schemes_by_name, slug) for the owner
        of the interaction. A form may override the owner's active schemes with
        its own (form-level security), resolved against the same definitions."""
        active, schemes = self._active, self._schemes_by_name
        slug = self._slug(owner_id) if owner_id is not None else None
        if owner_id is not None and owner_id in self._things_by_id:
            active, schemes = self._things_by_id[owner_id]
        elif owner_id is None and not active and len(self._things_by_id) == 1:
            # Owner-less paths (e.g. subscribe, which carries no owner) resolve
            # against the sole bound Thing, whose declared security is
            # unambiguous; otherwise the affordance would carry no auth.
            sole_id, (active, schemes) = next(iter(self._things_by_id.items()))
            slug = self._slug(sole_id)
        form_sec = self._form_security(form)
        if form_sec is not None:
            active = form_sec
        return active, schemes, slug

    def _credential_for(self, owner_id: str | None, slug: str | None, sname: str) -> Any:
        """The secret for a scheme, looked up by owner id, then slug, then
        scheme name (so a multi-owner client carries one secret per owner)."""
        for key in (owner_id, slug, sname):
            if key is not None and key in self._credentials:
                return self._credentials[key]
        return None

    async def _resolve_credentials(
        self, owner_id: str | None = None, form: Any = None
    ) -> list[Credential]:
        """Resolve the owner's active schemes into neutral credential material
        via the shared, transport-neutral primitive. If ``form`` declares its own
        security, that overrides the owner's for this affordance."""
        active, schemes, slug = self._resolve(owner_id, form)
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


def select_binding(bindings: list[ProtocolBinding], form: WoTForm) -> ProtocolBinding | None:
    """Pick the binding for ``form``.

    Content aware bindings (e.g. media) opt in with a ``handles(form)`` method
    and take precedence; a form can route by more than its scheme (an http(s)
    href carrying a media hint goes to the media binding, not http). Everything
    else routes by transport scheme.
    """
    for inv in bindings:
        handles = getattr(inv, "handles", None)
        if callable(handles) and handles(form):
            return inv
    want = form.scheme
    for inv in bindings:
        schemes = getattr(inv, "schemes", None) or (inv.scheme,)
        if want in schemes:
            return inv
    return None


def binding_schemes(binding: ProtocolBinding) -> tuple[str, ...]:
    """Every scheme a binding claims: ``schemes`` if present, else ``scheme``."""
    return tuple(getattr(binding, "schemes", None) or (binding.scheme,))


# Capability protocols. A binding implements ``invoke`` (the core contract) and
# opts into each capability below by adding the matching method. The runtime
# checks for each at call time; these name them for type checkers and the
# conformance kit (see :mod:`thingctx.testing`).


@runtime_checkable
class ContentRouted(Protocol):
    """A binding that claims a form by its content, not only by its scheme (for
    example a media hint riding on an http href). Checked before scheme
    routing, so it can take a form a scheme match would otherwise win."""

    def handles(self, form: WoTForm) -> bool:
        pass


@runtime_checkable
class Readable(Protocol):
    """A binding that can read a property's current value."""

    async def read(self, prop: Any, form: WoTForm) -> Any:
        pass


@runtime_checkable
class Writable(Protocol):
    """A binding that can write a property's value."""

    async def write(self, prop: Any, form: WoTForm, value: Any) -> Any:
        pass


@runtime_checkable
class Subscribable(Protocol):
    """A binding that can open a push stream for an event or observable
    property and yield each value. ``target`` is the affordance (a
    ``WoTEvent``/``WoTProperty``), so the binding can authenticate as its owner;
    ``args`` carries any subscribe-time parameters (an event's
    ``subscription`` schema)."""

    async def subscribe(
        self, target: Any, form: WoTForm, args: dict[str, Any] | None = None
    ) -> Any:
        pass


@runtime_checkable
class BulkProperties(Protocol):
    """A binding that reads or writes several properties through a Thing-level
    bulk form (the readallproperties / readmultipleproperties /
    writeallproperties / writemultipleproperties ops). ``read_all`` returns a
    ``{name: value}`` map (``names`` selects a subset); ``write_all`` takes one.
    The runtime falls back to per-property read/write when a binding lacks this."""

    async def read_all(self, thing: Any, form: WoTForm, names: Any = None) -> Any:
        pass

    async def write_all(self, thing: Any, form: WoTForm, values: dict[str, Any]) -> Any:
        pass


@runtime_checkable
class AsyncAction(Protocol):
    """A binding that drives the long-running action lifecycle: start with
    ``invoke_async`` (returns an ``ActionStatus`` handle), poll with
    ``query_action``, and stop with ``cancel_action``, for an action declared
    ``synchronous: false`` with the queryaction / cancelaction ops. The runtime
    falls back to a plain ``invoke`` when a binding lacks this."""

    async def invoke_async(
        self, action: WoTAction, form: WoTForm, arguments: dict[str, Any]
    ) -> Any:
        pass

    async def query_action(self, status: Any) -> Any:
        pass

    async def cancel_action(self, status: Any) -> Any:
        pass


@runtime_checkable
class MediaConsumer(Protocol):
    """A binding that yields decoded media frames from a continuous source.
    ``frames`` is a synchronous factory returning an async iterator."""

    def frames(
        self,
        action: WoTAction,
        form: WoTForm,
        arguments: dict[str, Any],
        *,
        track: str = "video",
    ) -> Any:
        pass


@runtime_checkable
class MediaPublisher(Protocol):
    """A binding that pushes media frames to an ingest target."""

    async def publish(
        self,
        action: WoTAction,
        form: WoTForm,
        frames: Any,
        arguments: dict[str, Any],
        *,
        track: str = "video",
    ) -> None:
        pass


@runtime_checkable
class SecurityAware(Protocol):
    """A binding that accepts the consumed Things' declared security, so the
    runtime can bind auth without the adopter wiring it per call."""

    def with_things(self, things: Any) -> Any:
        pass


@runtime_checkable
class Closeable(Protocol):
    """A binding that pools transport resources and releases them on close."""

    async def aclose(self) -> None:
        pass
