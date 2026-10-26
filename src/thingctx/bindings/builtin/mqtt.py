# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""MqttBinding: drive a Thing over mqtt (publish + await a reply)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import ssl
import urllib.parse
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from thingctx.auth import (
    AuthRegistry,
    AuthStrategy,
    BearerToken,
    Credential,
    apply_mqtt,
    resolve_credentials,
)
from thingctx.bindings.base import AuthMixin, ProtocolBinding
from thingctx.contracts import implements
from thingctx.reliability import RetryPolicy, TransportError
from thingctx.thing import _filled_form, _href_var_names, _usable_user_name, is_mqtt_user_name

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from thingctx.auth import ClientCertificate
    from thingctx.auth.mqtt import MqttAuthPlan
    from thingctx.thing import WoTAction, WoTForm, WoTProperty


def _decode_mqtt(payload: bytes) -> Any:
    """Decode an MQTT payload: JSON to a value, else a best-effort string."""
    try:
        return json.loads(payload.decode())
    except Exception:
        try:
            return payload.decode(errors="replace")
        except Exception:
            return payload


def _answers(msg: Any, correlation: bytes) -> bool:
    """True if ``msg`` is the reply to the request sent with ``correlation``. A
    reply without Correlation Data comes from a responder that does not copy it
    back, and is taken as the answer."""
    got = getattr(getattr(msg, "properties", None), "CorrelationData", None)
    return got is None or got == correlation


# PUBACK reason 0x10: the broker accepted the message and no subscription matched.
_NO_MATCHING_SUBSCRIBERS = 0x10
# A reason code of 0x80 or more is a failure (MQTT 5.0, section 2.4).
_FIRST_FAILURE = 0x80


def _carrier(scheme: Any) -> str | None:
    """The MQTT 5 User Property a bearer scheme sends its token under, or None. A
    header bearer scheme with a ``name`` sends the token with every message under
    that name; one without keeps the token as the connection password."""
    if getattr(scheme, "scheme", None) != "bearer" or getattr(scheme, "in_", "header") != "header":
        return None
    name = (getattr(scheme, "raw", None) or {}).get("name")
    return name if isinstance(name, str) and name else None


@dataclass(frozen=True)
class MqttTls:
    """TLS for an MQTT connection. ``ca_certs`` names the CA file that verifies the
    broker; without it the system's CA store does. ``certfile`` and ``keyfile`` name
    a client certificate and its key, for a broker that asks for one. The broker's
    certificate and hostname are always verified."""

    ca_certs: str | None = None
    certfile: str | None = None
    keyfile: str | None = None

    def apply(self, client: Any, keyfile_password: str | None = None) -> None:
        """Set this TLS on a paho client, before it connects."""
        client.tls_set(
            ca_certs=self.ca_certs,
            certfile=self.certfile,
            keyfile=self.keyfile,
            cert_reqs=ssl.CERT_REQUIRED,
            keyfile_password=keyfile_password,
        )
        client.tls_insecure_set(False)


def _connack_ok(rc: Any) -> bool:
    """True if a CONNACK reason code means success, across paho v1 (int 0) and
    v2 (a ReasonCode whose ``value`` is 0)."""
    if rc == 0:
        return True
    return bool(getattr(rc, "value", None) == 0)


@implements(ProtocolBinding)
class MqttBinding(AuthMixin):
    """The MQTT transport, on ``paho-mqtt``. The form's ``href`` is
    ``mqtt://broker[:port]/<topic>``, or ``mqtts://`` for MQTT over TLS, on port
    8883 unless the href names one; ``tls`` (an :class:`MqttTls`) puts every
    connection on TLS. A request/reply ``invoke`` is an MQTT 5 request that names
    ``<topic>/reply`` as its Response Topic and takes the reply carrying its
    Correlation Data. Auth resolves through :class:`AuthMixin` and
    applies via ``apply_mqtt``; a token becomes the password.

    The connect is retried with backoff, ops default to QoS 1, and the
    subscription is re-established on every reconnect, because paho does not
    resubscribe for you. Pass ``client_factory`` to supply your own client.

    Implements ``invoke``, ``read`` and ``subscribe``. Property ``write``, bulk
    ops, and the async action lifecycle do not map cleanly to pub/sub; a
    long-running action falls back to a plain ``invoke`` that returns the reply,
    not an ``ActionStatus`` handle to poll or cancel.

    A URI variable typed ``MQTT_USER_NAME`` is set to the User Name this binding
    connects with, so the topic names its sender. A call is refused before anything
    is sent when its connection has no User Name that can be one topic level.
    """

    scheme = "mqtt"
    schemes = ("mqtt", "mqtts")

    def __init__(
        self,
        *,
        broker: str | None = None,
        timeout: float = 10.0,
        credentials: dict | None = None,
        allow_insecure_oauth: bool = False,
        auth: AuthRegistry | None = None,
        extra_auth: list[AuthStrategy] | None = None,
        qos: int = 1,
        client_id: str | None = None,
        clean_session: bool | None = None,
        connect_retries: int = 3,
        backoff: float = 0.2,
        connect_timeout: float = 10.0,
        client_factory: Any = None,
        tls: MqttTls | None = None,
    ) -> None:
        self._broker = broker
        self._tls = tls
        self._init_auth(
            credentials=credentials,
            auth=auth,
            extra_auth=extra_auth,
            timeout=timeout,
            allow_insecure_oauth=allow_insecure_oauth,
        )
        self._qos = qos
        self._client_id = client_id
        # A persistent session (clean_session=False) lets the broker queue QoS-1
        # messages while disconnected, but needs a stable client id. Default:
        # persistent when an id is given, clean otherwise.
        self._clean_session = (client_id is None) if clean_session is None else clean_session
        self._connect_timeout = connect_timeout
        self._client_factory = client_factory
        self._connect_policy = RetryPolicy(retries=connect_retries, backoff=backoff)
        # Thing id -> (Thing level uriVariables, @context prefixes), to find the
        # variable typed MQTT_USER_NAME.
        self._thing_scopes: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
        # The client of each open subscription, for aclose.
        self._subscriptions: set[Any] = set()

    def _register(self, thing: Any) -> None:
        super()._register(thing)
        tid = getattr(thing, "id", None)
        if tid is not None:
            self._thing_scopes[tid] = (
                dict(getattr(thing, "uri_variables", None) or {}),
                dict(getattr(thing, "context_prefixes", None) or {}),
            )

    async def _with_user_name(self, target: Any, form: WoTForm, method: str) -> WoTForm:
        """``form`` with each URI variable typed ``MQTT_USER_NAME`` set, verbatim, to
        the User Name this binding connects with (MQTT 5.0, section 3.1.3.5). Raises
        TransportError before connecting when the connection has no User Name that
        can be one topic level, or when an argument already filled the variable."""
        template = (getattr(form, "raw", None) or {}).get("href") or form.href
        named = _href_var_names(template)
        if not named:
            return form
        owner = getattr(target, "thing_id", None)
        thing_vars, prefixes = self._thing_scopes.get(owner or "", ({}, {}))
        # An affordance's own declaration wins over the Thing's (TD 1.1, 5.3.1.2).
        declared = {**thing_vars, **(getattr(target, "uri_variables", None) or {})}
        mine = {name for name in named if is_mqtt_user_name(declared.get(name), prefixes)}
        if not mine:
            return form
        if mine - _href_var_names(form.href):
            detail = "an argument took the place of the MQTT User Name the topic names"
            raise TransportError(method, form.href, detail=detail)
        user = apply_mqtt(await self._connection_credentials(owner, form)).username
        if not _usable_user_name(user):
            detail = (
                "the topic names its sender by MQTT User Name, and this connection "
                "has none that can be one topic level"
            )
            raise TransportError(method, form.href, detail=detail)
        # Simple expansion would percent encode a name such as a@b; reserved
        # expansion (RFC 6570, section 3.2.3) keeps it as the broker spells it.
        href = form.href
        for name in mine:
            href = href.replace(f"{{{name}}}", f"{{+{name}}}")
        return _filled_form(replace(form, href=href), dict.fromkeys(mine, user))[0]

    def _new_client(self, v5: bool = False) -> Any:
        """A paho client that works across paho-mqtt 1.x and 2.x (2.x requires
        an explicit callback API version). Uses MQTT v5 when ``v5`` is set, for
        enhanced authentication and request/reply, both v5 features."""
        if self._client_factory is not None:
            return self._client_factory()
        # optional dep, kept local so the core imports without the extra
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        cid = self._client_id or ""
        version = getattr(mqtt, "CallbackAPIVersion", None)
        # paho-mqtt >= 2.0 requires an explicit callback API version as the first
        # positional arg; 1.x has no such parameter. Pass it by keyword only when
        # present so there is no positional collision with client_id/protocol.
        # An MQTT 5 client takes VERSION2 callbacks, the only shape whose on_publish
        # carries the PUBACK reason code. on_connect and on_message read the same
        # under both.
        api = None if version is None else (version.VERSION2 if v5 else version.VERSION1)
        ver_kw = {"callback_api_version": api} if api is not None else {}
        if v5:
            # MQTT v5 has no clean_session (it uses a per-connect clean_start).
            client = mqtt.Client(**ver_kw, client_id=cid, protocol=mqtt.MQTTv5)
        else:
            client = mqtt.Client(**ver_kw, client_id=cid, clean_session=self._clean_session)
        # Bound the reconnect backoff paho applies when a live connection drops.
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        return client

    def _tls_for(self, form: WoTForm | None, cert: ClientCertificate | None) -> MqttTls | None:
        """The TLS a connection uses: the ``tls`` option's, else the system CA
        store's for an ``mqtts`` href, with a client certificate credential's files
        in place of its own. None for a plain connection."""
        mqtts = form is not None and urllib.parse.urlparse(form.href).scheme == "mqtts"
        tls = self._tls or (MqttTls() if mqtts else None)
        if cert is None:
            return tls
        ca_certs = cert.ca_certs or (tls.ca_certs if tls else None)
        return MqttTls(ca_certs=ca_certs, certfile=cert.certfile, keyfile=cert.keyfile)

    @staticmethod
    def _configure_client(client: Any, plan: MqttAuthPlan, tls: MqttTls | None) -> None:
        """Apply a plan's username and password, and ``tls`` with the key password
        of the plan's client certificate. (Enhanced auth, being a v5 CONNECT
        property, is handled at connect time.)"""
        if plan.username is not None:
            client.username_pw_set(plan.username, plan.password)
        elif plan.password is not None:
            client.username_pw_set("", plan.password)  # token-as-password
        if tls is not None:
            password = plan.tls.password if plan.tls is not None else None
            tls.apply(client, password.get_secret_value() if password is not None else None)

    @staticmethod
    def _connect_properties(plan: MqttAuthPlan) -> Any:
        """The MQTT v5 CONNECT properties carrying enhanced authentication
        (``AuthenticationMethod`` + ``AuthenticationData``), or ``None``."""
        if plan.enhanced is None:
            return None
        # optional dep, kept local so the core imports without the extra
        from paho.mqtt.packettypes import PacketTypes  # noqa: PLC0415
        from paho.mqtt.properties import Properties  # noqa: PLC0415

        props = Properties(PacketTypes.CONNECT)
        props.AuthenticationMethod = plan.enhanced.method
        if plan.enhanced.data:
            props.AuthenticationData = plan.enhanced.data.get_secret_bytes()
        return props

    def _request_properties(
        self, reply_topic: str, correlation: bytes, caller: list[tuple[str, str]] | None = None
    ) -> Any:
        """The MQTT v5 PUBLISH properties that make a message a request: the
        Response Topic to answer on, the Correlation Data to answer with, the
        caller's User Properties, and a Message Expiry Interval of the reply timeout,
        so the broker drops a request nobody took in time instead of delivering it
        late."""
        # optional dep, kept local so the core imports without the extra
        from paho.mqtt.packettypes import PacketTypes  # noqa: PLC0415
        from paho.mqtt.properties import Properties  # noqa: PLC0415

        props = Properties(PacketTypes.PUBLISH)
        props.ResponseTopic = reply_topic
        props.CorrelationData = correlation
        props.MessageExpiryInterval = max(1, math.ceil(self._timeout))
        if caller:
            props.UserProperty = list(caller)
        return props

    @staticmethod
    def _caller_only(caller: list[tuple[str, str]]) -> Any:
        """PUBLISH properties that carry only the caller's User Properties."""
        # optional dep, kept local so the core imports without the extra
        from paho.mqtt.packettypes import PacketTypes  # noqa: PLC0415
        from paho.mqtt.properties import Properties  # noqa: PLC0415

        props = Properties(PacketTypes.PUBLISH)
        props.UserProperty = list(caller)
        return props

    @staticmethod
    def _fail_when_unserved(
        client: Any, fut: asyncio.Future[Any], method: str, url: str, topic: str
    ) -> None:
        """Fail ``fut`` as soon as the PUBACK reports no matching subscriber or a
        refusal, since no reply can follow; the error's ``status`` is the PUBACK
        reason code. paho's VERSION2 on_publish passes the reason code after the
        message id; without one, nothing fails here."""
        loop = asyncio.get_running_loop()

        def _fail(code: int, detail: str) -> None:
            if not fut.done():
                fut.set_exception(TransportError(method, url, status=code, detail=detail))

        def _on_publish(_c: Any, _u: Any, _mid: Any, *reason: Any) -> None:
            code = getattr(reason[0], "value", None) if reason else None
            if code == _NO_MATCHING_SUBSCRIBERS:
                loop.call_soon_threadsafe(
                    _fail, code, f"no subscriber on {topic}, so nothing serves it"
                )
            elif isinstance(code, int) and code >= _FIRST_FAILURE:
                loop.call_soon_threadsafe(_fail, code, f"the broker refused the publish on {topic}")

        client.on_publish = _on_publish

    def _endpoint(self, form: WoTForm, fallback: str) -> tuple[str, int, str]:
        # An MQTT topic filter may contain '#' (multi-level wildcard) and '+', both
        # legal, common last characters. urlparse would read '#' as a URL fragment
        # and drop the wildcard, so parse the authority with urlparse but take the
        # topic straight from the raw href after "host[:port]/", fragment intact.
        u = urllib.parse.urlparse(form.href)
        host = self._broker or u.hostname or "localhost"
        # IANA registers 8883 for MQTT over TLS and 1883 for MQTT.
        port = u.port or (8883 if u.scheme == "mqtts" else 1883)
        href = form.href
        scheme_sep = href.find("://")
        rest = href[scheme_sep + 3 :] if scheme_sep != -1 else href
        slash = rest.find("/")
        topic = rest[slash + 1 :] if slash != -1 else ""
        # Strip a query string if one was appended (MQTT topics carry no query),
        # but never strip '#': it is the wildcard, not a fragment.
        q = topic.find("?")
        if q != -1:
            topic = topic[:q]
        topic = topic or fallback
        return host, port, topic

    def _split_security(
        self, owner_id: str | None, form: WoTForm | None
    ) -> tuple[tuple, dict[str, str], dict, str | None]:
        """The active schemes that authenticate the connection, the bearer schemes
        that travel with each message (by the User Property each names), the
        scheme definitions, and the owner's slug."""
        active, schemes, slug = self._resolve(owner_id, form)
        carried = {s: name for s in active if (name := _carrier(schemes.get(s)))}
        return tuple(s for s in active if s not in carried), carried, schemes, slug

    async def _scheme_credentials(
        self, owner_id: str | None, slug: str | None, active: tuple, schemes: dict
    ) -> list[Credential]:
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

    async def _connection_credentials(
        self, owner_id: str | None, form: WoTForm | None
    ) -> list[Credential]:
        connection, _, schemes, slug = self._split_security(owner_id, form)
        return await self._scheme_credentials(owner_id, slug, connection, schemes)

    async def _caller_properties(
        self, owner_id: str | None, form: WoTForm | None
    ) -> list[tuple[str, str]]:
        """The User Properties that carry the caller's bearer token with a message,
        for a responder that authorizes each message rather than the connection."""
        _, carried, schemes, slug = self._split_security(owner_id, form)
        pairs: list[tuple[str, str]] = []
        for sname, name in carried.items():
            creds = await self._scheme_credentials(owner_id, slug, (sname,), schemes)
            pairs.extend(
                (name, f"{c.scheme} {c.token.get_secret_value()}")
                for c in creds
                if isinstance(c, BearerToken)
            )
        return pairs

    async def _apply_auth(
        self, client: Any, owner_id: str | None, form: WoTForm | None = None
    ) -> MqttAuthPlan:
        """Configure an existing client's connection auth from the owner's
        credentials. Returns the ``MqttAuthPlan`` for inspection/testing."""
        plan = apply_mqtt(await self._connection_credentials(owner_id, form))
        self._configure_client(client, plan, self._tls_for(form, plan.tls))
        return plan

    async def _connect(
        self,
        owner_id: str | None,
        host: str,
        port: int,
        form: WoTForm | None = None,
        *,
        v5: bool = False,
    ) -> tuple[Any, Any]:
        """Resolve the owner's credentials, build a client of the right protocol,
        and configure its connection auth. Returns ``(client, properties)`` ready
        to connect. All auth comes from the shared, transport-neutral layer. A
        form's own security overrides the owner's for that affordance. ``v5``
        asks for MQTT v5, which enhanced authentication also implies. A bearer
        token that travels with each message is left out of the connection."""
        plan = apply_mqtt(await self._connection_credentials(owner_id, form))
        client = self._new_client(v5=v5 or plan.enhanced is not None)
        self._configure_client(client, plan, self._tls_for(form, plan.tls))
        return client, self._connect_properties(plan)

    async def _establish(
        self, client: Any, host: str, port: int, *, topics: list[str], props: Any = None
    ) -> None:
        """Connect with retry/backoff and (re)subscribe to ``topics`` on every
        successful (re)connection, then wait for CONNACK. Raises TransportError
        if it cannot connect within the retry budget."""

        loop = asyncio.get_running_loop()
        policy = self._connect_policy
        connect_kwargs = {"properties": props} if props else {}
        for attempt in range(policy.retries + 1):
            # A fresh event per attempt so a late CONNACK from a previous,
            # abandoned attempt can never satisfy this one's wait.
            connected = asyncio.Event()

            def _on_connect(
                _c: Any, _u: Any, _flags: Any, rc: Any, *_args: Any, _ev: asyncio.Event = connected
            ) -> None:  # paho v1+v2
                if _connack_ok(rc):
                    # paho does not resubscribe after a reconnect; do it here so a
                    # dropped connection transparently restores the subscription.
                    for t in topics:
                        client.subscribe(t, qos=self._qos)
                    loop.call_soon_threadsafe(_ev.set)

            client.on_connect = _on_connect
            try:
                client.connect(host, port, **connect_kwargs)
                client.loop_start()
                await asyncio.wait_for(connected.wait(), timeout=self._connect_timeout)
            except Exception as exc:
                # Tear the attempt down fully (stop the loop and close the socket)
                # before retrying or giving up, so no connection leaks.
                await self._shutdown(client)
                if attempt < policy.retries:
                    await asyncio.sleep(policy.delay(attempt))
                    continue
                raise TransportError(
                    "CONNECT", f"mqtt://{host}:{port}", attempts=attempt + 1, cause=exc
                ) from exc
            else:
                return

    @staticmethod
    async def _shutdown(client: Any) -> None:
        # DISCONNECT first, so the network thread ends; the join then runs off the
        # event loop, which keeps serving while it waits.
        with contextlib.suppress(Exception):
            client.disconnect()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(client.loop_stop)

    async def invoke(self, action: WoTAction, form: WoTForm, arguments: dict[str, Any]) -> Any:
        """Publish to the form topic. When the action declares an ``output``
        schema, or the call carries the caller's token for a responder that checks
        each message, send an MQTT 5 request and await its reply on
        ``<topic>/reply``. Otherwise fire and forget: publish, wait for PUBACK at
        QoS >= 1, return ``{"ok": True}`` without subscribing for a reply."""

        form = await self._with_user_name(action, form, "PUBLISH")
        host, port, topic = self._endpoint(form, getattr(action, "name", "action"))
        owner = getattr(action, "thing_id", None)
        caller = await self._caller_properties(owner, form)
        # A responder that authorizes each message always answers, refusals
        # included, so its answer is awaited.
        expect_reply = bool(getattr(action, "output_schema", None)) or bool(caller)
        reply_topic = f"{topic}/reply"
        correlation = uuid.uuid4().bytes
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] | None = loop.create_future() if expect_reply else None
        client, props = await self._connect(
            owner, host, port, form, v5=expect_reply or bool(caller)
        )

        if fut is not None:

            def _on_message(_c: Any, _u: Any, msg: Any) -> None:
                if not fut.done() and _answers(msg, correlation):
                    loop.call_soon_threadsafe(fut.set_result, _decode_mqtt(msg.payload))

            client.on_message = _on_message
            self._fail_when_unserved(client, fut, "PUBLISH", f"mqtt://{host}:{port}/{topic}", topic)

        # Retain: call-time ``retain`` wins over the form's ``mqv:retain``.
        retain = False
        body = arguments
        if isinstance(arguments, dict) and "retain" in arguments:
            body = {k: v for k, v in arguments.items() if k != "retain"}
            retain = bool(arguments.get("retain"))
        elif form.raw.get("mqv:retain") is not None:
            retain = bool(form.raw.get("mqv:retain"))

        try:
            topics = [reply_topic] if expect_reply else []
            await self._establish(client, host, port, topics=topics, props=props)
            publish_kwargs: dict[str, Any] = {}
            if expect_reply:
                properties = self._request_properties(reply_topic, correlation, caller)
                publish_kwargs = {"properties": properties}
            elif caller:
                publish_kwargs = {"properties": self._caller_only(caller)}
            info = client.publish(
                topic, json.dumps(body), qos=self._qos, retain=retain, **publish_kwargs
            )
            # At QoS >= 1, confirm the broker stored the publish (PUBACK) before
            # waiting on a reply (or returning), so a dropped publish is not
            # mistaken for a slow device. wait_for_publish blocks, so run it off
            # the event loop.
            wait_pub = getattr(info, "wait_for_publish", None)
            if self._qos and callable(wait_pub):
                await loop.run_in_executor(None, lambda: wait_pub(self._timeout))
            # fut is created iff expect_reply (see above); testing it directly
            # both returns the no-reply result and narrows the reply path.
            if fut is None:
                return {"ok": True, "topic": topic}
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise TransportError(
                "PUBLISH",
                f"mqtt://{host}:{port}/{topic}",
                detail=f"no reply on {reply_topic} within {self._timeout}s",
                cause=exc,
            ) from exc
        finally:
            await self._shutdown(client)

    async def read(self, prop: WoTProperty, form: WoTForm) -> Any:
        """Read a property: the value the broker retains on the topic when the form
        has ``mqv:retain``, otherwise the reply on ``<topic>/reply`` to an empty
        MQTT 5 request, the shape the MQTT gateway serves."""

        form = await self._with_user_name(prop, form, "READ")
        host, port, topic = self._endpoint(form, getattr(prop, "name", "property"))
        retained = bool(form.raw.get("mqv:retain"))
        listen = topic if retained else f"{topic}/reply"
        correlation = uuid.uuid4().bytes
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        owner = getattr(prop, "thing_id", None)
        caller = [] if retained else await self._caller_properties(owner, form)
        client, props = await self._connect(owner, host, port, form, v5=not retained)

        def _on_message(_c: Any, _u: Any, msg: Any) -> None:
            if not fut.done() and (retained or _answers(msg, correlation)):
                loop.call_soon_threadsafe(fut.set_result, _decode_mqtt(msg.payload))

        client.on_message = _on_message
        if not retained:
            self._fail_when_unserved(client, fut, "READ", f"mqtt://{host}:{port}/{topic}", topic)
        try:
            await self._establish(client, host, port, topics=[listen], props=props)
            if not retained:
                request = self._request_properties(listen, correlation, caller)
                client.publish(topic, "", qos=self._qos, properties=request)
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise TransportError(
                "READ",
                f"mqtt://{host}:{port}/{topic}",
                detail=f"no value on {listen} within {self._timeout}s",
                cause=exc,
            ) from exc
        finally:
            await self._shutdown(client)

    async def subscribe(
        self, target: Any, form: WoTForm, args: dict[str, Any] | None = None
    ) -> AsyncIterator[Any]:
        """Subscribe to the form's MQTT topic; yield each message. This is the
        events / observable-property binding for MQTT: a long-lived subscription
        that survives broker reconnects (the topic is re-subscribed on every
        reconnect). ``target`` is the affordance, so the connection
        authenticates as its owner."""

        name = target if isinstance(target, str) else target.name
        owner = getattr(target, "thing_id", None)
        form = await self._with_user_name(target, form, "SUBSCRIBE")
        host, port, topic = self._endpoint(form, name)
        queue: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        client, props = await self._connect(owner, host, port, form)

        def _on_message(_c: Any, _u: Any, msg: Any) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, _decode_mqtt(msg.payload))

        client.on_message = _on_message
        try:
            await self._establish(client, host, port, topics=[topic], props=props)
        except BaseException:
            await self._shutdown(client)
            raise
        self._subscriptions.add(client)

        async def _stream() -> AsyncIterator[Any]:
            try:
                while True:
                    yield await queue.get()
            finally:
                self._subscriptions.discard(client)
                await self._shutdown(client)

        return _stream()

    async def aclose(self) -> None:
        """Disconnect every open subscription, including one whose stream was never
        read."""
        # Closing an async generator before its first item skips its finally block,
        # so a stream nobody read would keep its connection without this.
        for client in list(self._subscriptions):
            await self._shutdown(client)
        self._subscriptions.clear()
