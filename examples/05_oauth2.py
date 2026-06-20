"""A working OAuth2 (client-credentials) flow, end to end and offline.

Starts two real local HTTP endpoints in a background thread:

  * POST /token       an OAuth2 client-credentials token endpoint
  * POST /pump/speed  a protected resource that requires a valid bearer token

Then it points thingctx at a Thing Description whose security is ``oauth2``.
thingctx fetches a token from /token using the client id/secret you supply at
runtime (never in the TD), caches it, and attaches it to the protected call --
all from the description, with no OAuth code in your app.

    PYTHONPATH=src python examples/05_oauth2.py

No network, no real provider, no keys. Everything runs on localhost.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import thingctx

CLIENT_ID = "pump-agent"
CLIENT_SECRET = "s3cret"
VALID_TOKEN = "demo-access-token-123"

# Server-side counters so the demo can show what actually happened.
STATE = {"token_issued": 0, "speed": 0}


def _basic_creds(header: str):
    """Pull (client_id, client_secret) out of an HTTP Basic Authorization
    header, or (None, None) if it is not Basic."""
    import base64

    if not header.startswith("Basic "):
        return None, None
    try:
        decoded = base64.b64decode(header[6:]).decode()
        cid, _, secret = decoded.partition(":")
        return cid, secret
    except Exception:  # noqa: BLE001
        return None, None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the default stderr access log
        pass

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()

        if self.path == "/token":
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            # Accept the client secret either via HTTP Basic (RFC 6749 §2.3.1,
            # the method thingctx tries first) or in the body (client_secret_post).
            cid, secret = _basic_creds(self.headers.get("Authorization", ""))
            if cid is None:
                cid, secret = form.get("client_id"), form.get("client_secret")
            ok = (
                form.get("grant_type") == "client_credentials"
                and cid == CLIENT_ID
                and secret == CLIENT_SECRET
            )
            if not ok:
                return self._json(401, {"error": "invalid_client"})
            STATE["token_issued"] += 1
            return self._json(200, {"access_token": VALID_TOKEN, "expires_in": 3600})

        if self.path == "/pump/speed":
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {VALID_TOKEN}":
                return self._json(401, {"error": "unauthorized"})
            body = json.loads(raw or "{}")
            STATE["speed"] = body.get("rpm", 0)
            return self._json(200, {"ok": True, "rpm": STATE["speed"]})

        self._json(404, {"error": "not found"})


def _td(base: str) -> dict:
    """A Thing whose protected action is reached over an oauth2-secured form.
    The TD declares the *scheme and token endpoint*; the secret is supplied to
    the invoker at runtime, so this document is safe to commit and share."""
    return {
        "@context": ["https://www.w3.org/2022/wot/td/v1.1", {"htv": "http://www.w3.org/2011/http#"}],
        "id": "urn:thingctx:demo-pump",
        "title": "Demo Pump (OAuth2)",
        "securityDefinitions": {
            "oauth": {
                "scheme": "oauth2",
                "flow": "client_credentials",
                "token": f"{base}/token",
                "scopes": ["pump:write"],
            }
        },
        "security": ["oauth"],
        "actions": {
            "set_speed": {
                "title": "set_speed",
                "description": "Set the pump speed in RPM.",
                "input": {"type": "object", "properties": {"rpm": {"type": "integer"}}},
                "forms": [{"href": f"{base}/pump/speed", "htv:methodName": "POST"}],
            }
        },
    }


async def run(base: str) -> None:
    td = _td(base)

    # The only place a secret appears: handed to the invoker at runtime, keyed
    # by the Thing id. The TD itself carries no credential.
    creds = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    invoker = thingctx.HttpInvoker(credentials={"urn:thingctx:demo-pump": creds})
    client = thingctx.ThingClient(tds=[td], invokers=[invoker])

    print("actions:", [t["function"]["name"] for t in client.list_actions()])

    # First call: no token cached -> thingctx hits /token, then the API.
    r1 = await client.invoke("demo-pump.set_speed", {"rpm": 1200})
    print(f"set_speed(1200) -> {r1}   (tokens issued: {STATE['token_issued']})")
    assert r1 == {"ok": True, "rpm": 1200}

    # Second call: token is cached, so /token is NOT hit again.
    r2 = await client.invoke("demo-pump.set_speed", {"rpm": 1800})
    print(f"set_speed(1800) -> {r2}   (tokens issued: {STATE['token_issued']})")
    assert r2 == {"ok": True, "rpm": 1800}
    assert STATE["token_issued"] == 1, "token should have been cached, not re-fetched"

    # A wrong secret never gets a token, so the protected call is refused.
    bad_creds = {"client_id": CLIENT_ID, "client_secret": "wrong"}
    bad = thingctx.HttpInvoker(credentials={"urn:thingctx:demo-pump": bad_creds})
    bad_client = thingctx.ThingClient(tds=[td], invokers=[bad])
    try:
        await bad_client.invoke("demo-pump.set_speed", {"rpm": 9999})
        raise AssertionError("a bad secret should not have been able to set speed")
    except Exception as exc:  # noqa: BLE001 - any auth failure is the point
        print(f"bad secret refused: {type(exc).__name__}")

    assert STATE["speed"] == 1800, "the rejected call must not have changed device state"
    print("\nOK: thingctx ran the OAuth2 flow from the TD; the device is protected.")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(run(base))
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
