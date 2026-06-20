"""OAuth2 JWT-bearer assertion grant (RFC 7523) against a real local server.

This is how Google Cloud service accounts authenticate: the client signs a JWT
with its private key (RS256) and exchanges it for a bearer token. The local
token endpoint *verifies the assertion's signature with the public key*, so the
test proves thingctx really signs a valid assertion -- not just posts a blob.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

pytest.importorskip("jwt")
pytest.importorskip("cryptography")

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from thingctx import HttpInvoker, parse_thing  # noqa: E402

GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
TOKEN = "jwt-minted-access-token"
ISS = "svc@project.iam.gserviceaccount.com"


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv, pub


PRIVATE_KEY, PUBLIC_KEY = _keypair()


class _Handler(BaseHTTPRequestHandler):
    seen = {"token_calls": 0, "claims": None, "api_auth": None, "bad": None}

    def log_message(self, *a):
        pass

    def _json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        if self.path == "/token":
            type(self).seen["token_calls"] += 1
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            if form.get("grant_type") != GRANT or "assertion" not in form:
                type(self).seen["bad"] = "wrong-grant"
                return self._json(400, {"error": "unsupported_grant_type"})
            try:
                claims = jwt.decode(
                    form["assertion"],
                    PUBLIC_KEY,
                    algorithms=["RS256"],
                    audience=None,
                    options={"verify_aud": False},
                )
            except Exception as e:  # noqa: BLE001
                type(self).seen["bad"] = f"bad-signature: {e}"
                return self._json(401, {"error": "invalid_assertion"})
            type(self).seen["claims"] = claims
            return self._json(200, {"access_token": TOKEN, "expires_in": 3600})
        if self.path == "/api/do":
            type(self).seen["api_auth"] = self.headers.get("Authorization")
            if self.headers.get("Authorization") == f"Bearer {TOKEN}":
                return self._json(200, {"ok": True})
            return self._json(401, {"error": "unauthorized"})
        self._json(404, {})


@pytest.fixture()
def server():
    _Handler.seen = {"token_calls": 0, "claims": None, "api_auth": None, "bad": None}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _Handler.seen
    finally:
        srv.shutdown()


def _thing(base):
    return parse_thing(
        {
            "@context": "https://www.w3.org/2022/wot/td/v1.1",
            "id": "urn:dev:gcp",
            "title": "gcp",
            "securityDefinitions": {
                "sa": {"scheme": "oauth2", "flow": "client_credentials", "token": f"{base}/token"}
            },
            "security": ["sa"],
            "actions": {"do": {"forms": [{"href": f"{base}/api/do", "htv:methodName": "POST"}]}},
        }
    )


def _cred(base):
    # A service-account-style credential (Google's service_account.json shape).
    return {
        "client_email": ISS,
        "private_key": PRIVATE_KEY,
        "token_uri": f"{base}/token",
        "scopes": ["https://www.googleapis.com/auth/devstorage.read_only"],
    }


def _action_form(base):
    action = SimpleNamespace(thing_id="urn:dev:gcp", idempotent=False)
    form = SimpleNamespace(href=f"{base}/api/do", raw={"htv:methodName": "POST"})
    return action, form


async def test_jwt_bearer_signs_valid_assertion_and_attaches_bearer(server):
    base, seen = server
    inv = HttpInvoker(credentials={"urn:dev:gcp": _cred(base)}).with_security(_thing(base))
    action, form = _action_form(base)

    result = await inv.invoke(action, form, {"x": 1})

    assert result == {"ok": True}
    assert seen["bad"] is None
    # The assertion verified against the public key and carried the SA claims.
    assert seen["claims"]["iss"] == ISS
    assert seen["claims"]["scope"] == "https://www.googleapis.com/auth/devstorage.read_only"
    assert seen["claims"]["aud"] == f"{base}/token"
    assert seen["api_auth"] == f"Bearer {TOKEN}"


async def test_jwt_bearer_token_is_cached(server):
    base, seen = server
    inv = HttpInvoker(credentials={"urn:dev:gcp": _cred(base)}).with_security(_thing(base))
    action, form = _action_form(base)

    await inv.invoke(action, form, {})
    await inv.invoke(action, form, {})

    assert seen["token_calls"] == 1  # second call reused the cached token
