# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Redis binding: URL semantics, codecs, key I/O, pub/sub, and TD routing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from thingctx import ThingClient
from thingctx.bindings.builtin.redis import (
    RedisBinding,
    _decode_redis,
    _encode_redis,
    _parse_endpoint,
)
from thingctx.testing import assert_binding_contract, binding_capabilities

REPO = Path(__file__).resolve().parent.parent


def _prop(schema: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(schema=schema or {})


def test_redis_binding_conforms_and_advertises_capabilities() -> None:
    binding = RedisBinding(client_factory=lambda *_args, **_kwargs: None)
    assert_binding_contract(binding)
    caps = binding_capabilities(binding)
    assert caps["readable"] and caps["writable"] and caps["subscribable"]


def test_parse_endpoint_uses_path_as_resource_and_query_as_db() -> None:
    endpoint = _parse_endpoint("rediss://cache.local:6380/sensor%3Atemp?db=2")
    assert endpoint.connection_url == "rediss://cache.local:6380/2"
    assert endpoint.resource == "sensor:temp"


@pytest.mark.parametrize(
    ("href", "message"),
    [
        ("http://cache.local/key", "redis:// or rediss://"),
        ("redis:///key", "hostname"),
        ("redis://cache.local", "key or channel"),
        ("redis://cache.local/key?db=-1", "non-negative integer"),
        ("redis://cache.local/key?db=nope", "non-negative integer"),
        ("redis://cache.local/key?db=1&db=2", "at most once"),
        ("redis://cache.local/key?timeout=1", "unsupported"),
        ("redis://cache.local/key#fragment", "fragment"),
        ("redis://user:secret@cache.local/key", "must not embed credentials"),
    ],
)
def test_parse_endpoint_rejects_ambiguous_or_unsafe_forms(href: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _parse_endpoint(href)


@pytest.mark.parametrize(
    ("raw", "schema", "expected"),
    [
        (b"hello", None, "hello"),
        (b"42", None, 42),
        (b"3.5", None, 3.5),
        (b"true", None, True),
        (b'{"room":"kitchen"}', None, {"room": "kitchen"}),
        (b"[1,2]", None, [1, 2]),
        (b"42", {"type": "string"}, "42"),
        (b"\xff\xfe", None, b"\xff\xfe"),
        (None, None, None),
    ],
)
def test_decode_redis(raw: Any, schema: dict[str, Any] | None, expected: Any) -> None:
    assert _decode_redis(raw, schema) == expected


def test_encode_redis_uses_interoperable_text_and_json() -> None:
    assert _encode_redis("ready", {"type": "string"}) == b"ready"
    assert _encode_redis(42, {"type": "integer"}) == b"42"
    assert _encode_redis({"value": 42}) == b'{"value":42}'
    assert _encode_redis(b"raw") == b"raw"


def test_encode_redis_refuses_non_json_values() -> None:
    with pytest.raises(TypeError):
        _encode_redis(object())


class FakePubSub:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def get_message(self, **_kwargs: Any) -> dict[str, Any] | None:
        return self.messages.pop(0) if self.messages else None

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def close(self) -> None:
        # Deliberately the redis-py 5.x name; this tests close/aclose compatibility.
        self.closed = True


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.pubsub_instance = FakePubSub([])
        self.closed = False

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: bytes) -> bool:
        self.values[key] = value
        return True

    def pubsub(self) -> FakePubSub:
        return self.pubsub_instance

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_read_write_use_key_semantics_and_schema() -> None:
    fake = FakeRedis()
    created: list[tuple[str, dict[str, Any]]] = []

    def factory(url: str, **kwargs: Any) -> FakeRedis:
        created.append((url, kwargs))
        return fake

    binding = RedisBinding(client_factory=factory)
    form = SimpleNamespace(href="redis://localhost/sensor:status")
    prop = _prop({"type": "string"})

    assert await binding.write(prop, form, "42") == {"ok": True}
    assert fake.values["sensor:status"] == b"42"
    assert await binding.read(prop, form) == "42"
    assert created == [("redis://localhost/0", {"decode_responses": False})]


@pytest.mark.asyncio
async def test_missing_key_returns_none() -> None:
    fake = FakeRedis()
    binding = RedisBinding(client_factory=lambda *_args, **_kwargs: fake)
    form = SimpleNamespace(href="redis://localhost/missing")
    assert await binding.read(_prop({"type": "string"}), form) is None


@pytest.mark.asyncio
async def test_subscribe_uses_channel_semantics_and_decodes_event_schema() -> None:
    fake = FakeRedis()
    fake.pubsub_instance = FakePubSub(
        [{"type": "message", "channel": b"alerts", "data": b'{"level":"high"}'}]
    )
    binding = RedisBinding(client_factory=lambda *_args, **_kwargs: fake)
    target = SimpleNamespace(data_schema={"type": "object"})
    form = SimpleNamespace(href="redis://localhost/alerts")

    stream = await binding.subscribe(target, form)
    assert await anext(stream) == {"level": "high"}
    await stream.aclose()

    assert fake.pubsub_instance.subscribed == ["alerts"]
    assert fake.pubsub_instance.unsubscribed == ["alerts"]
    assert fake.pubsub_instance.closed is True


@pytest.mark.asyncio
async def test_subscribe_rejects_unused_arguments() -> None:
    binding = RedisBinding(client_factory=lambda *_args, **_kwargs: FakeRedis())
    with pytest.raises(ValueError, match="do not accept"):
        await binding.subscribe(None, SimpleNamespace(href="redis://localhost/alerts"), {"x": 1})


@pytest.mark.asyncio
async def test_aclose_closes_pooled_clients_and_is_idempotent() -> None:
    fake = FakeRedis()
    binding = RedisBinding(client_factory=lambda *_args, **_kwargs: fake)
    await binding.read(_prop(), SimpleNamespace(href="redis://localhost/key"))
    await binding.aclose()
    await binding.aclose()
    assert fake.closed is True


@pytest.fixture
def fakeredis_pair():
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()

    def factory(_url: str, **kwargs: Any):
        return fakeredis.FakeAsyncRedis(server=server, **kwargs)

    peer = fakeredis.FakeAsyncRedis(server=server, decode_responses=False)
    return factory, peer


@pytest.mark.asyncio
async def test_example_td_routes_keys_and_pubsub_end_to_end(fakeredis_pair) -> None:
    factory, peer = fakeredis_pair
    td = json.loads((REPO / "examples" / "registry" / "redis.td.json").read_text())
    binding = RedisBinding(client_factory=factory)
    client = ThingClient(tds=[td], bindings=[binding], approve_when="never")

    try:
        assert await client.write_property("redis__temperature", 21.5) == {"ok": True}
        assert await client.read_property("redis__temperature") == 21.5

        assert await client.write_property("redis__status", "42") == {"ok": True}
        assert await client.read_property("redis__status") == "42"

        stream = await client.subscribe("redis__alerts")
        next_item = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await peer.publish(
            "sensor:alerts",
            b'{"level":"warning","message":"temperature high"}',
        )
        assert await asyncio.wait_for(next_item, timeout=1) == {
            "level": "warning",
            "message": "temperature high",
        }
        await stream.aclose()
    finally:
        await client.aclose()
        close = getattr(peer, "aclose", None) or getattr(peer, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result
