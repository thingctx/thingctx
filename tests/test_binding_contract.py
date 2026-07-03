# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The binding contract: built-ins conform, an adopter can replace one or add a
new protocol, and the runtime routes through the registry either way."""

from __future__ import annotations

import pytest

from thingctx import BindingRegistry, ThingClient, build_builtin
from thingctx.bindings import BUILTIN_BINDINGS, default_bindings, discover_bindings
from thingctx.testing import assert_binding_contract, binding_capabilities
from thingctx.thing import WoTForm

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:pump:v1",
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {
        "set_speed": {
            "input": {"type": "object", "properties": {"rpm": {"type": "integer"}}},
            "forms": [{"href": "local://set_speed"}],
        }
    },
}


@pytest.mark.parametrize("name", BUILTIN_BINDINGS)
def test_builtin_bindings_conform(name):
    assert_binding_contract(build_builtin(name))


def test_builtin_capability_advertisement():
    media = build_builtin("media")
    caps = binding_capabilities(media)
    assert caps["content_routed"] and caps["media_consumer"] and caps["media_publisher"]
    assert binding_capabilities(build_builtin("mqtt"))["subscribable"]
    http = binding_capabilities(build_builtin("http"))
    assert http["readable"] and http["writable"] and http["subscribable"]
    # http is the reference transport: it also drives bulk and the async lifecycle.
    assert http["bulk_properties"] and http["async_action"]
    # mqtt and local do not advertise bulk or async; the runtime falls back.
    assert not binding_capabilities(build_builtin("mqtt"))["bulk_properties"]
    assert not binding_capabilities(build_builtin("local"))["async_action"]


def test_bulk_capability_must_be_async():
    class SyncBulk:
        scheme = "x"

        async def invoke(self, action, form, arguments):
            return None

        def read_all(self, thing, form, names=None):  # not async
            return {}

        async def write_all(self, thing, form, values):
            return {}

    with pytest.raises(AssertionError, match="read_all"):
        assert_binding_contract(SyncBulk())


def test_default_registry_is_http_and_local():
    reg = BindingRegistry.default()
    assert reg.schemes() == ("http", "https", "local")
    assert [type(d).__name__ for d in default_bindings()] == ["HttpBinding", "LocalBinding"]


def test_register_replaces_the_builtin_it_covers():
    class MyHttp:
        schemes = ("http", "https")
        scheme = "http"

        async def invoke(self, action, form, arguments):
            return "mine"

    reg = BindingRegistry.default()  # HttpBinding + LocalBinding
    reg.register(MyHttp())
    # The built-in http binding is replaced, not stacked: one binding per scheme.
    assert [type(d).__name__ for d in reg.bindings] == ["MyHttp", "LocalBinding"]
    chosen = reg.resolve(WoTForm(href="http://x/y", op=("invokeaction",)))
    assert isinstance(chosen, MyHttp)


def test_replacing_http_keeps_a_content_routed_binding():
    # media claims forms by content (a hint on an http href), so it legitimately
    # sits beside the http binding; replacing http must not remove it.
    reg = BindingRegistry.default(media=True)

    class MyHttp:
        schemes = ("http", "https")
        scheme = "http"

        async def invoke(self, action, form, arguments):
            return "mine"

    reg.register(MyHttp())
    names = [type(d).__name__ for d in reg.bindings]
    assert "MediaBinding" in names and "MyHttp" in names and "HttpBinding" not in names


def test_add_a_new_protocol_binding():
    class OpcUa:
        scheme = "opc.tcp"

        async def invoke(self, action, form, arguments):
            return "opc"

    assert_binding_contract(OpcUa())
    reg = BindingRegistry.default().register(OpcUa())
    chosen = reg.resolve(WoTForm(href="opc.tcp://plc/node", op=("invokeaction",)))
    assert isinstance(chosen, OpcUa)


@pytest.mark.asyncio
async def test_client_routes_through_an_injected_registry():
    seen = {}

    class LocalShim:
        scheme = "local"

        async def invoke(self, action, form, arguments):
            seen["args"] = arguments
            return {"ok": True}

    reg = BindingRegistry([LocalShim()])
    client = ThingClient(tds=[TD], bindings=reg)
    out = await client.invoke("pump.set_speed", {"rpm": 7})
    assert out == {"ok": True}
    assert seen["args"] == {"rpm": 7}


@pytest.mark.asyncio
async def test_bindings_kwarg_still_works():
    class LocalShim:
        scheme = "local"

        async def invoke(self, action, form, arguments):
            return "legacy"

    client = ThingClient(tds=[TD], bindings=[LocalShim()])
    assert await client.invoke("pump.set_speed", {"rpm": 1}) == "legacy"


def test_unknown_builtin_name_raises():
    with pytest.raises(KeyError):
        build_builtin("does-not-exist")


def test_discover_bindings_is_empty_without_entry_points():
    assert discover_bindings(group="thingctx.bindings.test-none") == []


def test_discover_auth_is_empty_without_entry_points():
    from thingctx import discover_auth

    assert discover_auth(group="thingctx.auth.test-none") == []


def test_provider_contract_kit():
    from thingctx import BaseAuth, implements
    from thingctx.auth import CredentialProvider
    from thingctx.testing import assert_provider_contract

    @implements(CredentialProvider)
    class Good(BaseAuth):
        name = "good"

        def matches(self, scheme, credential):
            return False

        async def resolve(self, ctx):
            return None

    assert_provider_contract(Good())

    class BadSyncResolve:
        name = "bad"

        def matches(self, scheme, credential):
            return False

        def resolve(self, ctx):  # not async
            return None

    with pytest.raises(AssertionError):
        assert_provider_contract(BadSyncResolve())


def test_registry_contract_kit():
    from thingctx import implements
    from thingctx.registry import Registry
    from thingctx.testing import assert_registry_contract

    @implements(Registry)
    class Good:
        def fetch(self):
            return [{"id": "urn:x"}]

    assert_registry_contract(Good())

    class BadReturnsNonList:
        def fetch(self):
            return {"not": "a list"}

    with pytest.raises(AssertionError):
        assert_registry_contract(BadReturnsNonList())


def test_custom_media_backend_conforms():
    import threading
    from collections.abc import Iterator

    from thingctx import Frame
    from thingctx.testing import assert_media_backend_contract

    class FakeBackend:
        def can_open(self, url: str, hint: dict) -> bool:
            return True

        def read(self, url, *, options: dict, stop: threading.Event) -> Iterator[Frame]:
            yield Frame(data=b"", kind="video")

        def write(self, frames, target, *, options: dict, stop: threading.Event) -> None:
            for _ in frames:
                pass

    assert_media_backend_contract(FakeBackend())


def test_media_backend_with_non_generator_read_is_rejected():
    from thingctx.testing import assert_media_backend_contract

    class BadBackend:
        def can_open(self, url, hint):
            return True

        def read(self, url, *, options, stop):  # returns a list, not a generator
            return []

        def write(self, frames, target, *, options, stop):
            return None

    with pytest.raises(AssertionError):
        assert_media_backend_contract(BadBackend())


def test_builtin_media_backends_conform():
    pytest.importorskip("av")
    from thingctx.bindings.builtin.media.backends import ExtractorBackend, PyAVBackend
    from thingctx.testing import assert_media_backend_contract

    assert_media_backend_contract(PyAVBackend())
    assert_media_backend_contract(ExtractorBackend())


def test_implements_accepts_a_conforming_binding():
    from thingctx import ProtocolBinding, implements

    @implements(ProtocolBinding)
    class Good:
        scheme = "x"

        async def invoke(self, action, form, arguments):
            return None

    assert Good.__thingctx_implements__ == (ProtocolBinding,)


def test_implements_rejects_a_missing_method_at_definition_time():
    from thingctx import ProtocolBinding, implements

    with pytest.raises(TypeError, match="invoke"):

        @implements(ProtocolBinding)
        class NoInvoke:
            scheme = "x"


def test_implements_accepts_an_annotated_only_attribute():
    from thingctx import ProtocolBinding, implements

    @implements(ProtocolBinding)
    class Annotated:
        scheme: str  # set in __init__, declared here so the contract is visible

        async def invoke(self, action, form, arguments):
            return None

    assert Annotated.__thingctx_implements__ == (ProtocolBinding,)


# --------------------------------------------------------------------------- #
# Load-time capability gate (validate="strict")
# --------------------------------------------------------------------------- #

_GATE_BASE = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:gate",
    "title": "Gate",
}


def test_strict_refuses_an_unapplied_security_scheme():
    from thingctx.validate import TDValidationError

    td = {
        **_GATE_BASE,
        "securityDefinitions": {"d_sc": {"scheme": "digest", "in": "header", "qop": "auth"}},
        "security": ["d_sc"],
        "actions": {"go": {"forms": [{"href": "https://x/go"}]}},
    }
    with pytest.raises(TDValidationError, match="digest"):
        ThingClient(tds=[td], bindings=[build_builtin("http")], validate="strict")


def test_strict_refuses_a_transport_with_no_binding():
    from thingctx.validate import TDValidationError

    td = {
        **_GATE_BASE,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {"go": {"forms": [{"href": "opc.tcp://plc/node"}]}},
    }
    with pytest.raises(TDValidationError, match="opc.tcp"):
        ThingClient(tds=[td], bindings=[build_builtin("local")], validate="strict")


def test_strict_refuses_an_undriven_subprotocol():
    from thingctx.validate import TDValidationError

    td = {
        **_GATE_BASE,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "events": {"e": {"forms": [{"href": "https://x/e", "subprotocol": "longpoll"}]}},
    }
    with pytest.raises(TDValidationError, match="longpoll"):
        ThingClient(tds=[td], bindings=[build_builtin("http")], validate="strict")


def test_strict_accepts_a_fully_supported_td():
    td = {
        **_GATE_BASE,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {"go": {"forms": [{"href": "local://go"}]}},
    }
    # A supported TD loads under strict without raising.
    ThingClient(tds=[td], bindings=[build_builtin("local")], validate="strict")


def test_implements_works_for_media_backend_and_registry():
    from thingctx import MediaBackend, implements
    from thingctx.registry import Registry

    @implements(Registry)
    class Reg:
        def fetch(self):
            return []

    @implements(MediaBackend)
    class Backend:
        def can_open(self, url, hint):
            return True

        def read(self, url, *, options, stop):
            yield None

        def write(self, frames, target, *, options, stop):
            return None

    assert Reg().fetch() == []
    assert Backend.__thingctx_implements__ == (MediaBackend,)
