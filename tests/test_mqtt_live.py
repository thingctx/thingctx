"""Live MQTT Tier 1 proof against a real Mosquitto broker.

Hermetic and credible: the test launches its *own* Mosquitto on an ephemeral
port with ``allow_anonymous false`` and a password file, so a connection only
succeeds if real credentials are presented. It then drives the broker through
``MqttInvoker`` -- which configures the connection purely from neutral
credential material resolved by the shared auth layer -- and asserts:

* the right username/password authenticates and a request/reply roundtrip works,
* a wrong password is rejected by the broker (the auth really is enforced).

Skipped when ``mosquitto``/``mosquitto_passwd`` or ``paho-mqtt`` are absent.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("paho.mqtt.client")
import paho.mqtt.client as mqtt  # noqa: E402

from thingctx import parse_thing  # noqa: E402
from thingctx.invokers import MqttInvoker  # noqa: E402

MOSQUITTO = shutil.which("mosquitto")
MOSQUITTO_PASSWD = shutil.which("mosquitto_passwd")

pytestmark = pytest.mark.skipif(
    not (MOSQUITTO and MOSQUITTO_PASSWD),
    reason="mosquitto / mosquitto_passwd not installed",
)

USER, PASSWORD = "tcuser", "s3cret-pw"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _new_client():
    version = getattr(mqtt, "CallbackAPIVersion", None)
    return mqtt.Client(version.VERSION1) if version else mqtt.Client()


@pytest.fixture()
def broker(tmp_path: Path):
    """Launch a password-protected Mosquitto; yield its port."""
    pwfile = tmp_path / "passwd"
    subprocess.run(
        [MOSQUITTO_PASSWD, "-c", "-b", str(pwfile), USER, PASSWORD],
        check=True,
        capture_output=True,
    )
    port = _free_port()
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous false\npassword_file {pwfile}\n"
    )
    proc = subprocess.Popen(
        [MOSQUITTO, "-c", str(conf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for the listener to accept connections.
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
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _responder(port: int, topic: str):
    """An authenticated echo service: replies to ``<topic>`` on ``<topic>/reply``."""
    client = _new_client()
    client.username_pw_set(USER, PASSWORD)

    def _on_message(c, _u, msg):  # noqa: ANN001
        c.publish(f"{topic}/reply", b'{"ok": true, "echo": ' + msg.payload + b"}")

    client.on_message = _on_message
    client.connect("127.0.0.1", port)
    client.subscribe(topic)
    client.loop_start()
    return client


def _td(port: int, topic: str) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:pump",
        "title": "pump",
        "securityDefinitions": {"sc": {"scheme": "basic"}},
        "security": ["sc"],
        "actions": {"do": {"forms": [{"href": f"mqtt://127.0.0.1:{port}/{topic}"}]}},
    }


async def test_username_password_authenticates_and_roundtrips(broker):
    port = broker
    topic = "tc/pump/do"
    responder = _responder(port, topic)
    try:
        thing = parse_thing(_td(port, topic))
        inv = MqttInvoker(
            credentials={"urn:dev:pump": {"username": USER, "password": PASSWORD}}
        ).with_security(thing)
        action = SimpleNamespace(thing_id="urn:dev:pump", name="do")
        form = SimpleNamespace(href=f"mqtt://127.0.0.1:{port}/{topic}", raw={})

        result = await asyncio.wait_for(inv.invoke(action, form, {"speed": 7}), timeout=5)

        assert result["ok"] is True
        assert result["echo"]["speed"] == 7
    finally:
        responder.loop_stop()
        responder.disconnect()


async def test_wrong_password_is_rejected_by_broker(broker):
    """The broker enforces auth: bad credentials cannot connect (proving the
    successful path really authenticated, not slipped through anonymously)."""
    port = broker
    client = _new_client()
    client.username_pw_set(USER, "wrong-password")
    client.connect("127.0.0.1", port)
    client.loop_start()
    time.sleep(0.3)
    # paho reports connection failure via is_connected() after the CONNACK.
    connected = client.is_connected()
    client.loop_stop()
    try:
        client.disconnect()
    except Exception:  # noqa: BLE001
        pass
    assert not connected
