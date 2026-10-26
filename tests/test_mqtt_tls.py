# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""MQTT over TLS: an ``mqtts`` href or a ``tls`` option puts the client binding and
the gateway on TLS, which verifies the broker's certificate and hostname, and a
gateway on TLS projects ``mqtts`` forms. A plain ``mqtt`` href or ``host:port`` stays
plain. The live tests run a TLS Mosquitto with a CA they make, and skip without
``mosquitto`` or ``openssl``."""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from thingctx import ThingClient, parse_thing
from thingctx.auth import ClientCertificate
from thingctx.bindings import LocalBinding, MqttBinding, MqttTls
from thingctx.bindings.base import select_binding
from thingctx.gateways import Gateway
from thingctx.gateways.builtin.mqtt import MqttGatewayBinding
from thingctx.reliability import TransportError

pytest.importorskip("paho.mqtt.client")

MOSQUITTO = shutil.which("mosquitto")
OPENSSL = shutil.which("openssl")


class _FakeClient:
    def __init__(self) -> None:
        self.tls: dict | None = None
        self.insecure: bool | None = None

    def username_pw_set(self, username, password=None) -> None:
        pass

    def tls_set(self, **kw) -> None:
        self.tls = kw

    def tls_insecure_set(self, value: bool) -> None:
        self.insecure = value

    def reconnect_delay_set(self, **kw) -> None:
        pass


def _td(href: str) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:x",
        "title": "x",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {"do": {"forms": [{"href": href}]}},
    }


async def _configured(href: str, **binding) -> _FakeClient:
    fake = _FakeClient()
    thing = parse_thing(_td(href))
    inv = MqttBinding(client_factory=lambda: fake, **binding).with_security(thing)
    await inv._connect("urn:dev:x", "broker", 8883, thing.actions["do"].forms[0])
    return fake


def test_tls_sets_the_files_and_verifies_the_certificate_and_hostname():
    fake = _FakeClient()
    MqttTls(ca_certs="ca.pem", certfile="c.pem", keyfile="k.pem").apply(fake)
    assert fake.tls == {
        "ca_certs": "ca.pem",
        "certfile": "c.pem",
        "keyfile": "k.pem",
        "cert_reqs": ssl.CERT_REQUIRED,
        "keyfile_password": None,
    }
    assert fake.insecure is False


async def test_a_plain_href_stays_plain():
    assert (await _configured("mqtt://broker/x")).tls is None


async def test_an_mqtts_href_uses_tls_with_the_system_ca_store():
    fake = await _configured("mqtts://broker/x")
    assert fake.tls is not None
    assert (fake.tls["ca_certs"], fake.tls["certfile"], fake.tls["cert_reqs"]) == (
        None,
        None,
        ssl.CERT_REQUIRED,
    )
    assert fake.insecure is False


async def test_the_tls_option_puts_a_plain_href_on_tls():
    fake = await _configured("mqtt://broker/x", tls=MqttTls(ca_certs="ca.pem"))
    assert fake.tls is not None
    assert fake.tls["ca_certs"] == "ca.pem"


async def test_a_client_certificate_credential_keeps_the_option_ca_and_its_password():
    cert = ClientCertificate(certfile="c.pem", keyfile="k.pem", password="pw")
    fake = await _configured(
        "mqtt://broker/x", tls=MqttTls(ca_certs="ca.pem"), credentials={"urn:dev:x": cert}
    )
    assert fake.tls is not None
    assert (fake.tls["ca_certs"], fake.tls["certfile"], fake.tls["keyfile"]) == (
        "ca.pem",
        "c.pem",
        "k.pem",
    )
    assert fake.tls["keyfile_password"] == "pw"


@pytest.mark.parametrize(
    ("href", "port"),
    [("mqtts://b/t", 8883), ("mqtt://b/t", 1883), ("mqtts://b:9/t", 9), ("mqtt://b:9/t", 9)],
)
def test_the_default_port_follows_the_scheme(href, port):
    assert MqttBinding()._endpoint(SimpleNamespace(href=href), "t") == ("b", port, "t")


def test_an_mqtts_form_routes_to_the_mqtt_binding():
    binding = MqttBinding()
    form = parse_thing(_td("mqtts://broker/x")).actions["do"].forms[0]
    assert select_binding([binding], form) is binding


_PUMP = SimpleNamespace(id="urn:test:pump")


@pytest.mark.parametrize(
    ("broker", "tls", "base"),
    [
        ("h:1", None, "mqtt://h:1"),
        ("mqtt://h:1", None, "mqtt://h:1"),
        ("mqtts://h:1", None, "mqtts://h:1"),
        ("mqtts://h", None, "mqtts://h"),
        ("h:1", MqttTls(), "mqtts://h:1"),
    ],
)
def test_a_gateway_on_tls_projects_mqtts_forms(broker, tls, base):
    gateway = MqttGatewayBinding(broker, tls=tls)
    (form,) = gateway.project_forms(_PUMP, "stop", "invokeaction")
    assert form["href"] == f"{base}/tc/pump/actions/stop"
    assert gateway.scheme == base.partition(":")[0]


@pytest.mark.parametrize("broker", ["ws://h:1", "mqtts://", ""])
def test_a_gateway_refuses_a_broker_it_cannot_reach(broker):
    with pytest.raises(ValueError, match="broker must be"):
        MqttGatewayBinding(broker)


# -- live: a TLS Mosquitto with a CA made for the test ----------------------- #

_CONFIG = """\
[req]
distinguished_name = dn
[dn]
[ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
[server]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run([OPENSSL, *args], cwd=cwd, check=True, capture_output=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def tls_broker(tmp_path: Path):
    """A Mosquitto that serves only TLS, with a certificate for ``localhost`` signed
    by a CA made here. Yields ``(port, CA file)``."""
    if not (MOSQUITTO and OPENSSL):
        pytest.skip("mosquitto / openssl not installed")
    (tmp_path / "openssl.cnf").write_text(_CONFIG)
    cnf = ("-config", "openssl.cnf")
    _openssl(
        *("req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-days", "1"),
        *("-keyout", "ca.key", "-out", "ca.pem", "-subj", "/CN=thingctx test CA"),
        *cnf,
        *("-extensions", "ca"),
        cwd=tmp_path,
    )
    _openssl(
        *("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key"),
        *("-out", "server.csr", "-subj", "/CN=localhost"),
        *cnf,
        cwd=tmp_path,
    )
    _openssl(
        *("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key"),
        *("-set_serial", "2", "-days", "1", "-out", "server.pem"),
        *("-extfile", "openssl.cnf", "-extensions", "server"),
        cwd=tmp_path,
    )
    port = _free_port()
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous true\n"
        f"cafile {tmp_path / 'ca.pem'}\ncertfile {tmp_path / 'server.pem'}\n"
        f"keyfile {tmp_path / 'server.key'}\n"
    )
    proc = subprocess.Popen(
        [MOSQUITTO, "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:  # pragma: no cover
        proc.terminate()
        pytest.skip("mosquitto did not start")
    try:
        yield port, str(tmp_path / "ca.pem")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


_NATIVE = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:test:pump",
    "title": "pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {"stop": {"output": {"type": "object"}, "forms": [{"href": "local://stop"}]}},
}


async def test_a_gateway_and_a_client_round_trip_over_tls(tls_broker):
    port, ca = tls_broker
    device = LocalBinding({"stop": lambda: {"stopped": True}})
    gateway = Gateway(
        ThingClient(tds=[_NATIVE], bindings=[device]),
        MqttGatewayBinding(f"mqtts://localhost:{port}", tls=MqttTls(ca_certs=ca)),
    )
    await gateway.start()
    try:
        td = gateway.projected_tds["pump"]
        assert td["actions"]["stop"]["forms"][0]["href"].startswith(f"mqtts://localhost:{port}/")
        binding = MqttBinding(timeout=5.0, tls=MqttTls(ca_certs=ca))
        assert await ThingClient(tds=[td], bindings=[binding]).invoke("pump__stop") == {
            "stopped": True
        }
    finally:
        await gateway.aclose()


@pytest.mark.parametrize(
    ("href", "trusted"),
    [
        # The system's CA store does not trust the test CA.
        ("mqtts://localhost:{port}/x", False),
        # The certificate names localhost, not 127.0.0.1.
        ("mqtts://127.0.0.1:{port}/x", True),
        # A plain client cannot speak to a TLS listener.
        ("mqtt://localhost:{port}/x", False),
    ],
)
async def test_a_broker_the_client_cannot_verify_is_refused(tls_broker, href, trusted):
    port, ca = tls_broker
    tls = MqttTls(ca_certs=ca) if trusted else None
    inv = MqttBinding(connect_retries=0, connect_timeout=2.0, backoff=0, tls=tls)
    form = SimpleNamespace(href=href.format(port=port), raw={})
    with pytest.raises(TransportError):
        await inv.invoke(SimpleNamespace(name="x"), form, {})
