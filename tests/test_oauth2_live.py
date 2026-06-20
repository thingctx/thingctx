"""OAuth2 against a real local server (no mocked transport).

The other OAuth2 tests use httpx.MockTransport to assert logic. This one runs a
real HTTP token endpoint + protected resource on a loopback socket, so the
actual httpx request path is exercised: real client-credentials grant, real
HTTP Basic client auth, real bearer attach. It proves the flow is not an
artifact of the mock.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from thingctx import HttpInvoker, parse_thing

CLIENT_ID = "svc-agent"
CLIENT_SECRET = "top-secret"
TOKEN = "real-access-token"


class _Handler(BaseHTTPRequestHandler):
    seen = {"token_auth_style": None, "api_auth": None, "token_calls": 0}

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
            auth = self.headers.get("Authorization", "")
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            if auth.startswith("Basic "):
                cid, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
                type(self).seen["token_auth_style"] = "basic"
            else:
                cid, secret = form.get("client_id"), form.get("client_secret")
                type(self).seen["token_auth_style"] = "post"
            if cid == CLIENT_ID and secret == CLIENT_SECRET:
                return self._json(200, {"access_token": TOKEN, "expires_in": 3600})
            return self._json(401, {"error": "invalid_client"})

        if self.path == "/api/do":
            type(self).seen["api_auth"] = self.headers.get("Authorization")
            if self.headers.get("Authorization") == f"Bearer {TOKEN}":
                return self._json(200, {"ok": True})
            return self._json(401, {"error": "unauthorized"})

        self._json(404, {})


@pytest.fixture()
def server():
    _Handler.seen = {"token_auth_style": None, "api_auth": None, "token_calls": 0}
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
            "id": "urn:dev:svc",
            "title": "svc",
            "securityDefinitions": {
                "oauth": {
                    "scheme": "oauth2",
                    "flow": "client_credentials",
                    "token": f"{base}/token",
                    "scopes": ["do:write"],
                }
            },
            "security": ["oauth"],
            "actions": {
                "do": {"forms": [{"href": f"{base}/api/do", "htv:methodName": "POST"}]}
            },
        }
    )


def _action_form(base):
    action = SimpleNamespace(thing_id="urn:dev:svc", idempotent=False)
    form = SimpleNamespace(href=f"{base}/api/do", raw={"htv:methodName": "POST"})
    return action, form


async def test_real_oauth2_flow_uses_basic_and_attaches_bearer(server):
    base, seen = server
    inv = HttpInvoker(
        credentials={"urn:dev:svc": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}
    ).with_security(_thing(base))
    action, form = _action_form(base)

    result = await inv.invoke(action, form, {"x": 1})

    assert result == {"ok": True}
    # Over a real socket: client auth was HTTP Basic, the API saw the bearer.
    assert seen["token_auth_style"] == "basic"
    assert seen["api_auth"] == f"Bearer {TOKEN}"


async def test_real_token_caching_one_fetch(server):
    base, seen = server
    inv = HttpInvoker(
        credentials={"urn:dev:svc": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}
    ).with_security(_thing(base))
    action, form = _action_form(base)

    await inv.invoke(action, form, {})
    await inv.invoke(action, form, {})

    assert seen["token_calls"] == 1  # cached across the two calls


async def test_real_bad_secret_is_refused(server):
    base, seen = server
    import httpx

    inv = HttpInvoker(
        credentials={"urn:dev:svc": {"client_id": CLIENT_ID, "client_secret": "wrong"}}
    ).with_security(_thing(base))
    action, form = _action_form(base)

    with pytest.raises(httpx.HTTPStatusError):
        await inv.invoke(action, form, {})
    assert seen["api_auth"] is None  # never reached the protected resource


async def test_non_https_token_endpoint_is_refused_by_default():
    """A non-loopback, non-https token endpoint must be rejected before any
    secret leaves the process."""
    thing = parse_thing(
        {
            "@context": "https://www.w3.org/2022/wot/td/v1.1",
            "id": "urn:dev:svc",
            "title": "svc",
            "securityDefinitions": {
                "oauth": {"scheme": "oauth2", "token": "http://insecure.example.com/token"}
            },
            "security": ["oauth"],
            "actions": {
                "do": {"forms": [{"href": "https://api.example.com/do", "htv:methodName": "POST"}]}
            },
        }
    )
    inv = HttpInvoker(
        credentials={"urn:dev:svc": {"client_id": "a", "client_secret": "b"}}
    ).with_security(thing)
    action = SimpleNamespace(thing_id="urn:dev:svc", idempotent=False)
    form = SimpleNamespace(href="https://api.example.com/do", raw={"htv:methodName": "POST"})

    with pytest.raises(ValueError, match="non-https"):
        await inv.invoke(action, form, {})
