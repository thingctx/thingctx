"""MqttInvoker: drive a Thing over mqtt (publish + await a reply)."""

from __future__ import annotations

import json

from thingctx.auth import AuthRegistry, AuthStrategy, apply_mqtt
from thingctx.invokers.base import _AuthBinding


def _decode_mqtt(payload):
    """Decode an MQTT payload: JSON to a value, else a best-effort string."""
    try:
        return json.loads(payload.decode())
    except Exception:  # noqa: BLE001 - non-JSON payloads fall back to text
        try:
            return payload.decode(errors="replace")
        except Exception:  # noqa: BLE001
            return payload


def _connack_ok(rc) -> bool:
    """True if a CONNACK reason code means success, across paho v1 (int 0) and
    v2 (a ReasonCode whose ``value`` is 0)."""
    if rc == 0:
        return True
    return getattr(rc, "value", None) == 0


class MqttInvoker(_AuthBinding):
    """Publish the action input to the form's mqtt topic, await a reply.

    Built on ``paho-mqtt``. The form's ``href`` is ``mqtt://broker[:port]/<topic>``;
    a request/reply ``invoke`` awaits the reply on ``<topic>/reply``.

    Authentication is the *same* transport-neutral layer the HTTP invoker uses:
    bind resources with ``with_security``/``with_things`` and pass
    ``credentials``; the shared primitive resolves them into neutral material and
    ``apply_mqtt`` maps it onto the CONNECT (username/password, mutual TLS, or v5
    enhanced auth). A username/password scheme becomes username/password; a token
    becomes the password (token-as-password).

    Reliability is built in: the connect is retried with backoff,
    publishes/subscribes use QoS 1 by default, the subscription is
    **re-established on every reconnect** (paho does not resubscribe for you),
    and connect/reply failures surface as the same ``TransportError`` the HTTP
    invoker raises. Pass ``client_factory`` to supply your own configured client
    (or a fake, in tests).
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
        client_factory=None,
    ) -> None:
        from thingctx.reliability import RetryPolicy

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

    def _new_client(self, enhanced: bool = False):
        """A paho client that works across paho-mqtt 1.x and 2.x (2.x requires
        an explicit callback API version). Uses MQTT v5 when ``enhanced`` auth
        is in play, since enhanced authentication is a v5 feature."""
        if self._client_factory is not None:
            return self._client_factory()
        import paho.mqtt.client as mqtt  # type: ignore

        cid = self._client_id or ""
        version = getattr(mqtt, "CallbackAPIVersion", None)
        args = (version.VERSION1,) if version is not None else ()  # paho-mqtt >= 2.0
        if enhanced:
            # MQTT v5 has no clean_session (it uses a per-connect clean_start).
            client = mqtt.Client(*args, client_id=cid, protocol=mqtt.MQTTv5)
        else:
            client = mqtt.Client(*args, client_id=cid, clean_session=self._clean_session)
        # Bound the reconnect backoff paho applies when a live connection drops.
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        return client

    @staticmethod
    def _configure_client(client, plan) -> None:
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
    def _connect_properties(plan):
        """The MQTT v5 CONNECT properties carrying enhanced authentication
        (``AuthenticationMethod`` + ``AuthenticationData``), or ``None``."""
        if plan.enhanced is None:
            return None
        from paho.mqtt.packettypes import PacketTypes  # type: ignore
        from paho.mqtt.properties import Properties  # type: ignore

        props = Properties(PacketTypes.CONNECT)
        props.AuthenticationMethod = plan.enhanced.method
        if plan.enhanced.data:
            props.AuthenticationData = plan.enhanced.data.get_secret_bytes()
        return props

    def _endpoint(self, form, fallback: str):
        import urllib.parse

        u = urllib.parse.urlparse(form.href)
        host = self._broker or u.hostname or "localhost"
        port = u.port or 1883
        topic = u.path.lstrip("/") or fallback
        return host, port, topic

    async def _apply_auth(self, client, owner_id: str | None):
        """Configure an existing client's connection auth from the owner's
        credentials. Returns the ``MqttAuthPlan`` for inspection/testing."""
        plan = apply_mqtt(await self._resolve_credentials(owner_id))
        self._configure_client(client, plan)
        return plan

    async def _connect(self, owner_id: str | None, host: str, port: int):
        """Resolve the owner's credentials, build a client of the right protocol,
        and configure its connection auth. Returns ``(client, properties)`` ready
        to connect -- all auth comes from the shared, transport-neutral layer."""
        plan = apply_mqtt(await self._resolve_credentials(owner_id))
        client = self._new_client(enhanced=plan.enhanced is not None)
        self._configure_client(client, plan)
        return client, self._connect_properties(plan)

    async def _establish(self, client, host, port, *, topics, props=None):
        """Connect with retry/backoff and (re)subscribe to ``topics`` on every
        successful (re)connection, then wait for CONNACK. Raises TransportError
        if it cannot connect within the retry budget."""
        import asyncio

        from thingctx.reliability import TransportError

        loop = asyncio.get_running_loop()
        policy = self._connect_policy
        connect_kwargs = {"properties": props} if props else {}
        for attempt in range(policy.retries + 1):
            # A fresh event per attempt so a late CONNACK from a previous,
            # abandoned attempt can never satisfy this one's wait.
            connected = asyncio.Event()

            def _on_connect(_c, _u, _flags, rc, *_args, _ev=connected):  # paho v1+v2
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
                return
            except Exception as exc:  # noqa: BLE001 - normalize every connect failure
                # Tear the attempt down fully (stop the loop and close the socket)
                # before retrying or giving up, so no connection leaks.
                self._shutdown(client)
                if attempt < policy.retries:
                    await asyncio.sleep(policy.delay(attempt))
                    continue
                raise TransportError(
                    "CONNECT", f"mqtt://{host}:{port}", attempts=attempt + 1, cause=exc
                ) from exc

    @staticmethod
    def _shutdown(client) -> None:
        for step in ("loop_stop", "disconnect"):
            try:
                getattr(client, step)()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                pass

    async def invoke(self, action, form, arguments):  # noqa: ANN001
        import asyncio

        from thingctx.reliability import TransportError

        host, port, topic = self._endpoint(form, getattr(action, "name", "action"))
        reply_topic = f"{topic}/reply"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        client, props = await self._connect(getattr(action, "thing_id", None), host, port)

        def _on_message(_c, _u, msg):  # noqa: ANN001
            payload = _decode_mqtt(msg.payload)
            if not fut.done():
                loop.call_soon_threadsafe(fut.set_result, payload)

        client.on_message = _on_message
        try:
            await self._establish(client, host, port, topics=[reply_topic], props=props)
            info = client.publish(topic, json.dumps(arguments), qos=self._qos)
            # At QoS >= 1, confirm the broker stored the publish (PUBACK) before
            # waiting on a reply, so a dropped publish is not mistaken for a slow
            # device. wait_for_publish blocks, so run it off the event loop.
            wait_pub = getattr(info, "wait_for_publish", None)
            if self._qos and callable(wait_pub):
                await loop.run_in_executor(None, lambda: wait_pub(self._timeout))
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

    async def subscribe(self, name, form):  # noqa: ANN001
        """Subscribe to the form's MQTT topic; yield each message. This is the
        events / observable-property binding for MQTT: a long-lived subscription
        that survives broker reconnects (the topic is re-subscribed on every
        reconnect)."""
        import asyncio

        host, port, topic = self._endpoint(form, name)
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        client, props = await self._connect(getattr(form, "thing_id", None), host, port)

        def _on_message(_c, _u, msg):  # noqa: ANN001
            loop.call_soon_threadsafe(queue.put_nowait, _decode_mqtt(msg.payload))

        client.on_message = _on_message
        try:
            await self._establish(client, host, port, topics=[topic], props=props)
        except BaseException:
            self._shutdown(client)
            raise

        async def _stream():
            try:
                while True:
                    yield await queue.get()
            finally:
                self._shutdown(client)

        return _stream()
