"""The Policy Enforcement Point: authorization at the runtime dispatch layer.

:class:`AuthorizedClient` wraps a :class:`thingctx.ThingClient` and intercepts
``invoke`` / ``read_property`` / ``write_property``. For each call it resolves
the ``(thing_id, affordance, op)`` being attempted, asks the PDP, and either
delegates to the real client or refuses BEFORE the client selects a binding or
touches any transport.

Why a wrapper and not a broker/topic check: the enforcement sits BELOW the
transport choice. A multi-transport Thing (an HTTP
form AND an MQTT form for the same affordance) hits ONE check here, whichever
transport the call would route to. A topic-level ACL only sees the transport it
brokers, so it misses the HTTP path; this PEP cannot, because it fires before
``ThingClient`` even resolves a form. That is the multi-transport bypass closing
structurally.

Every WoT operation is enforced: actions (``invokeaction``), the property
read/write split (``readproperty`` / ``writeproperty``), observe / events
(``observeproperty`` / ``subscribeevent``), and media (as ``invokeaction`` on the
media action). Streaming ops use two enforcement points, a subscribe-time gate
plus a per-delivery filter that reads the token ``exp`` and stops the stream when
it lapses (see :func:`_authorized_stream`).
"""

from __future__ import annotations

from typing import Any

from thingctx import ThingClient
from thingctx.authz.pdp import AccessRequest, PolicyDecisionPoint


class AuthorizationDenied(Exception):
    """Raised by the PEP when the PDP denies a call, before any device touch.

    Deliberately distinct from thingctx's own error ENVELOPES (dict returns like
    ``{"error": ...}``) and from an authentication error (a failure to validate
    the inbound token before its claims ever reach this PEP). This one means: the
    identity was authenticated, but is not authorized for this
    ``(thing, affordance, op)``.
    """

    def __init__(self, request: AccessRequest, reason: str) -> None:
        self.request = request
        self.reason = reason
        super().__init__(
            f"authorization denied: {request.op} on "
            f"{request.thing_id}/{request.affordance} ({reason})"
        )


async def _single_value_aiter(value: Any):
    """An async iterator that yields exactly one value (a denial envelope) then
    stops, so a `raise_on_deny=False` subscribe denial is visible to `async for`."""
    yield value


def _token_expired(identity: Any, *, now: float | None = None) -> bool:
    """True if the identity's token has expired.

    The identity is the validated claims dict the guard returned; a JWT carries
    ``exp`` (seconds since the epoch). This is what makes the per-delivery filter
    REAL, not a re-run of a pure function: a claims dict does not expire on its
    own, but its ``exp`` claim is a wall-clock deadline we compare against now.
    A missing ``exp`` is treated as expired (fail-closed): the guard requires exp
    on inbound tokens, so its absence here means an untrusted identity."""
    import time as _time

    if not isinstance(identity, dict):
        return True  # no claims -> cannot prove validity -> treat as expired
    exp = identity.get("exp")
    if not isinstance(exp, int | float):
        return True
    return (now if now is not None else _time.time()) >= float(exp)


async def _authorized_stream(stream, pdp, identity, request, *, revocation_check=None):
    """Wrap a device stream so each delivered value is re-authorized, and STOP the
    stream the moment authorization lapses.

    Two lapse conditions, both real:
    1. TOKEN EXPIRY: the identity's ``exp`` deadline passes while the stream lives.
       Checked against the wall clock on each delivery, so a stream cannot outlive
       the token that authorized it. THIS is the staleness window, and it is
       closed by reading exp, not by re-running the PDP.
    2. REVOCATION: an optional ``revocation_check(identity, request) -> bool`` that
       returns True to revoke (a role pulled, a policy change). The default
       re-asks the PDP, which catches a policy/grant change; combined with the exp
       check it catches both a lapsed token and a lapsed grant.

    On lapse we cut the stream FORWARD (stop yielding); we do not claw back values
    already delivered, the correct semantics for a live-feed revocation."""
    async for value in stream:
        if _token_expired(identity):
            return  # token expired: stop the stream
        decision = await pdp.decide(identity, request)
        if not decision.permit:
            return  # grant/policy lapsed: stop the stream
        if revocation_check is not None and await revocation_check(identity, request):
            return
        yield value


class AuthorizedClient:
    """A PEP proxy around ``ThingClient``. Same dispatch surface, authorized.

    Construct with the underlying client, a PDP, and the validated identity
    (the claims dict a guard's ``validate`` returned, or ``None``). Each guarded
    method resolves the affordance the underlying client would use, asks the PDP
    with the op fixed for that method, and only then delegates.

    On denial it RAISES :class:`AuthorizationDenied` by default (so a denied
    call can never be mistaken for a device response). Pass
    ``raise_on_deny=False`` to instead return a thingctx-style error envelope
    (``{"error": "authorization denied", ...}``), matching how ``ThingClient``'s
    approval gate returns a blocked envelope.

    Args:
        client: the real :class:`thingctx.ThingClient` to guard.
        pdp: the :class:`PolicyDecisionPoint` to consult.
        identity: the validated claims (dict) for the caller, or ``None``.
        raise_on_deny: raise :class:`AuthorizationDenied` (default) vs return an
            error envelope.
    """

    def __init__(
        self,
        client: ThingClient,
        pdp: PolicyDecisionPoint,
        *,
        identity: Any = None,
        raise_on_deny: bool = True,
    ) -> None:
        self._client = client
        self._pdp = pdp
        self._identity = identity
        self._raise = raise_on_deny

    # -- affordance resolution ------------------------------------------- #
    #
    # The dispatch methods take a dotted "<slug>.<name>" tool key. We resolve
    # that key back to the SAME affordance object the underlying client would
    # dispatch (via its own _route/_props/_events maps), so the (thing_id,
    # affordance) we authorize is exactly what will run. Reading the client's
    # resolved maps (rather than re-deriving the slug) is what keeps the PEP
    # correct for URL-shaped ids and any future slug change: we authorize the
    # real target, not a guessed name.

    def _resolve_affordance(self, kind: str, key: str) -> Any:
        table = {
            "action": getattr(self._client, "_route", {}),
            "property": getattr(self._client, "_props", {}),
            "event": getattr(self._client, "_events", {}),
        }[kind]
        return table.get(key)

    async def _check(self, kind: str, key: str, op: str) -> AccessRequest | dict | None:
        """Authorize one dispatch. Returns None to proceed, or (when
        ``raise_on_deny`` is False) an error envelope; raises on deny otherwise.

        If the affordance is unknown to the underlying client, we do NOT raise:
        we let the real method run so its own "unknown ..." envelope is returned
        unchanged. Authorizing something that does not exist is meaningless, and
        we must not turn a 'not found' into an 'unauthorized'.
        """
        affordance = self._resolve_affordance(kind, key)
        if affordance is None:
            return None  # unknown target: let the real client answer

        # The form the client would pick, purely for audit context (scheme). The
        # decision does NOT depend on it: that is the multi-transport point.
        form = affordance.primary_form(prefer=getattr(self._client, "_prefer", ()))
        request = AccessRequest(
            thing_id=affordance.thing_id,
            affordance=affordance.name,
            op=op,
            form_scheme=(form.scheme if form is not None else None),
        )
        decision = await self._pdp.decide(self._identity, request)
        if decision.permit:
            return None
        if self._raise:
            raise AuthorizationDenied(request, decision.reason)
        return {
            "error": "authorization denied",
            "thing": request.thing_id,
            "affordance": request.affordance,
            "op": request.op,
            "reason": decision.reason,
        }

    # -- guarded dispatch ------------------------------------------------- #

    async def invoke(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Authorize ``invokeaction`` for the action, then delegate."""
        blocked = await self._check("action", tool_name, "invokeaction")
        if blocked is not None:
            return blocked
        return await self._client.invoke(tool_name, arguments)

    async def read_property(self, name: str) -> Any:
        """Authorize ``readproperty`` for the property, then delegate."""
        blocked = await self._check("property", name, "readproperty")
        if blocked is not None:
            return blocked
        return await self._client.read_property(name)

    async def write_property(self, name: str, value: Any) -> Any:
        """Authorize ``writeproperty`` for the property, then delegate."""
        blocked = await self._check("property", name, "writeproperty")
        if blocked is not None:
            return blocked
        return await self._client.write_property(name, value)

    async def subscribe(self, name: str) -> Any:
        """Authorize the subscription at establish time, then re-check on every
        delivered value.

        WoT ``subscribe`` covers two operations: an EVENT (``subscribeevent``)
        and an observable PROPERTY (``observeproperty``). We resolve which and
        authorize the right op. Two enforcement points, because a stream is not a
        request/response:

        1. SUBSCRIBE-TIME GATE: authorize ``(thing, affordance, op)`` before the
           binding opens the stream. An ungranted caller never subscribes.
        2. PER-DELIVERY FILTER: the token can expire while the stream lives, so we
           re-authorize before yielding each value and STOP the stream the moment
           the grant lapses (a revoked role, an expired identity). This closes the
           staleness window an authorize-once gate would leave open.
        """
        # An observable property is in _props; an event is in _events. Resolve the
        # op from where the name lives (events checked first, matching the client).
        event = self._resolve_affordance("event", name)
        if event is not None:
            kind, op, affordance = "event", "subscribeevent", event
        else:
            prop = self._resolve_affordance("property", name)
            if prop is None:
                # Unknown target: let the real client answer with its own error.
                return await self._client.subscribe(name)
            kind, op, affordance = "property", "observeproperty", prop

        # 1. Subscribe-time gate.
        blocked = await self._check(kind, name, op)
        if blocked is not None:
            # raise_on_deny=False path: yield a single error envelope as a stream
            # so `async for` sees the denial rather than silently nothing.
            return _single_value_aiter(blocked)

        stream = await self._client.subscribe(name)

        # 2. Per-delivery filter: re-authorize before each value; stop on lapse.
        request = AccessRequest(thing_id=affordance.thing_id, affordance=affordance.name, op=op)
        return _authorized_stream(stream, self._pdp, self._identity, request)

    def frames(
        self, name: str, arguments: dict[str, Any] | None = None, *, track: str = "video"
    ) -> Any:  # returns an async iterator
        """Media consumption (a continuous stream) reaches the device. A media
        affordance is a WoT ACTION, so it is authorized as ``invokeaction`` (the
        op its form declares), gated at establish time, and filtered per frame.

        ``ThingClient.frames`` is ``async def`` and returns an async iterator; we
        wrap it so the invokeaction gate fires before the stream opens, then
        filter each frame."""
        affordance = self._resolve_media(name)
        if affordance is None:
            return self._passthrough_frames(name, arguments, track)
        # Authorize invokeaction (media's real op): return an async iterator that
        # checks the gate before opening the device stream.
        return self._media_frames(affordance, name, arguments, track)

    async def _passthrough_frames(self, name, arguments, track):
        """Unknown media affordance: delegate to the client (which answers with
        its own 'unknown' handling). Awaits the coroutine, then yields."""
        stream = await self._client.frames(name, arguments, track=track)
        async for v in stream:
            yield v

    async def _media_frames(self, affordance: Any, name: str, arguments: dict | None, track: str):
        blocked = await self._check_affordance(affordance, "invokeaction")
        if blocked is not None:
            yield blocked
            return
        request = AccessRequest(
            thing_id=affordance.thing_id, affordance=affordance.name, op="invokeaction"
        )
        # ThingClient.frames is `async def` and RETURNS an async iterator, so it
        # must be awaited before iterating (not a sync factory).
        stream = await self._client.frames(name, arguments, track=track)
        async for value in _authorized_stream(stream, self._pdp, self._identity, request):
            yield value

    async def publish(
        self,
        name: str,
        frames: Any,
        arguments: dict[str, Any] | None = None,
        *,
        track: str = "video",
    ) -> Any:
        """Media publish reaches the device (a write of a live signal). A media
        affordance is a WoT action, so authorize ``invokeaction`` before
        delegating. Matches the real ``ThingClient.publish`` signature."""
        affordance = self._resolve_media(name)
        if affordance is None:
            return await self._client.publish(name, frames, arguments, track=track)
        blocked = await self._check_affordance(affordance, "invokeaction")
        if blocked is not None:
            return blocked
        return await self._client.publish(name, frames, arguments, track=track)

    def _resolve_media(self, key: Any) -> Any:
        """Resolve a media affordance from the client's media map. Media
        affordances live in ThingClient._media (split out of the action route).
        Returns None if unknown."""
        media = getattr(self._client, "_media", {})
        if isinstance(key, str):
            return media.get(key)
        return key if hasattr(key, "thing_id") and hasattr(key, "name") else None

    async def _check_affordance(self, affordance: Any, op: str) -> dict | None:
        """Authorize a resolved affordance object for ``op``. Returns None to
        proceed, an envelope on deny (raise_on_deny=False), or raises."""
        form = affordance.primary_form(prefer=getattr(self._client, "_prefer", ()))
        request = AccessRequest(
            thing_id=affordance.thing_id,
            affordance=affordance.name,
            op=op,
            form_scheme=(form.scheme if form is not None else None),
        )
        decision = await self._pdp.decide(self._identity, request)
        if decision.permit:
            return None
        if self._raise:
            raise AuthorizationDenied(request, decision.reason)
        return {
            "error": "authorization denied",
            "thing": request.thing_id,
            "affordance": request.affordance,
            "op": request.op,
            "reason": decision.reason,
        }

    def as_tools(self) -> Any:
        """Return the tool specs and the GUARDED invoke, never the raw one.

        ThingClient.as_tools() hands back (specs, self.invoke). If we forwarded
        that, the caller (an agent loop, the MCP bridge) would get the UNGUARDED
        invoke and every authorization check would be bypassed. So we return this
        PEP's own guarded invoke instead."""
        specs = self._client.list_actions()
        return specs, self.invoke

    # -- pass-throughs (fail-closed allowlist) --------------------------- #
    #
    # A default-forward proxy on a security boundary is default-ALLOW: every
    # device-reaching method not explicitly wrapped would sail through. So we do
    # NOT forward by default. Only these read-only introspection / lifecycle
    # members, which do not reach a device, pass through. Anything else raises
    # AttributeError, which is the safe failure: a new device-reaching method on
    # ThingClient does not silently become an unguarded hole; the PEP breaks
    # loudly until it is explicitly wrapped or allowlisted.
    _SAFE_PASSTHROUGH = frozenset(
        {
            "list_actions",
            "list_properties",
            "list_events",
            "list_media",
            "tool_specs",
            "action_for",
            "things",
            "media_form",
            "aclose",
            "__aenter__",
            "__aexit__",
        }
    )

    def __getattr__(self, name: str) -> Any:
        if name in type(self)._SAFE_PASSTHROUGH:
            return getattr(self._client, name)
        raise AttributeError(
            f"{type(self).__name__} does not expose {name!r}: it is not an "
            f"allowlisted safe method, and forwarding it could bypass "
            f"authorization. Wrap it in the PEP or add it to _SAFE_PASSTHROUGH "
            f"only if it does not reach a device."
        )


def guard_client(
    client: ThingClient,
    pdp: PolicyDecisionPoint,
    *,
    identity: Any = None,
    raise_on_deny: bool = True,
) -> AuthorizedClient:
    """Wrap ``client`` in a PEP for ``identity``, deciding via ``pdp``.

    Factory sugar for :class:`AuthorizedClient`.
    """
    return AuthorizedClient(client, pdp, identity=identity, raise_on_deny=raise_on_deny)
