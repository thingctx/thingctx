# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""MqttBinding: drive a Thing over mqtt (publish + await a reply)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.parse
from typing import TYPE_CHECKING, Any

from thingctx.auth import AuthRegistry, AuthStrategy, apply_mqtt
from thingctx.bindings.base import AuthMixin, ProtocolBinding
from thingctx.contracts import implements
from thingctx.reliability import RetryPolicy, TransportError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from thingctx.auth.mqtt import MqttAuthPlan
    from thingctx.thing import WoTAction, WoTForm


def _decode_mqtt(payload: bytes) -> Any:
    """Decode an MQTT payload: JSON to a value, else a best-effort string."""
    try:
        return json.loads(payload.decode())
    except Exception:
        try:
            return payload.decode(errors="replace")
        except Exception:
            return payload


def _connack_ok(rc: Any) -> bool:
    """True if a CONNACK reason code means success, across paho v1 (int 0) and
    v2 (a ReasonCode whose ``value`` is 0)."""
    if rc == 0:
        return True
    return bool(getattr(rc, "value", None) == 0)


@implements(ProtocolBinding)
class MqttBinding(AuthMixin):
    """The MQTT transport, on ``paho-mqtt``. The form's ``href`` is
    ``mqtt://broker[:port]/<topic>``; a request/reply ``invoke`` awaits the reply
    on ``<topic>/reply``. Auth resolves through :class:`AuthMixin` and applies via
    ``apply_mqtt``; a token becomes the password.

    The connect is retried with backoff, ops default to QoS 1, and the
    subscription is re-established on every reconnect, because paho does not
    resubscribe for you. Pass ``client_factory`` to supply your own client.

    Implements ``invoke`` and ``subscribe`` only. Property ``read`` / ``write``,
    bulk ops, and the async action lifecycle do not map cleanly to pub/sub; a
    long-running action falls back to a plain ``invoke`` that returns the reply,
    not an ``ActionStatus`` handle to poll or cancel.
    """

    scheme = "mqtt"

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
    ) -> None:
        self._broker = broker
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

    def _new_client(self, enhanced: bool = False) -> Any:
        """A paho client that works across paho-mqtt 1.x and 2.x (2.x requires
        an explicit callback API version). Uses MQTT v5 when ``enhanced`` auth
        is in play, since enhanced authentication is a v5 feature."""
        if self._client_factory is not None:
            return self._client_factory()
        # optional dep, kept local so the core imports without the extra
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        cid = self._client_id or ""
        version = getattr(mqtt, "CallbackAPIVersion", None)
        # paho-mqtt >= 2.0 requires an explicit callback API version as the first
        # positional arg; 1.x has no such parameter. Pass it by keyword only when
        # present so there is no positional collision with client_id/protocol.
        ver_kw = {"callback_api_version": version.VERSION1} if version is not None else {}
        if enhanced:
            # MQTT v5 has no clean_session (it uses a per-connect clean_start).
            client = mqtt.Client(**ver_kw, client_id=cid, protocol=mqtt.MQTTv5)
        else:
            client = mqtt.Client(**ver_kw, client_id=cid, clean_session=self._clean_session)
        # Bound the reconnect backoff paho applies when a live connection drops.
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        return client

    @staticmethod
    def _configure_client(client: Any, plan: MqttAuthPlan) -> None:
        """Apply connection-level auth from a plan: username/password and mTLS.
        (Enhanced auth, being a v5 CONNECT property, is handled at connect time.)"""
        if plan.username is not None:
            client.username_pw_set(plan.username, plan.password)
        elif plan.password is not None:
            client.username_pw_set("", plan.password)  # token-as-password
        if plan.tls is not None:
            client.tls_set(
                ca_certs=plan.tls.ca_certs,
                certfile=plan.tls.certfile,
                keyfile=plan.tls.keyfile,
            )

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

    def _endpoint(self, form: WoTForm, fallback: str) -> tuple[str, int, str]:
        # An MQTT topic filter may contain '#' (multi-level wildcard) and '+', both
        # legal, common last characters. urlparse would read '#' as a URL fragment
        # and drop the wildcard, so parse the authority with urlparse but take the
        # topic straight from the raw href after "host[:port]/", fragment intact.
        u = urllib.parse.urlparse(form.href)
        host = self._broker or u.hostname or "localhost"
        port = u.port or 1883
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

    async def _apply_auth(
        self, client: Any, owner_id: str | None, form: WoTForm | None = None
    ) -> MqttAuthPlan:
        """Configure an existing client's connection auth from the owner's
        credentials. Returns the ``MqttAuthPlan`` for inspection/testing."""
        plan = apply_mqtt(await self._resolve_credentials(owner_id, form))
        self._configure_client(client, plan)
        return plan

    async def _connect(
        self, owner_id: str | None, host: str, port: int, form: WoTForm | None = None
    ) -> tuple[Any, Any]:
        """Resolve the owner's credentials, build a client of the right protocol,
        and configure its connection auth. Returns ``(client, properties)`` ready
        to connect. All auth comes from the shared, transport-neutral layer. A
        form's own security overrides the owner's for that affordance."""
        plan = apply_mqtt(await self._resolve_credentials(owner_id, form))
        client = self._new_client(enhanced=plan.enhanced is not None)
        self._configure_client(client, plan)
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
                self._shutdown(client)
                if attempt < policy.retries:
                    await asyncio.sleep(policy.delay(attempt))
                    continue
                raise TransportError(
                    "CONNECT", f"mqtt://{host}:{port}", attempts=attempt + 1, cause=exc
                ) from exc
            else:
                return

    @staticmethod
    def _shutdown(client: Any) -> None:
        for step in ("loop_stop", "disconnect"):
            with contextlib.suppress(Exception):
                getattr(client, step)()

    async def invoke(self, action: WoTAction, form: WoTForm, arguments: dict[str, Any]) -> Any:
        """Publish to the form topic. When the action declares an ``output``
        schema, await a reply on ``<topic>/reply`` (request/response). When it
        does not, fire-and-forget: publish, wait for PUBACK at QoS >= 1, return
        ``{"ok": True}`` without subscribing for a reply."""

        host, port, topic = self._endpoint(form, getattr(action, "name", "action"))
        expect_reply = bool(getattr(action, "output_schema", None))
        reply_topic = f"{topic}/reply"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] | None = loop.create_future() if expect_reply else None
        client, props = await self._connect(getattr(action, "thing_id", None), host, port, form)

        if expect_reply:

            def _on_message(_c: Any, _u: Any, msg: Any) -> None:
                payload = _decode_mqtt(msg.payload)
                if fut is not None and not fut.done():
                    loop.call_soon_threadsafe(fut.set_result, payload)

            client.on_message = _on_message

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
            info = client.publish(topic, json.dumps(body), qos=self._qos, retain=retain)
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
            self._shutdown(client)

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
            self._shutdown(client)
            raise

        async def _stream() -> AsyncIterator[Any]:
            try:
                while True:
                    yield await queue.get()
            finally:
                self._shutdown(client)

        return _stream()
