"""The neutral contract in action: providers resolve secrets into Credential
material, and per-transport appliers map that material onto HTTP and MQTT.

The headline guarantee is reuse: the *same* credential, declared once on a
Thing, drives both an HTTP request and an MQTT connection -- because nothing in
the auth layer is transport-shaped. All offline; the MQTT wiring is checked with
a fake paho client.
"""

from __future__ import annotations

import base64

from thingctx import parse_thing
from thingctx.auth import (
    ApiKeyCredential,
    BasicCredential,
    BearerToken,
    ClientCertificate,
    EnhancedAuth,
    RequestSigner,
    SignatureCredential,
    apply_http,
    apply_mqtt,
)
from thingctx.invokers import HttpInvoker, MqttInvoker

# --------------------------------------------------------------------------- #
# apply_http: each Credential kind maps to the right HTTP slot
# --------------------------------------------------------------------------- #


def test_apply_http_bearer_and_basic_set_authorization():
    bearer = apply_http([BearerToken(token="T")])
    assert bearer.headers["Authorization"] == "Bearer T"

    basic = apply_http([BasicCredential("u", "p")])
    assert basic.headers["Authorization"] == "Basic " + base64.b64encode(b"u:p").decode()


def test_apply_http_apikey_header_vs_query():
    h = apply_http([ApiKeyCredential(name="X-Key", value="K", location="header")])
    assert h.headers["X-Key"] == "K" and h.params == {}

    q = apply_http([ApiKeyCredential(name="key", value="K", location="query")])
    assert q.params["key"] == "K" and "key" not in q.headers


def test_apply_http_client_certificate_becomes_cert():
    plan = apply_http([ClientCertificate(certfile="c.pem", keyfile="k.pem")])
    assert plan.cert == ("c.pem", "k.pem")


def test_apply_http_signature_and_request_signer_schedule_signers():
    plan = apply_http(
        [
            SignatureCredential(
                algorithm="aws-sigv4", key_id="a", secret_key="b", params={"service": "sts"}
            ),
            RequestSigner(sign=lambda r: None),
        ]
    )
    assert len(plan.signers) == 2


def test_apply_http_unknown_signature_algorithm_is_skipped():
    plan = apply_http([SignatureCredential(algorithm="no-such-alg", key_id="a", secret_key="b")])
    assert not plan.signers


def test_apply_http_ignores_enhanced_auth():
    plan = apply_http([EnhancedAuth(method="K8S-SAT", data=b"x")])
    assert plan.headers == {} and plan.params == {} and not plan.signers


# --------------------------------------------------------------------------- #
# apply_mqtt: each Credential kind maps to the right CONNECT slot
# --------------------------------------------------------------------------- #


def test_apply_mqtt_basic_sets_username_password():
    plan = apply_mqtt([BasicCredential("u", "p")])
    assert (plan.username, plan.password) == ("u", "p")


def test_apply_mqtt_token_becomes_password():
    assert apply_mqtt([BearerToken(token="TOK")]).password == "TOK"
    assert apply_mqtt([ApiKeyCredential(name="x", value="K")]).password == "K"


def test_apply_mqtt_client_certificate_is_tls():
    cert = ClientCertificate(certfile="c.pem", keyfile="k.pem", ca_certs="ca.pem")
    plan = apply_mqtt([cert])
    assert plan.tls is cert


def test_apply_mqtt_enhanced_auth_is_carried():
    ea = EnhancedAuth(method="SCRAM-SHA-256", data=b"init")
    assert apply_mqtt([ea]).enhanced is ea


def test_apply_mqtt_ignores_http_only_kinds():
    plan = apply_mqtt(
        [
            SignatureCredential(algorithm="aws-sigv4", key_id="a", secret_key="b"),
            RequestSigner(sign=lambda r: None),
        ]
    )
    assert not plan.has_credentials


# --------------------------------------------------------------------------- #
# MqttInvoker wiring: neutral material configures paho before connect
# --------------------------------------------------------------------------- #


class _FakeClient:
    def __init__(self):
        self.user = None
        self.tls = None

    def username_pw_set(self, username, password=None):
        self.user = (username, password)

    def tls_set(self, **kw):
        self.tls = kw


def _td(scheme: dict) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:x",
        "title": "x",
        "securityDefinitions": {"sc": scheme},
        "security": ["sc"],
        "actions": {"do": {"forms": [{"href": "mqtt://broker/x"}]}},
    }


async def test_mqtt_invoker_basic_sets_username_password():
    thing = parse_thing(_td({"scheme": "basic"}))
    inv = MqttInvoker(credentials={"urn:dev:x": "user:pass"}).with_security(thing)
    client = _FakeClient()
    await inv._apply_auth(client, "urn:dev:x")
    assert client.user == ("user", "pass")


async def test_mqtt_invoker_token_is_password():
    thing = parse_thing(_td({"scheme": "bearer"}))
    inv = MqttInvoker(credentials={"urn:dev:x": "TOK"}).with_security(thing)
    client = _FakeClient()
    await inv._apply_auth(client, "urn:dev:x")
    assert client.user == ("", "TOK")


async def test_mqtt_invoker_enhanced_auth_uses_v5_connect_properties():
    """Tier 2: EnhancedAuth (AIO SAT / EMQX SCRAM) flows through as neutral
    material and is mapped onto an MQTT v5 CONNECT -- a v5 client plus the
    AuthenticationMethod/Data properties. (Built offline; no broker.)"""
    import paho.mqtt.client as mqtt

    thing = parse_thing(_td({"scheme": "nosec"}))
    ea = EnhancedAuth(method="K8S-SAT", data=b"sat-token")
    inv = MqttInvoker(credentials={"urn:dev:x": ea}).with_security(thing)

    client, props = await inv._connect("urn:dev:x", "broker", 8883)
    assert client._protocol == mqtt.MQTTv5
    assert props is not None
    assert props.AuthenticationMethod == "K8S-SAT"
    assert props.AuthenticationData == b"sat-token"


async def test_mqtt_invoker_no_enhanced_auth_stays_v3_no_properties():
    thing = parse_thing(_td({"scheme": "basic"}))
    inv = MqttInvoker(credentials={"urn:dev:x": "u:p"}).with_security(thing)
    client, props = await inv._connect("urn:dev:x", "broker", 1883)
    import paho.mqtt.client as mqtt

    assert client._protocol != mqtt.MQTTv5
    assert props is None


async def test_mqtt_invoker_mtls_calls_tls_set():
    # mTLS material flows through the layer via the direct/passthrough provider:
    # the credential is itself neutral ClientCertificate material.
    thing = parse_thing(_td({"scheme": "nosec"}))
    cert = ClientCertificate(certfile="c.pem", keyfile="k.pem", ca_certs="ca.pem")
    inv = MqttInvoker(credentials={"urn:dev:x": cert}).with_security(thing)
    client = _FakeClient()
    await inv._apply_auth(client, "urn:dev:x")
    assert client.tls == {"ca_certs": "ca.pem", "certfile": "c.pem", "keyfile": "k.pem"}


# --------------------------------------------------------------------------- #
# The point of the whole refactor: one credential, every transport
# --------------------------------------------------------------------------- #


async def test_same_basic_credential_drives_http_and_mqtt():
    """A `basic` Thing with one secret: HTTP turns it into an Authorization
    header, MQTT into a username/password -- from the identical declaration."""
    thing = parse_thing(_td({"scheme": "basic"}))

    http = HttpInvoker(credentials={"urn:dev:x": "user:pass"}).with_security(thing)
    headers, _params, _signers, _cert = await http._prepare("urn:dev:x")
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"user:pass").decode()

    mq = MqttInvoker(credentials={"urn:dev:x": "user:pass"}).with_security(thing)
    client = _FakeClient()
    await mq._apply_auth(client, "urn:dev:x")
    assert client.user == ("user", "pass")


async def test_same_mtls_credential_drives_http_and_mqtt():
    """mTLS material is reused verbatim: HTTP sets the client cert, MQTT calls
    tls_set -- same ClientCertificate, no transport-specific auth code."""
    thing = parse_thing(_td({"scheme": "nosec"}))
    cert = ClientCertificate(certfile="c.pem", keyfile="k.pem", ca_certs="ca.pem")

    http = HttpInvoker(credentials={"urn:dev:x": cert}).with_security(thing)
    _h, _p, _s, http_cert = await http._prepare("urn:dev:x")
    assert http_cert == ("c.pem", "k.pem")

    mq = MqttInvoker(credentials={"urn:dev:x": cert}).with_security(thing)
    client = _FakeClient()
    await mq._apply_auth(client, "urn:dev:x")
    assert client.tls["certfile"] == "c.pem"
