# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""RedisBinding: read/write Redis keys and subscribe to Redis channels."""

from __future__ import annotations

import contextlib
import inspect
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from thingctx.bindings.base import ProtocolBinding
from thingctx.contracts import implements

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from thingctx.thing import WoTAction, WoTForm, WoTProperty


@dataclass(frozen=True)
class RedisEndpoint:
    """One parsed Redis form endpoint.

    ``connection_url`` identifies the Redis server and logical database.
    ``resource`` is interpreted as a Redis key by ``read``/``write`` and as a
    pub/sub channel by ``subscribe``.
    """

    connection_url: str
    resource: str


def _parse_endpoint(href: str) -> RedisEndpoint:
    """Parse ``redis[s]://host[:port]/<resource>[?db=N]``.

    The path names the resource rather than Redis' conventional database-number
    path because a WoT form needs to identify the key/channel it drives. Select
    a logical Redis database with ``?db=N`` instead. The form must not embed
    credentials; pass those when constructing the binding instead.
    """

    parts = urlsplit(href)
    if parts.scheme not in {"redis", "rediss"}:
        raise ValueError("Redis form href must use redis:// or rediss://")
    if not parts.hostname:
        raise ValueError("Redis form href must include a hostname")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Redis form href must not embed credentials; pass them to RedisBinding")
    if parts.fragment:
        raise ValueError("Redis form href must not include a fragment")

    resource = unquote(parts.path.lstrip("/"))
    if not resource:
        raise ValueError("Redis form href must include a key or channel in its path")

    query = parse_qs(parts.query, keep_blank_values=True)
    unknown = set(query) - {"db"}
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unsupported Redis form query parameter(s): {names}")

    db_values = query.get("db", ["0"])
    if len(db_values) != 1:
        raise ValueError("Redis form db must be specified at most once")
    try:
        db = int(db_values[0])
    except ValueError as exc:
        raise ValueError("Redis form db must be a non-negative integer") from exc
    if db < 0:
        raise ValueError("Redis form db must be a non-negative integer")

    # Rebuild a redis-py connection URL whose path is the logical DB. The form's
    # original path is the thingctx resource and is intentionally not forwarded.
    connection_url = urlunsplit((parts.scheme, parts.netloc, f"/{db}", "", ""))
    return RedisEndpoint(connection_url=connection_url, resource=resource)


def _schema_type(schema: Any) -> str | None:
    """Return a single declared JSON-schema type when one is unambiguous."""

    if not isinstance(schema, dict):
        return None
    raw = schema.get("type")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        concrete = [item for item in raw if isinstance(item, str) and item != "null"]
        if len(concrete) == 1:
            return concrete[0]
    return None


def _target_schema(target: Any) -> dict[str, Any] | None:
    """Return a property's value schema or an event's data schema."""

    schema = getattr(target, "schema", None)
    if isinstance(schema, dict):
        return schema
    data_schema = getattr(target, "data_schema", None)
    return data_schema if isinstance(data_schema, dict) else None


def _decode_redis(value: Any, schema: dict[str, Any] | None = None) -> Any:
    """Decode Redis bytes into the most useful Python value.

    Redis returns bytes by default. A TD-declared ``string`` remains a string;
    JSON-shaped data for number/boolean/object/array schemas is JSON-decoded.
    Without a schema, valid JSON is decoded heuristically and other UTF-8 stays
    text. Non-UTF-8 payloads stay bytes.
    """

    if value is None or not isinstance(value, bytes | bytearray | memoryview):
        return value

    raw = bytes(value)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw

    if _schema_type(schema) == "string":
        return text

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _encode_redis(value: Any, schema: dict[str, Any] | None = None) -> bytes:
    """Encode a Python value for a Redis ``SET`` or ``PUBLISH`` payload.

    Strings are written as ordinary UTF-8 so non-thingctx Redis clients see the
    expected value. Structured/scalar JSON values are compact-JSON encoded;
    bytes pass through unchanged. The TD schema lets the read side preserve a
    numeric-looking string such as ``"42"`` as a string.
    """

    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        kind = _schema_type(schema) or "Redis"
        raise TypeError(f"{kind} value must be bytes, text, or JSON-serializable") from exc


async def _close_resource(resource: Any) -> None:
    """Close redis-py 5.x (``close``) or newer (``aclose``) resources."""

    closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if not callable(closer):
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


@implements(ProtocolBinding)
class RedisBinding:
    """Drive Redis keys and pub/sub channels with redis-py's asyncio client.

    A form uses ``redis://host[:port]/<name>[?db=N]`` (or ``rediss://`` for
    TLS). The WoT affordance decides what ``<name>`` addresses:

    * property ``read`` / ``write`` -> a Redis key (``GET`` / ``SET``);
    * event/observable ``subscribe`` -> a Redis pub/sub channel (``SUBSCRIBE``).

    The URL therefore does not guess whether a name is a key or a channel. The
    Thing Description operation supplies that meaning. Redis action invocation
    has no mapping in this binding and raises ``NotImplementedError``.

    Credentials belong in binding configuration, not in a Thing Description:
    pass redis-py connection options such as ``username`` / ``password`` via
    ``connection_kwargs`` instead of embedding them in the form URL.
    """

    scheme = "redis"
    schemes = ("redis", "rediss")

    def __init__(
        self,
        *,
        connection_kwargs: dict[str, Any] | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._connection_kwargs = dict(connection_kwargs or {})
        self._client_factory = client_factory
        self._clients: dict[str, Any] = {}

    def _new_client(self, connection_url: str) -> Any:
        options = {**self._connection_kwargs, "decode_responses": False}
        if self._client_factory is not None:
            return self._client_factory(connection_url, **options)

        # Optional dependency: importing thingctx must not import redis-py.
        import redis.asyncio as redis  # noqa: PLC0415

        return redis.from_url(connection_url, **options)

    def _client(self, endpoint: RedisEndpoint) -> Any:
        client = self._clients.get(endpoint.connection_url)
        if client is None:
            client = self._new_client(endpoint.connection_url)
            self._clients[endpoint.connection_url] = client
        return client

    async def invoke(
        self,
        action: WoTAction,
        form: WoTForm,
        arguments: dict[str, Any],
    ) -> Any:
        del action, form, arguments
        raise NotImplementedError(
            "RedisBinding maps properties to keys and subscriptions to channels; "
            "Redis actions are not supported"
        )

    async def read(self, prop: WoTProperty, form: WoTForm) -> Any:
        """GET the Redis key named by the form path."""

        endpoint = _parse_endpoint(form.href)
        value = await self._client(endpoint).get(endpoint.resource)
        return _decode_redis(value, _target_schema(prop))

    async def write(self, prop: WoTProperty, form: WoTForm, value: Any) -> Any:
        """SET the Redis key named by the form path."""

        endpoint = _parse_endpoint(form.href)
        payload = _encode_redis(value, _target_schema(prop))
        ok = await self._client(endpoint).set(endpoint.resource, payload)
        return {"ok": bool(ok)}

    async def subscribe(
        self,
        target: Any,
        form: WoTForm,
        args: dict[str, Any] | None = None,
    ) -> AsyncIterator[Any]:
        """SUBSCRIBE to the Redis channel named by the form path."""

        if args:
            raise ValueError("Redis subscriptions do not accept subscription arguments")

        endpoint = _parse_endpoint(form.href)
        pubsub = self._client(endpoint).pubsub()
        try:
            await pubsub.subscribe(endpoint.resource)
        except BaseException:
            await _close_resource(pubsub)
            raise

        schema = _target_schema(target)

        async def _stream() -> AsyncIterator[Any]:
            try:
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=None,
                    )
                    if message is not None and message.get("type") in {"message", "pmessage"}:
                        yield _decode_redis(message.get("data"), schema)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.unsubscribe(endpoint.resource)
                with contextlib.suppress(Exception):
                    await _close_resource(pubsub)

        return _stream()

    async def aclose(self) -> None:
        """Close every pooled Redis client. Safe to call more than once."""

        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await _close_resource(client)

    async def __aenter__(self) -> RedisBinding:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
