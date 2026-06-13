"""Invokers: one per transport scheme. invoke() picks the invoker whose
scheme matches the form's href.

LocalInvoker: local:// (or no scheme), an in-process callable.
HttpInvoker:  http/https, needs httpx.
MqttInvoker:  mqtt, publish + await a reply, needs paho-mqtt.

A new transport is one more Invoker; the TD is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from thingctx.thing import WoTAction, WoTForm


def _decode(resp, empty=None):
    """Decode an httpx response by its content type: JSON to a value, text
    to a str, anything else (e.g. an image) to raw bytes. An empty body
    returns `empty`."""
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


class LocalInvoker:
    """Invoke actions whose form points at an in-process callable.

    Pass the callables in one expression, keyed by action name, or an
    object whose methods match the action names::

        LocalInvoker({"set_speed": fn, "status": fn})   # a mapping
        LocalInvoker(pump_device)                        # an object

    Sync and async callables both work (sync ones are awaited for you).
    A form with no scheme (or ``local://name``) routes here. You can also
    ``.register(name, fn)`` after construction.
    """

    scheme = "local"

    def __init__(self, handlers: Any = None) -> None:
        self._fns: dict[str, Callable[..., Any]] = {}
        self._obj = None
        # name -> list of subscriber queues (events / observable props).
        self._subs: dict[str, list] = {}
        if isinstance(handlers, dict):
            for k, fn in handlers.items():
                self.register(k, fn)
        elif handlers is not None:
            # An object: resolve action names to its methods on demand.
            self._obj = handlers

    def register(self, key: str, fn: Callable[..., Any]) -> None:
        self._fns[key] = fn

    def _resolve(self, name: str) -> Callable[..., Any] | None:
        if name in self._fns:
            return self._fns[name]
        if self._obj is not None:
            fn = getattr(self._obj, name, None)
            if callable(fn):
                return fn
        return None

    async def invoke(self, action, form, arguments):  # noqa: ANN001
        # Try the form href tail, then the action name.
        key = (form.href.split("://", 1)[-1] if form.href else "") or action.name
        fn = self._resolve(key) or self._resolve(action.name)
        if fn is None:
            return {"error": f"no local callable registered for {key!r}"}
        import inspect

        try:
            result = fn(**arguments)
        except TypeError as e:
            # Bad or extra args; return the error as data so the caller
            # can correct, rather than crashing.
            return {"error": f"invalid arguments for {action.name}: {e}"}
        if inspect.isawaitable(result):
            return await result
        return result

    # Telemetry: read a property, subscribe to a stream
    async def read(self, prop, form):  # noqa: ANN001
        """Read a property's current value. Resolves ``get_<name>`` /
        ``<name>`` on the device object, else a same-named attribute."""
        key = (form.href.split("://", 1)[-1] if form.href else "") or prop.name
        fn = self._resolve(f"get_{prop.name}") or self._resolve(key) or self._resolve(prop.name)
        if fn is not None:
            import inspect

            r = fn()
            return await r if inspect.isawaitable(r) else r
        # Fall back to a plain attribute on the device object.
        if self._obj is not None and hasattr(self._obj, prop.name):
            return getattr(self._obj, prop.name)
        return {"error": f"no readable source for property {prop.name!r}"}

    async def write(self, prop, form, value):  # noqa: ANN001
        """Write a property: call ``set_<name>(value)`` on the device,
        else set a same-named attribute."""
        import inspect

        fn = self._resolve(f"set_{prop.name}")
        if fn is not None:
            r = fn(value)
            return await r if inspect.isawaitable(r) else r
        if self._obj is not None and hasattr(self._obj, prop.name):
            setattr(self._obj, prop.name, value)
            return {"ok": True, prop.name: value}
        return {"error": f"no writable target for property {prop.name!r}"}

    async def subscribe(self, name, form):  # noqa: ANN001
        """Subscribe to an event / observable property by name. Returns
        an async iterator yielding pushed values. The device pushes with
        :meth:`emit`."""
        import asyncio

        queue: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(name, []).append(queue)

        async def _stream():
            try:
                while True:
                    yield await queue.get()
            finally:
                subs = self._subs.get(name, [])
                if queue in subs:
                    subs.remove(queue)

        return _stream()

    def emit(self, name: str, value: Any) -> None:
        """Device side: push a value to everyone subscribed to ``name``
        (an event or an observable property)."""
        for q in self._subs.get(name, []):
            q.put_nowait(value)


class HttpInvoker:
    """POST the action input as JSON to the form's http(s) URL.

    Honors TD-declared security. Bind one Thing with ``with_security`` or a
    whole fleet with ``with_things``; each request then authenticates as the
    Thing that owns the action being invoked. Supply secrets in
    ``credentials``, keyed by Thing id, Thing slug, or scheme name (looked up
    in that order) -- so a multi-Thing client can carry a different secret per
    Thing. The TD names the scheme; you supply the secret.
    """

    scheme = "http"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        headers: dict | None = None,
        credentials: dict | None = None,
    ) -> None:
        self._timeout = timeout
        self._headers = headers or {}
        self._credentials = credentials or {}
        self._schemes_by_name: dict = {}  # set by with_security()
        self._active: tuple = ()
        # thing_id -> (active security names, schemes_by_name); set by
        # with_security/with_things so auth resolves per owning Thing.
        self._things_by_id: dict = {}
        # This invoker also claims https.
        self.schemes = ("http", "https")

    def with_security(self, thing) -> HttpInvoker:
        """Bind a parsed Thing's declared security schemes so requests
        carry the right auth. Returns self (chainable)."""
        self._schemes_by_name = dict(getattr(thing, "security_schemes", {}) or {})
        self._active = tuple(getattr(thing, "security", ()) or ())
        self._register(thing)
        return self

    def with_things(self, things) -> HttpInvoker:
        """Bind many Things so each request authenticates as the Thing that
        owns the action. Returns self (chainable)."""
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
        """Thing-id -> short slug, matching the tool-name scheme
        (urn:thingctx:google-maps -> google-maps)."""
        parts = [p for p in str(thing_id).split(":") if p]
        if len(parts) >= 2 and parts[-1].lower().lstrip("v").isdigit():
            parts = parts[:-1]
        slug = parts[-1] if parts else str(thing_id)
        return "".join(c if (c.isalnum() or c in "._-") else "-" for c in slug)

    def _auth(self, owner_id: str | None = None) -> tuple[dict, dict]:
        """Build (extra_headers, query_params) for the Thing that owns the
        interaction. Resolves that Thing's active scheme(s) and the matching
        secret (by Thing id, then slug, then scheme name)."""
        headers: dict = {}
        params: dict = {}
        active, schemes = self._active, self._schemes_by_name
        if owner_id is not None and owner_id in self._things_by_id:
            active, schemes = self._things_by_id[owner_id]
        slug = self._slug(owner_id) if owner_id is not None else None
        for sname in active:
            scheme = schemes.get(sname)
            if scheme is None or scheme.scheme == "nosec":
                continue
            secret = None
            for key in (owner_id, slug, sname):
                if key is not None and key in self._credentials:
                    secret = self._credentials[key]
                    break
            if secret is None:
                continue
            if scheme.scheme == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            elif scheme.scheme == "basic":
                import base64

                token = base64.b64encode(secret.encode()).decode()
                headers["Authorization"] = f"Basic {token}"
            elif scheme.scheme == "apikey":
                if scheme.in_ == "query":
                    params[scheme.key_name] = secret
                else:
                    headers[scheme.key_name] = secret
        return headers, params

    def _hp(self, owner_id: str | None = None):
        """Merge static headers with TD-security auth to (headers, params)."""
        auth_h, params = self._auth(owner_id)
        return {**self._headers, **auth_h}, params

    async def invoke(self, action, form, arguments):  # noqa: ANN001
        import httpx

        headers, params = self._hp(getattr(action, "thing_id", None))
        # WoT HTTP binding: honor the form's declared method, else default
        # by safety. Idempotent (safe) actions GET with args as query
        # params; others POST with a JSON body.
        method = form.raw.get("htv:methodName")
        if method is None:
            method = "GET" if getattr(action, "idempotent", False) else "POST"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            if method.upper() == "GET":
                resp = await client.get(
                    form.href,
                    headers=headers,
                    params={**params, **arguments},
                )
            else:
                resp = await client.request(
                    method,
                    form.href,
                    json=arguments,
                    headers=headers,
                    params=params,
                )
            resp.raise_for_status()
            return _decode(resp)

    async def read(self, prop, form):  # noqa: ANN001
        """GET the property's current value from its form URL."""
        import httpx

        headers, params = self._hp(getattr(prop, "thing_id", None))
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(form.href, headers=headers, params=params)
            resp.raise_for_status()
            return _decode(resp)

    async def write(self, prop, form, value):  # noqa: ANN001
        """PUT the new value to the property's form URL (the WoT
        ``writeproperty`` HTTP binding default)."""
        import httpx

        headers, params = self._hp(getattr(prop, "thing_id", None))
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.put(
                form.href,
                json=value,
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            return _decode(resp, empty={"ok": True})

    async def subscribe(self, name, form):  # noqa: ANN001
        """Subscribe over Server-Sent Events (the HTTP streaming binding
        WoT uses for events / observable properties). Yields each
        ``data:`` payload as it arrives."""
        import json as _json

        import httpx

        headers, params = self._hp()

        async def _stream():
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", form.href, headers=headers, params=params) as resp:
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            raw = line[5:].strip()
                            try:
                                yield _json.loads(raw)
                            except ValueError:
                                yield raw

        return _stream()


class MqttInvoker:
    """Publish the action input to the form's mqtt topic, await a reply.

    A thin reference implementation over ``paho-mqtt``. The form's
    ``href`` is ``mqtt://broker[:port]/<topic>``; the reply is awaited
    on ``<topic>/reply`` (overridable). Swap this out for your own MQTT
    plumbing, the SDK only needs the ``invoke`` shape.
    """

    scheme = "mqtt"

    def __init__(self, *, broker: str | None = None, timeout: float = 10.0) -> None:
        self._broker = broker
        self._timeout = timeout

    async def invoke(self, action, form, arguments):  # noqa: ANN001
        import asyncio
        import urllib.parse

        import paho.mqtt.client as mqtt  # type: ignore

        u = urllib.parse.urlparse(form.href)
        host = self._broker or u.hostname or "localhost"
        port = u.port or 1883
        topic = u.path.lstrip("/") or action.name
        reply_topic = f"{topic}/reply"

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()

        client = mqtt.Client()

        def _on_message(_c, _u, msg):  # noqa: ANN001
            try:
                payload = json.loads(msg.payload.decode())
            except Exception:  # noqa: BLE001
                payload = msg.payload.decode(errors="replace")
            if not fut.done():
                loop.call_soon_threadsafe(fut.set_result, payload)

        client.on_message = _on_message
        client.connect(host, port)
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
        """Subscribe to the form's MQTT topic; yield each message. This
        is the native WoT events / observable-property binding for MQTT:
        a long-lived subscription, not a request/reply."""
        import asyncio
        import urllib.parse

        import paho.mqtt.client as mqtt  # type: ignore

        u = urllib.parse.urlparse(form.href)
        host = self._broker or u.hostname or "localhost"
        port = u.port or 1883
        topic = u.path.lstrip("/") or name

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        client = mqtt.Client()

        def _on_message(_c, _u, msg):  # noqa: ANN001
            try:
                payload = json.loads(msg.payload.decode())
            except Exception:  # noqa: BLE001
                payload = msg.payload.decode(errors="replace")
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        client.on_message = _on_message
        client.connect(host, port)
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


def select_invoker(
    invokers: list[Invoker],
    form: WoTForm,
) -> Invoker | None:
    """Pick the invoker that handles ``form``'s transport scheme."""
    want = form.scheme
    for inv in invokers:
        schemes = getattr(inv, "schemes", None) or (inv.scheme,)
        if want in schemes:
            return inv
    return None
