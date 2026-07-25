# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Conformance kit for the thingctx extension contracts.

Run these checks against any extension, built-in or third party, to prove it
honours the contract the runtime drives it through:

* :func:`assert_binding_contract` for a :class:`~thingctx.ProtocolBinding`: it
  names a scheme, exposes an async ``invoke``, and is consistent about the
  capabilities it advertises.
* :func:`assert_media_backend_contract` for a
  :class:`~thingctx.MediaBackend`: the pluggable engine behind ``MediaBinding``.
  It exposes synchronous ``can_open`` / ``read`` / ``write`` that run off the
  event loop.
* :func:`assert_provider_contract` for a
  :class:`~thingctx.CredentialProvider`: it names itself, decides what it
  handles with a synchronous ``matches``, and resolves credential material with
  an async ``resolve``.
* :func:`assert_registry_contract` for a :class:`~thingctx.registry.Registry`: a
  discovery source that yields Thing Descriptions from a synchronous ``fetch``.

An adopter writing an extension uses these to gain confidence the runtime will
drive it the same way it drives the built-ins.

    from thingctx.testing import assert_binding_contract
    assert_binding_contract(MyBinding())
"""

from __future__ import annotations

import inspect
from typing import Any

from thingctx import CredentialProvider
from thingctx.bindings import (
    AsyncAction,
    BulkProperties,
    ContentRouted,
    MediaBackend,
    MediaConsumer,
    MediaPublisher,
    ProtocolBinding,
    Readable,
    Subscribable,
    Writable,
    binding_schemes,
)
from thingctx.gateways import (
    Announces,
    EventMirroring,
    GatewayBinding,
    PubSubOnly,
    QoSAware,
    RequestReply,
)
from thingctx.registry import Registry

# Capability -> (attribute name, expected to be a coroutine function).
_ASYNC_CAPS = (
    (Readable, "read"),
    (Writable, "write"),
    (Subscribable, "subscribe"),
    (MediaPublisher, "publish"),
)

# Capabilities whose methods are all expected to be coroutine functions.
_MULTI_ASYNC_CAPS = (
    (BulkProperties, ("read_all", "write_all")),
    (AsyncAction, ("invoke_async", "query_action", "cancel_action")),
)


def binding_capabilities(binding: Any) -> dict[str, bool]:
    """Report which optional capabilities a binding advertises. Handy for docs
    and for asserting a binding supports what a TD needs."""
    return {
        "content_routed": isinstance(binding, ContentRouted),
        "readable": isinstance(binding, Readable),
        "writable": isinstance(binding, Writable),
        "subscribable": isinstance(binding, Subscribable),
        "bulk_properties": isinstance(binding, BulkProperties),
        "async_action": isinstance(binding, AsyncAction),
        "media_consumer": isinstance(binding, MediaConsumer),
        "media_publisher": isinstance(binding, MediaPublisher),
    }


def assert_binding_contract(binding: Any) -> None:
    """Assert ``binding`` satisfies the core contract and that every capability
    it advertises has the right shape. Raises ``AssertionError`` on a breach."""
    schemes = binding_schemes(binding)
    assert schemes and all(
        isinstance(s, str) and s for s in schemes
    ), "a binding must name at least one non-empty scheme"

    assert isinstance(binding, ProtocolBinding), "a binding must expose a scheme and invoke()"
    assert inspect.iscoroutinefunction(binding.invoke), "invoke() must be async"

    if isinstance(binding, ContentRouted):
        assert callable(binding.handles), "handles must be callable"
        assert not inspect.iscoroutinefunction(binding.handles), "handles must be synchronous"

    if isinstance(binding, MediaConsumer):
        assert callable(binding.frames), "frames must be callable"
        assert not inspect.iscoroutinefunction(
            binding.frames
        ), "frames is a synchronous factory returning an async iterator"

    for cap, attr in _ASYNC_CAPS:
        if isinstance(binding, cap):
            method = getattr(binding, attr)
            assert inspect.iscoroutinefunction(method), f"{attr}() must be async"

    for cap, attrs in _MULTI_ASYNC_CAPS:
        if isinstance(binding, cap):
            for attr in attrs:
                assert inspect.iscoroutinefunction(
                    getattr(binding, attr)
                ), f"{attr}() must be async"


def assert_media_backend_contract(backend: Any) -> None:
    """Assert ``backend`` satisfies the :class:`~thingctx.MediaBackend` contract:
    the engine ``MediaBinding`` runs to decode or encode media.

    A backend exposes ``can_open(url, hint) -> bool`` to claim a source, ``read``
    to yield :class:`~thingctx.Frame` objects, and ``write`` to push them to a
    target. All three are synchronous: the binding runs them in a worker thread
    off the event loop, and ``read`` / ``write`` stop when the passed
    ``threading.Event`` is set. Raises ``AssertionError`` on a breach."""

    assert isinstance(
        backend, MediaBackend
    ), "a media backend must expose can_open(), read(), and write()"
    for attr in ("can_open", "read", "write"):
        method = getattr(backend, attr)
        assert callable(method), f"{attr} must be callable"
        assert not inspect.iscoroutinefunction(
            method
        ), f"{attr}() must be synchronous; it runs off the event loop in a worker thread"
    assert inspect.isgeneratorfunction(
        backend.read
    ), "read() must be a generator that yields Frame objects until stop is set"


def assert_provider_contract(provider: Any) -> None:
    """Assert ``provider`` satisfies the :class:`~thingctx.CredentialProvider`
    contract: a named provider that decides what it handles with a synchronous
    ``matches`` and resolves neutral credential material with an async
    ``resolve``. Raises ``AssertionError`` on a breach."""

    assert isinstance(
        provider, CredentialProvider
    ), "a provider must expose name, matches(), and resolve()"
    assert (
        isinstance(provider.name, str) and provider.name
    ), "a provider must name itself with a non-empty string"
    assert callable(provider.matches), "matches must be callable"
    assert not inspect.iscoroutinefunction(
        provider.matches
    ), "matches() must be synchronous; it only inspects a scheme and credential"
    assert inspect.iscoroutinefunction(
        provider.resolve
    ), "resolve() must be async; it may mint a token over the network"


def assert_registry_contract(registry: Any, *, call: bool = True) -> None:
    """Assert ``registry`` satisfies the :class:`~thingctx.registry.Registry`
    contract: a discovery source with a synchronous ``fetch`` that returns a list
    of Thing Description dicts. With ``call=True`` (the default) it invokes
    ``fetch`` once and checks the shape; pass ``call=False`` to skip the call when
    fetching has a cost or side effect. Raises ``AssertionError`` on a breach."""

    assert isinstance(registry, Registry), "a registry must expose fetch()"
    assert callable(registry.fetch), "fetch must be callable"
    assert not inspect.iscoroutinefunction(registry.fetch), "fetch() must be synchronous"
    if call:
        tds = registry.fetch()
        assert isinstance(tds, list) and all(
            isinstance(td, dict) for td in tds
        ), "fetch() must return a list of Thing Description dicts"


def gateway_binding_capabilities(binding: Any) -> dict[str, bool]:
    """Report which optional capabilities a gateway binding (middleware driver)
    advertises. A driver opts into a capability by implementing its protocol, so
    the engine calls only what a driver declares (the anti-lowest-common-
    denominator rule)."""

    return {
        "request_reply": isinstance(binding, RequestReply),
        "event_mirroring": isinstance(binding, EventMirroring),
        "pubsub_only": isinstance(binding, PubSubOnly),
        "announces": isinstance(binding, Announces),
        "qos_aware": isinstance(binding, QoSAware),
    }


def assert_gateway_binding_contract(binding: Any) -> None:
    """Assert ``binding`` satisfies the :class:`~thingctx.gateways.GatewayBinding`
    contract (serve a fleet over a middleware) and that every capability it
    advertises has the right shape. Tests the CONTRACT, not a driver's own form
    vocabulary. Raises ``AssertionError`` on a breach.

    The core contract: a non-empty ``scheme``; a synchronous ``project_forms``
    returning a list of form dicts; async ``serve`` and ``aclose``. Optional
    capabilities (request/reply, event mirroring, QoS, announce) are checked for
    shape only when present, so a driver is never forced to support an operation
    its transport cannot carry."""

    assert isinstance(
        binding, GatewayBinding
    ), "a gateway binding must expose scheme, project_forms, serve, aclose"
    assert (
        isinstance(getattr(binding, "scheme", None), str) and binding.scheme
    ), "a gateway binding must name a non-empty scheme"
    assert callable(binding.project_forms), "project_forms must be callable"
    assert not inspect.iscoroutinefunction(
        binding.project_forms
    ), "project_forms must be synchronous (it builds forms, it does not do I/O)"
    assert inspect.iscoroutinefunction(binding.serve), "serve() must be async"
    assert inspect.iscoroutinefunction(binding.aclose), "aclose() must be async"

    # Optional capabilities: shape-checked only when advertised.
    if isinstance(binding, RequestReply):
        assert inspect.iscoroutinefunction(binding.reply), "reply() must be async"
    if isinstance(binding, EventMirroring):
        assert inspect.iscoroutinefunction(binding.mirror_event), "mirror_event() must be async"
    if isinstance(binding, Announces):
        assert inspect.iscoroutinefunction(binding.announce), "announce() must be async"
        assert inspect.iscoroutinefunction(binding.reap), "reap() must be async"
    if isinstance(binding, QoSAware):
        assert callable(binding.quality_terms), "quality_terms must be callable"
        assert not inspect.iscoroutinefunction(
            binding.quality_terms
        ), "quality_terms must be synchronous"
