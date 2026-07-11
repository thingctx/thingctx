"""Security-property regression tests for the WoT-derived authorization seam.

These lock the enforcement guarantees of :mod:`thingctx.authz` against a REAL
:class:`thingctx.ThingClient` (with recording stub bindings, so no network) and
on the CORE install only: no token guard, no external PDP, no extra dependency.
The identity handed to the PEP is a plain claims dict, exactly what a token
guard's ``validate`` would return, written inline so every test runs dep-free.

The properties guarded here:

* property read/write split           -> :func:`test_read_write_split`
* the TD-closed (op-derived) vocabulary and its wildcard containment
                                      -> :func:`test_vocabulary_is_td_closed`
* the WoT default-op rule             -> :func:`test_default_op_rule`,
                                         :func:`test_explicit_op_restricts_default`
* default-deny PEP (no identity, unknown target, fail-closed passthrough)
                                      -> :func:`test_no_identity_denies`,
                                         :func:`test_unknown_affordance_passes_through`,
                                         :func:`test_unknown_device_method_raises_not_forwards`
* the exp-based per-delivery stream filter
                                      -> :func:`test_token_expired_reads_exp`,
                                         :func:`test_per_delivery_filter_stops_on_real_expiry`
* media enforced as invokeaction      -> :func:`test_media_enforced_as_invokeaction`
* multi-transport coverage            -> :func:`test_multi_transport_coverage`
* AuthZEN mapping + fail-closed (pure, no httpx)
                                      -> :func:`test_authzen_mapping_and_fail_closed`
"""

from __future__ import annotations

import time

import pytest

from thingctx import ThingClient
from thingctx.authz import (
    AccessRequest,
    AuthorizationDenied,
    LocalPolicyGrantSource,
    PolicyDecisionPoint,
    build_vocabulary,
    from_authzen_response,
    guard_client,
    to_authzen_request,
)
from thingctx.authz.pep import _authorized_stream, _token_expired
from thingctx.thing import parse_thing

try:  # ProtocolBinding is the public stub base; import defensively for the name.
    from thingctx import ProtocolBinding
except ImportError:  # pragma: no cover - fall back to the module path
    from thingctx.binding import ProtocolBinding

PUMP_ID = "urn:dev:pump"


# --------------------------------------------------------------------------- #
# Test TD and recording stub binding
# --------------------------------------------------------------------------- #


def _pump_td() -> dict:
    """A pump exercising every vocabulary case:

    * ``setpoint``: read+write over TWO transports (http AND mqtt); multi transport.
    * ``serial``: ``readOnly`` with an explicit ``["readproperty"]``; closure case.
    * ``telemetry``: a property form with NO ``op``; default op (read+write).
    * ``reboot``: an action form with NO ``op``; default op (invokeaction).
    * ``alarm``: an event.
    """
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": PUMP_ID,
        "title": "Pump",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "properties": {
            "setpoint": {
                "type": "number",
                "forms": [
                    {
                        "href": "http://pump.local/setpoint",
                        "op": ["readproperty", "writeproperty", "observeproperty"],
                    },
                    {
                        "href": "mqtt://bus/pump/setpoint",
                        "op": ["readproperty", "writeproperty", "observeproperty"],
                    },
                ],
            },
            "serial": {
                "type": "string",
                "readOnly": True,
                "forms": [{"href": "http://pump.local/serial", "op": ["readproperty"]}],
            },
            "telemetry": {
                "type": "number",
                "forms": [{"href": "http://pump.local/telemetry"}],
            },
        },
        "actions": {
            "reboot": {"forms": [{"href": "http://pump.local/reboot"}]},
        },
        "events": {
            "alarm": {"forms": [{"href": "mqtt://bus/pump/alarm", "op": ["subscribeevent"]}]},
        },
    }


class _RecordingBinding(ProtocolBinding):
    """A stub transport that records every call, so a test can assert the PEP
    fired first: an empty ``fired`` means no device was ever touched."""

    def __init__(self, scheme: str, fired: list) -> None:
        self.scheme = scheme
        self._fired = fired

    async def read(self, prop, form):
        self._fired.append((self.scheme, "read", prop.name))
        return {"value": 42, "via": self.scheme}

    async def write(self, prop, form, value):
        self._fired.append((self.scheme, "write", prop.name, value))
        return {"ok": True, "via": self.scheme}

    async def invoke(self, action, form, arguments):
        self._fired.append((self.scheme, "invoke", action.name))
        return {"ok": True, "via": self.scheme}


def _client(td: dict, fired: list, *, prefer_mqtt: bool = False) -> ThingClient:
    http = _RecordingBinding("http", fired)
    mqtt = _RecordingBinding("mqtt", fired)
    order = [mqtt, http] if prefer_mqtt else [http, mqtt]
    return ThingClient(tds=[td], bindings=order)


# --------------------------------------------------------------------------- #
# property read/write split
# --------------------------------------------------------------------------- #


async def test_read_write_split():
    """Granted read but not write: read runs, write is refused before the device;
    the reverse split holds too."""
    td = _pump_td()
    vocab = build_vocabulary(parse_thing(td))
    policy = {
        "reader": {(PUMP_ID, "setpoint", "readproperty")},
        "writer": {(PUMP_ID, "setpoint", "writeproperty")},
    }
    pdp = PolicyDecisionPoint(vocab, LocalPolicyGrantSource(policy))

    fired: list = []
    client = _client(td, fired)
    reader = guard_client(client, pdp, identity={"roles": ["reader"]})
    assert (await reader.read_property("pump.setpoint")) == {"value": 42, "via": "http"}
    assert fired == [("http", "read", "setpoint")]

    fired.clear()
    with pytest.raises(AuthorizationDenied) as ei:
        await reader.write_property("pump.setpoint", 99)
    assert ei.value.request.op == "writeproperty"
    assert fired == []  # device never touched: PEP fired before binding selection
    await client.aclose()

    fired2: list = []
    client2 = _client(td, fired2)
    writer = guard_client(client2, pdp, identity={"roles": ["writer"]})
    assert (await writer.write_property("pump.setpoint", 55)) == {"ok": True, "via": "http"}
    fired2.clear()
    with pytest.raises(AuthorizationDenied) as ei2:
        await writer.read_property("pump.setpoint")
    assert ei2.value.request.op == "readproperty"
    assert fired2 == []
    await client2.aclose()


async def test_bulk_property_read_write_cannot_bypass_the_pdp():
    """A binding that offers a bulk read_all/write_all must not let
    read_all_properties / read_properties / write_properties skip authorization.
    With a deny-all PDP the bulk fast path is never taken; every property goes
    through the per-property authorized path and is denied."""

    class _BulkBinding(ProtocolBinding):
        scheme = "http"

        def __init__(self) -> None:
            self.bulk_fired: list[str] = []

        async def read(self, prop, form):
            return {"value": 1}

        async def write(self, prop, form, value):
            return {"ok": True}

        async def invoke(self, action, form, arguments):
            return {"ok": True}

        # The bulk fast path. If authorization is enforced, this must never run.
        async def read_all(self, thing, form, names=None):
            self.bulk_fired.append("read_all")
            return {"setpoint": 7, "serial": "SN"}

        async def write_all(self, thing, form, values):
            self.bulk_fired.append("write_all")
            return {"ok": True}

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": PUMP_ID,
        "title": "Pump",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "properties": {
            "setpoint": {
                "type": "number",
                "forms": [{"href": "http://p/setpoint", "op": ["readproperty", "writeproperty"]}],
            },
        },
        # bulk forms the fast path keys on
        "forms": [
            {"href": "http://p/all", "op": ["readallproperties", "writeallproperties"]},
        ],
    }
    vocab = build_vocabulary(parse_thing(td))
    # deny-all: an identity with a role that has no grants at all
    pdp = PolicyDecisionPoint(vocab, LocalPolicyGrantSource({}))

    binding = _BulkBinding()
    client = ThingClient(
        tds=[td], bindings=[binding], pdp=pdp, identity={"roles": ["nobody"]}, authz_raise=False
    )

    got = await client.read_all_properties()
    # The device's bulk read never ran; the per-property path returned a denial.
    assert binding.bulk_fired == [], "bulk read_all bypassed the PDP"
    assert any(isinstance(v, dict) and v.get("error") for v in got.values())

    await client.write_properties({"setpoint": 9})
    assert binding.bulk_fired == [], "bulk write_all bypassed the PDP"
    await client.aclose()


async def test_read_write_split_envelope_mode():
    """``raise_on_deny=False`` returns a thingctx-style error envelope instead of
    raising, and still touches no device."""
    td = _pump_td()
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(
        vocab, LocalPolicyGrantSource({"reader": {(PUMP_ID, "setpoint", "readproperty")}})
    )
    fired: list = []
    client = _client(td, fired)
    reader = guard_client(client, pdp, identity={"roles": ["reader"]}, raise_on_deny=False)
    denied = await reader.write_property("pump.setpoint", 1)
    assert denied["error"] == "authorization denied"
    assert denied["op"] == "writeproperty"
    assert denied["affordance"] == "setpoint"
    assert fired == []
    await client.aclose()


# --------------------------------------------------------------------------- #
# the op-derived vocabulary is TD-closed
# --------------------------------------------------------------------------- #


async def test_vocabulary_is_td_closed():
    """An op no form declares is not grantable. ``serial`` is read-only, so its
    ``writeproperty`` is absent from the vocabulary, and neither a policy that
    names it directly nor a wildcard grant can exercise it."""
    td = _pump_td()
    vocab = build_vocabulary(parse_thing(td))
    assert (PUMP_ID, "serial", "readproperty") in vocab
    assert (PUMP_ID, "serial", "writeproperty") not in vocab

    pdp = PolicyDecisionPoint(
        vocab, LocalPolicyGrantSource({"overreach": {(PUMP_ID, "serial", "writeproperty")}})
    )
    decision = await pdp.decide(
        {"roles": ["overreach"]}, AccessRequest(PUMP_ID, "serial", "writeproperty")
    )
    assert decision.permit is False
    assert "vocabulary" in decision.reason

    pdp_wild = PolicyDecisionPoint(
        vocab, LocalPolicyGrantSource({"op": {(PUMP_ID, "*", "writeproperty")}})
    )
    d_serial = await pdp_wild.decide(
        {"roles": ["op"]}, AccessRequest(PUMP_ID, "serial", "writeproperty")
    )
    d_setpoint = await pdp_wild.decide(
        {"roles": ["op"]}, AccessRequest(PUMP_ID, "setpoint", "writeproperty")
    )
    assert d_serial.permit is False  # read-only: wildcard cannot add it
    assert d_setpoint.permit is True  # writable: wildcard covers it

    fired: list = []
    client = _client(td, fired)
    ac = guard_client(client, pdp, identity={"roles": ["overreach"]})
    with pytest.raises(AuthorizationDenied):
        await ac.write_property("pump.serial", "x")
    assert fired == []
    await client.aclose()


# --------------------------------------------------------------------------- #
# the WoT default-op rule
# --------------------------------------------------------------------------- #


async def test_default_op_rule():
    """A property form with no ``op`` implies read+write; an action form with no
    ``op`` implies invokeaction. Prove both are grantable and exercisable."""
    td = _pump_td()
    vocab = build_vocabulary(parse_thing(td))
    assert (PUMP_ID, "telemetry", "readproperty") in vocab
    assert (PUMP_ID, "telemetry", "writeproperty") in vocab
    assert (PUMP_ID, "reboot", "invokeaction") in vocab

    policy = {
        "operator": {
            (PUMP_ID, "telemetry", "readproperty"),
            (PUMP_ID, "telemetry", "writeproperty"),
            (PUMP_ID, "reboot", "invokeaction"),
        }
    }
    pdp = PolicyDecisionPoint(vocab, LocalPolicyGrantSource(policy))
    fired: list = []
    client = _client(td, fired)
    op = guard_client(client, pdp, identity={"roles": ["operator"]})
    assert (await op.read_property("pump.telemetry"))["via"] == "http"
    assert (await op.write_property("pump.telemetry", 3))["via"] == "http"
    assert (await op.invoke("pump.reboot", {}))["via"] == "http"
    assert len(fired) == 3
    await client.aclose()


async def test_explicit_op_restricts_default():
    """An explicit ``op`` list is a RESTRICTION: serial's ``["readproperty"]``
    yields read but not write, though a bare form would default to both."""
    vocab = build_vocabulary(parse_thing(_pump_td()))
    assert (PUMP_ID, "serial", "readproperty") in vocab
    assert (PUMP_ID, "serial", "writeproperty") not in vocab


# --------------------------------------------------------------------------- #
# default-deny PEP
# --------------------------------------------------------------------------- #


async def test_no_identity_denies():
    """No identity (``None``) yields no roles, no grants, so any in-vocabulary
    call is denied for a granted-only policy."""
    td = _pump_td()
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(
        vocab, LocalPolicyGrantSource({"reader": {(PUMP_ID, "setpoint", "readproperty")}})
    )
    fired: list = []
    client = _client(td, fired)
    ac = guard_client(client, pdp, identity=None)
    with pytest.raises(AuthorizationDenied):
        await ac.read_property("pump.setpoint")
    assert fired == []
    await client.aclose()


async def test_unknown_affordance_passes_through():
    """An unknown tool name is NOT turned into an authorization error: the real
    client answers with its own 'unknown' envelope, so a 404-shaped miss never
    masquerades as a 403."""
    td = _pump_td()
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(vocab, LocalPolicyGrantSource({"reader": set()}))
    fired: list = []
    client = _client(td, fired)
    ac = guard_client(client, pdp, identity={"roles": ["reader"]})
    result = await ac.read_property("pump.nonexistent")
    assert "unknown property" in result["error"]
    await client.aclose()


async def test_unknown_device_method_raises_not_forwards():
    """A device-reaching method not explicitly wrapped/allowlisted must NOT be
    default-forwarded (that would be default-allow); it raises AttributeError.
    A safe introspection method still passes through."""
    td = _pump_td()
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(vocab, LocalPolicyGrantSource({"reader": set()}))
    fired: list = []
    client = _client(td, fired)
    ac = guard_client(client, pdp, identity={"roles": ["reader"]})
    with pytest.raises(AttributeError):
        _ = ac.some_future_device_method
    assert isinstance(ac.list_actions(), list)
    await client.aclose()


async def test_as_tools_returns_guarded_invoke():
    """``as_tools()`` must hand back the PEP's GUARDED invoke, never the raw one,
    or an agent loop / the MCP bridge would bypass every check."""
    td = _pump_td()
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(vocab, LocalPolicyGrantSource({"reader": set()}))
    fired: list = []
    client = _client(td, fired)
    ac = guard_client(client, pdp, identity={"roles": ["reader"]})
    specs, invoke = ac.as_tools()
    with pytest.raises(AuthorizationDenied):
        await invoke("pump.reboot", {})
    assert fired == []
    await client.aclose()


# --------------------------------------------------------------------------- #
# multi-transport coverage
# --------------------------------------------------------------------------- #


async def test_multi_transport_coverage():
    """setpoint has an http form AND an mqtt form; the PEP denies identically
    regardless of which transport the call would route to, with no device touched
    either way. Proven by flipping the preferred transport."""
    td = _pump_td()
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(vocab, LocalPolicyGrantSource({"nobody": set()}))
    for prefer_mqtt in (False, True):
        fired: list = []
        client = _client(td, fired, prefer_mqtt=prefer_mqtt)
        prop = client._props["pump.setpoint"]
        expected = "mqtt" if prefer_mqtt else "http"
        assert prop.primary_form(prefer=client._prefer).scheme == expected
        ac = guard_client(client, pdp, identity={"roles": ["nobody"]})
        with pytest.raises(AuthorizationDenied):
            await ac.write_property("pump.setpoint", 7)
        assert fired == []
        await client.aclose()


# --------------------------------------------------------------------------- #
# subscribe gate + the exp-based per-delivery stream filter
# --------------------------------------------------------------------------- #


async def test_subscribe_enforced_gate_and_denied():
    """subscribe is enforced at the gate: a granted observe returns the stream, an
    ungranted subscribeevent is denied."""
    td = _pump_td()
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(
        vocab, LocalPolicyGrantSource({"reader": {(PUMP_ID, "setpoint", "observeproperty")}})
    )
    fired: list = []
    client = _client(td, fired)
    ac = guard_client(client, pdp, identity={"roles": ["reader"]})
    it = await ac.subscribe("pump.setpoint")
    seen = [x async for x in it]
    assert seen == []  # granted; stub yields nothing, but NOT denied
    with pytest.raises(AuthorizationDenied):
        await ac.subscribe("pump.alarm")  # not granted subscribeevent
    await client.aclose()


def test_token_expired_reads_exp():
    """The per-delivery filter's expiry check reads the JWT ``exp`` claim against
    the clock, and fails closed when it is missing or the identity is not claims."""
    assert _token_expired({"exp": 0}) is True  # 1970 -> expired
    assert _token_expired({"exp": time.time() + 3600}) is False
    assert _token_expired({}) is True  # no exp -> fail closed
    assert _token_expired("not-a-dict") is True  # no claims -> fail closed


async def test_per_delivery_filter_stops_on_real_expiry():
    """With the REAL PDP (a pure permit), a stream is still cut the moment the
    identity's ``exp`` deadline passes. This is the staleness window closing, and
    it is closed by reading exp, not by re-running the PDP."""
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:p",
        "title": "P",
        "securityDefinitions": {"n": {"scheme": "nosec"}},
        "security": ["n"],
        "properties": {
            "t": {"observable": True, "forms": [{"href": "mqtt://t", "op": ["observeproperty"]}]}
        },
    }
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(
        vocab, LocalPolicyGrantSource({"r": {("urn:p", "t", "observeproperty")}})
    )
    req = AccessRequest("urn:p", "t", "observeproperty")

    async def _five():
        for i in range(5):
            yield {"reading": i}

    expired = {"roles": ["r"], "exp": time.time() - 10}
    got = [v async for v in _authorized_stream(_five(), pdp, expired, req)]
    assert got == []  # expired token -> stream stopped immediately by the filter

    valid = {"roles": ["r"], "exp": time.time() + 3600}
    got2 = [v async for v in _authorized_stream(_five(), pdp, valid, req)]
    assert got2 == [{"reading": i} for i in range(5)]  # valid token -> all flow


# --------------------------------------------------------------------------- #
# media enforced as invokeaction
# --------------------------------------------------------------------------- #


def _camera_td() -> dict:
    """A real media affordance: an action with a media (rtsp/video) form."""
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:cam",
        "title": "Cam",
        "securityDefinitions": {"n": {"scheme": "nosec"}},
        "security": ["n"],
        "actions": {"watch": {"forms": [{"href": "rtsp://cam/live", "contentType": "video/mp4"}]}},
    }


class _MediaBinding:
    """A content-routed stub that claims the media form and yields two frames, so
    the media path runs without the [media] extra."""

    schemes = ("rtsp",)
    scheme = "rtsp"

    def handles(self, form):
        return True

    def frames(self, action, form, arguments, *, track="video"):
        async def _gen():
            for f in ({"frame": 0}, {"frame": 1}):
                yield f

        return _gen()

    async def invoke(self, action, form, arguments):
        return None


async def test_media_enforced_as_invokeaction():
    """Media is authorized as ``invokeaction`` (media's real op). Without the
    grant the caller is denied; with it, the frames actually stream."""
    td = _camera_td()
    vocab = build_vocabulary(parse_thing(td))
    assert ("urn:cam", "watch", "invokeaction") in vocab  # media's real op

    pdp_deny = PolicyDecisionPoint(vocab, LocalPolicyGrantSource({"r": set()}))
    client = ThingClient(tds=[td], bindings=[_MediaBinding()])
    media_name = next(iter(getattr(client, "_media", {})), None)
    assert media_name is not None, "media affordance must register"
    ac = guard_client(client, pdp_deny, identity={"roles": ["r"], "exp": time.time() + 3600})
    with pytest.raises(AuthorizationDenied):
        async for _ in await ac.frames(media_name):
            pass
    await client.aclose()

    pdp_ok = PolicyDecisionPoint(
        vocab, LocalPolicyGrantSource({"r": {("urn:cam", "watch", "invokeaction")}})
    )
    client2 = ThingClient(tds=[td], bindings=[_MediaBinding()])
    media2 = next(iter(getattr(client2, "_media", {})), None)
    assert media2 is not None
    ac2 = guard_client(client2, pdp_ok, identity={"roles": ["r"], "exp": time.time() + 3600})
    got = [fr async for fr in await ac2.frames(media2)]
    assert got == [{"frame": 0}, {"frame": 1}], f"granted media must stream all frames, got {got}"
    await client2.aclose()


# --------------------------------------------------------------------------- #
# AuthZEN mapping + fail-closed (pure, no httpx)
# --------------------------------------------------------------------------- #


def test_authzen_mapping_and_fail_closed():
    """The AuthZEN 1.0 mapping and its fail-closed response reader are pure and
    run without httpx: a non-explicit-true decision denies."""
    req = AccessRequest("urn:demo:pump", "setpoint", "writeproperty", "http")
    body = to_authzen_request({"sub": "agent", "roles": ["op"]}, req)
    assert body["subject"]["type"] == "identity" and body["subject"]["id"] == "agent"
    assert body["action"]["name"] == "writeproperty"
    assert body["resource"]["id"] == "urn:demo:pump/setpoint"
    assert "context" in body  # form scheme carried
    assert from_authzen_response({"decision": True}, req).permit is True
    assert from_authzen_response({"decision": False}, req).permit is False
    assert from_authzen_response({}, req).permit is False  # missing -> deny (fail closed)
    assert from_authzen_response("garbage", req).permit is False  # non-object -> deny


# --------------------------------------------------------------------------- #
# native enforcement: the pdp= constructor IS the guarded path (no wrapper)
# --------------------------------------------------------------------------- #


class _StreamingBinding(ProtocolBinding):
    """A recording stub that also opens streams (subscribe) and records the open,
    so a stream test can assert the device stream was never established on deny."""

    def __init__(self, scheme: str, fired: list) -> None:
        self.scheme = scheme
        self._fired = fired

    async def read(self, prop, form):
        self._fired.append((self.scheme, "read", prop.name))
        return {"value": 42, "via": self.scheme}

    async def write(self, prop, form, value):
        self._fired.append((self.scheme, "write", prop.name, value))
        return {"ok": True, "via": self.scheme}

    async def invoke(self, action, form, arguments):
        self._fired.append((self.scheme, "invoke", action.name))
        return {"ok": True, "via": self.scheme}

    async def subscribe(self, target, form, args=None):
        self._fired.append((self.scheme, "subscribe", getattr(target, "name", target)))

        async def _gen():
            yield {"reading": 1}

        return _gen()


def _obs_pump_td() -> dict:
    """A pump whose setpoint is observable over mqtt (so subscribe has an
    observeproperty path) plus an event and an action, all over one recorded
    transport."""
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": PUMP_ID,
        "title": "Pump",
        "securityDefinitions": {"n": {"scheme": "nosec"}},
        "security": ["n"],
        "properties": {
            "setpoint": {
                "type": "number",
                "observable": True,
                "forms": [
                    {
                        "href": "mqtt://bus/pump/setpoint",
                        "op": ["readproperty", "writeproperty", "observeproperty"],
                    }
                ],
            },
        },
        "actions": {"reboot": {"forms": [{"href": "mqtt://bus/pump/reboot"}]}},
        "events": {
            "alarm": {"forms": [{"href": "mqtt://bus/pump/alarm", "op": ["subscribeevent"]}]}
        },
    }


async def _drain(res):
    """Consume a subscribe/frames result (an awaitable that returns an async
    iterator) and return the yielded values as a list."""
    it = await res if hasattr(res, "__await__") else res
    return [v async for v in it]


def _native_deny_all(td: dict, fired: list):
    """A ThingClient built via the native ``pdp=`` constructor with a real
    deny-all PDP (a role holding no grants). No guard_client, no wrapper."""
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(vocab, LocalPolicyGrantSource({"nobody": set()}))
    client = ThingClient(
        tds=[td],
        bindings=[_StreamingBinding("mqtt", fired)],
        pdp=pdp,
        identity={"roles": ["nobody"], "exp": time.time() + 3600},
    )
    return client


async def test_native_constructor_blocks_every_method_before_device():
    """The primary entry point: ``ThingClient(pdp=, identity=)``. A deny-all PDP
    blocks invoke / read_property / write_property / subscribe(event) /
    subscribe(observe) before any transport, with the device never touched."""
    td = _obs_pump_td()
    fired: list = []
    client = _native_deny_all(td, fired)

    with pytest.raises(AuthorizationDenied):
        await client.invoke("pump.reboot", {})
    assert fired == []

    with pytest.raises(AuthorizationDenied):
        await client.read_property("pump.setpoint")
    assert fired == []

    with pytest.raises(AuthorizationDenied):
        await client.write_property("pump.setpoint", 9)
    assert fired == []

    with pytest.raises(AuthorizationDenied):
        await _drain(client.subscribe("pump.alarm"))  # subscribeevent
    assert fired == []

    with pytest.raises(AuthorizationDenied):
        await _drain(client.subscribe("pump.setpoint"))  # observeproperty
    assert fired == []

    await client.aclose()


async def test_native_constructor_as_tools_invoke_is_authorized():
    """``as_tools()`` on a natively-constructed guarded client hands back the
    authorized invoke, so an agent loop / MCP bridge cannot bypass the check."""
    td = _obs_pump_td()
    fired: list = []
    client = _native_deny_all(td, fired)
    _specs, invoke = client.as_tools()
    with pytest.raises(AuthorizationDenied):
        await invoke("pump.reboot", {})
    assert fired == []
    await client.aclose()


async def test_guarded_is_native_client_sharing_state():
    """``guarded()`` returns a real ThingClient (not a proxy) that shares the
    original's internal maps by reference, so there is no second dispatch surface
    to drift."""
    td = _obs_pump_td()
    fired: list = []
    plain = ThingClient(tds=[td], bindings=[_StreamingBinding("mqtt", fired)])
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(vocab, LocalPolicyGrantSource({"nobody": set()}))
    g = plain.guarded(pdp, identity={"roles": ["nobody"]})
    assert type(g) is ThingClient
    assert g._things is plain._things
    assert g._route is plain._route
    assert g._registry is plain._registry
    # the clone enforces; the original does not
    assert g._pdp is pdp and plain._pdp is None
    _specs, invoke = g.as_tools()
    with pytest.raises(AuthorizationDenied):
        await invoke("pump.reboot", {})
    assert fired == []
    await plain.aclose()


async def test_no_pdp_is_backward_compatible():
    """No PDP at all (``pdp=None``, the default): every method works and nothing
    raises AuthorizationDenied. Proves the non-authz path is unchanged."""
    td = _obs_pump_td()
    fired: list = []
    client = ThingClient(tds=[td], bindings=[_StreamingBinding("mqtt", fired)])
    assert client._pdp is None
    assert (await client.invoke("pump.reboot", {}))["ok"] is True
    assert (await client.read_property("pump.setpoint"))["value"] == 42
    assert (await client.write_property("pump.setpoint", 3))["ok"] is True
    assert (await _drain(client.subscribe("pump.setpoint"))) == [{"reading": 1}]
    assert (await _drain(client.subscribe("pump.alarm"))) == [{"reading": 1}]
    _specs, invoke = client.as_tools()
    assert (await invoke("pump.reboot", {}))["ok"] is True
    await client.aclose()


async def test_envelope_mode_stream_denials_surface_and_touch_nothing():
    """With ``authz_raise=False`` a denied stream yields exactly one denial
    envelope (not a silent empty stream) and never opens the device stream."""
    td = _obs_pump_td()
    fired: list = []
    vocab = build_vocabulary(parse_thing(td))
    pdp = PolicyDecisionPoint(vocab, LocalPolicyGrantSource({"nobody": set()}))
    client = ThingClient(
        tds=[td],
        bindings=[_StreamingBinding("mqtt", fired)],
        pdp=pdp,
        identity={"roles": ["nobody"], "exp": time.time() + 3600},
        authz_raise=False,
    )
    ob = await _drain(client.subscribe("pump.setpoint"))
    ev = await _drain(client.subscribe("pump.alarm"))
    for vals, op in ((ob, "observeproperty"), (ev, "subscribeevent")):
        assert len(vals) == 1
        assert vals[0]["error"] == "authorization denied"
        assert vals[0]["op"] == op
    assert fired == []  # device stream never opened
    await client.aclose()
