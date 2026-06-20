"""MqttInvoker: drive a Thing over mqtt (publish + await a reply)."""

from __future__ import annotations

import json

from thingctx.auth import AuthRegistry, AuthStrategy, apply_mqtt
from thingctx.invokers.base import _AuthBinding


class MqttInvoker(_AuthBinding):
    """Publish the action input to the form's mqtt topic, await a reply.

    Built on ``paho-mqtt``. The form's ``href`` is ``mqtt://broker[:port]/<topic>``;
    the reply is awaited on ``<topic>/reply``.

    Authentication is the *same* transport-neutral layer the HTTP invoker uses:
    bind resources with ``with_security``/``with_things`` and pass
    ``credentials``; the shared primitive resolves them into neutral material and
    ``apply_mqtt`` maps it onto the CONNECT (username/password, mutual TLS, or
    v5 enhanced auth). A username/password scheme becomes username/password; a
    token becomes the password (token-as-password).
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
    ) -> None:
        self._broker = broker
        self._init_auth(
            credentials=credentials,
            auth=auth,
            extra_auth=extra_auth,
            timeout=timeout,
            allow_insecure_oauth=allow_insecure_oauth,
        )

    @staticmethod
    def _new_client(enhanced: bool = False):
        """A paho client that works across paho-mqtt 1.x and 2.x (2.x requires
        an explicit callback API version). Uses MQTT v5 when ``enhanced`` auth
        is in play, since enhanced authentication is a v5 feature."""
        import paho.mqtt.client as mqtt  # type: ignore

        kwargs = {"protocol": mqtt.MQTTv5} if enhanced else {}
        version = getattr(mqtt, "CallbackAPIVersion", None)
        if version is not None:  # paho-mqtt >= 2.0
            return mqtt.Client(version.VERSION1, **kwargs)
        return mqtt.Client(**kwargs)

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
        (``AuthenticationMethod`` + ``AuthenticationData``), or ``None``. The
        method names the mechanism; the data is the initial token/challenge."""
        if plan.enhanced is None:
            return None
        from paho.mqtt.packettypes import PacketTypes  # type: ignore
        from paho.mqtt.properties import Properties  # type: ignore

        props = Properties(PacketTypes.CONNECT)
        props.AuthenticationMethod = plan.enhanced.method
        if plan.enhanced.data:
            props.AuthenticationData = plan.enhanced.data.get_secret_bytes()
        return props

    async def _apply_auth(self, client, owner_id: str | None):
        """Configure an existing client's connection auth from the owner's
        credentials. Returns the ``MqttAuthPlan`` for inspection/testing."""
        plan = apply_mqtt(await self._resolve_credentials(owner_id))
        self._configure_client(client, plan)
        return plan

    async def _connect(self, owner_id: str | None, host: str, port: int):
        """Resolve the owner's credentials, build a client of the right
        protocol, configure it, and return ``(client, properties)`` ready to
        connect. All auth comes from the shared, transport-neutral layer."""
        plan = apply_mqtt(await self._resolve_credentials(owner_id))
        client = self._new_client(enhanced=plan.enhanced is not None)
        self._configure_client(client, plan)
        return client, self._connect_properties(plan)

    async def invoke(self, action, form, arguments):  # noqa: ANN001
        import asyncio
        import urllib.parse

        u = urllib.parse.urlparse(form.href)
        host = self._broker or u.hostname or "localhost"
        port = u.port or 1883
        topic = u.path.lstrip("/") or action.name
        reply_topic = f"{topic}/reply"

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()

        client, props = await self._connect(getattr(action, "thing_id", None), host, port)

        def _on_message(_c, _u, msg):  # noqa: ANN001
            try:
                payload = json.loads(msg.payload.decode())
            except Exception:  # noqa: BLE001
                payload = msg.payload.decode(errors="replace")
            if not fut.done():
                loop.call_soon_threadsafe(fut.set_result, payload)

        client.on_message = _on_message
        client.connect(host, port, **({"properties": props} if props else {}))
        client.subscribe(reply_topic)
        client.loop_start()
        try:
            client.publish(topic, json.dumps(arguments))
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            return {"error": f"mqtt reply timeout on {reply_topic}"}
        finally:
            client.loop_stop()
            client.disconnect()

    async def subscribe(self, name, form):  # noqa: ANN001
        """Subscribe to the form's MQTT topic; yield each message. This is the
        events / observable-property binding for MQTT: a long-lived
        subscription, not a request/reply."""
        import asyncio
        import urllib.parse

        u = urllib.parse.urlparse(form.href)
        host = self._broker or u.hostname or "localhost"
        port = u.port or 1883
        topic = u.path.lstrip("/") or name

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        client, props = await self._connect(getattr(form, "thing_id", None), host, port)

        def _on_message(_c, _u, msg):  # noqa: ANN001
            try:
                payload = json.loads(msg.payload.decode())
            except Exception:  # noqa: BLE001
                payload = msg.payload.decode(errors="replace")
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        client.on_message = _on_message
        client.connect(host, port, **({"properties": props} if props else {}))
        client.subscribe(topic)
        client.loop_start()

        async def _stream():
            try:
                while True:
                    yield await queue.get()
            finally:
                client.loop_stop()
                client.disconnect()

        return _stream()
