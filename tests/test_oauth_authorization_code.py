"""User-consent OAuth: authorization-code provider, token store, PKCE consent."""

from __future__ import annotations

import base64
import hashlib
import stat
import threading
import time
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

from thingctx.auth import oauth_consent, providers
from thingctx.auth.context import AuthContext
from thingctx.auth.credentials import BearerToken
from thingctx.auth.providers import OAuth2AuthorizationCodeAuth
from thingctx.auth.registry import DEFAULT_AUTH
from thingctx.auth.store import FileTokenStore, MemoryTokenStore, token_key
from thingctx.openapi import _security_from_spec


def _scheme(**kw):
    base = {
        "scheme": "oauth2",
        "flow": "code",
        "token": "https://idp/token",
        "scopes": (),
        "name": "s",
    }
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# token store
# --------------------------------------------------------------------------- #


def test_token_key_is_scope_order_independent():
    a = token_key("urn:t", "https://idp/token", ["b", "a"])
    b = token_key("urn:t", "https://idp/token", ["a", "b"])
    assert a == b


def test_memory_store_roundtrip_and_isolation():
    s = MemoryTokenStore()
    s.set("k", {"refresh_token": "r1"})
    got = s.get("k")
    assert got == {"refresh_token": "r1"}
    got["refresh_token"] = "mutated"  # a copy, not the stored dict
    assert s.get("k")["refresh_token"] == "r1"
    s.delete("k")
    assert s.get("k") is None


def test_file_store_roundtrip_and_perms(tmp_path):
    path = tmp_path / "tokens.json"
    s = FileTokenStore(path)
    s.set("k", {"refresh_token": "secret"})
    assert s.get("k")["refresh_token"] == "secret"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)
    s.delete("k")
    assert s.get("k") is None


# --------------------------------------------------------------------------- #
# provider: silent refresh only
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_provider_refreshes_from_stored_token(monkeypatch):
    store = MemoryTokenStore()
    key = token_key("urn:t", "https://idp/token", ())
    store.set(key, {"refresh_token": "r-old", "client_id": "cid"})

    async def fake_refresh(token_url, cid, secret, refresh, scopes, *, timeout):
        assert refresh == "r-old"
        assert cid == "cid"
        return {"access_token": "AT", "expires_in": 3600}

    monkeypatch.setattr(providers, "_refresh_grant", fake_refresh)
    prov = OAuth2AuthorizationCodeAuth(store=store)
    ctx = AuthContext(scheme=_scheme(), credential={"client_id": "cid"}, owner_id="urn:t")
    cred = await prov.resolve(ctx)
    assert isinstance(cred, BearerToken)
    assert cred.token.get_secret_value() == "AT"
    # Second call is served from the access-token cache (no refresh).
    monkeypatch.setattr(
        providers, "_refresh_grant", lambda *a, **k: pytest.fail("should be cached")
    )
    again = await prov.resolve(ctx)
    assert again.token.get_secret_value() == "AT"


@pytest.mark.asyncio
async def test_provider_persists_rotated_refresh_token(monkeypatch):
    store = MemoryTokenStore()
    key = token_key("urn:t", "https://idp/token", ())
    store.set(key, {"refresh_token": "r-old", "client_id": "cid"})

    async def fake_refresh(*a, **k):
        return {"access_token": "AT", "refresh_token": "r-new", "expires_in": 1}

    monkeypatch.setattr(providers, "_refresh_grant", fake_refresh)
    prov = OAuth2AuthorizationCodeAuth(store=store)
    ctx = AuthContext(scheme=_scheme(), credential={"client_id": "cid"}, owner_id="urn:t")
    await prov.resolve(ctx)
    assert store.get(key)["refresh_token"] == "r-new"


@pytest.mark.asyncio
async def test_provider_uses_client_secret_from_store(monkeypatch):
    # The MCP/agent case: no runtime credential carries a secret, so the
    # confidential client's secret must come from what consent persisted.
    store = MemoryTokenStore()
    key = token_key("urn:t", "https://idp/token", ())
    store.set(key, {"refresh_token": "r", "client_id": "cid", "client_secret": "shh"})
    seen = {}

    async def fake_refresh(token_url, cid, secret, refresh, scopes, *, timeout):
        seen["cid"], seen["secret"] = cid, secret
        return {"access_token": "AT", "expires_in": 3600}

    monkeypatch.setattr(providers, "_refresh_grant", fake_refresh)
    prov = OAuth2AuthorizationCodeAuth(store=store)
    ctx = AuthContext(scheme=_scheme(), credential=None, owner_id="urn:t")
    cred = await prov.resolve(ctx)
    assert isinstance(cred, BearerToken)
    assert seen == {"cid": "cid", "secret": "shh"}


@pytest.mark.asyncio
async def test_runtime_secret_overrides_stored(monkeypatch):
    store = MemoryTokenStore()
    key = token_key("urn:t", "https://idp/token", ())
    store.set(key, {"refresh_token": "r", "client_id": "cid", "client_secret": "stored"})
    seen = {}

    async def fake_refresh(token_url, cid, secret, refresh, scopes, *, timeout):
        seen["secret"] = secret
        return {"access_token": "AT", "expires_in": 3600}

    monkeypatch.setattr(providers, "_refresh_grant", fake_refresh)
    prov = OAuth2AuthorizationCodeAuth(store=store)
    ctx = AuthContext(
        scheme=_scheme(),
        credential={"client_id": "cid", "client_secret": "runtime"},
        owner_id="urn:t",
    )
    await prov.resolve(ctx)
    assert seen["secret"] == "runtime"


@pytest.mark.asyncio
async def test_rotation_preserves_client_secret(monkeypatch):
    store = MemoryTokenStore()
    key = token_key("urn:t", "https://idp/token", ())
    store.set(key, {"refresh_token": "r-old", "client_id": "cid", "client_secret": "shh"})

    async def fake_refresh(*a, **k):
        return {"access_token": "AT", "refresh_token": "r-new", "expires_in": 1}

    monkeypatch.setattr(providers, "_refresh_grant", fake_refresh)
    prov = OAuth2AuthorizationCodeAuth(store=store)
    ctx = AuthContext(scheme=_scheme(), credential=None, owner_id="urn:t")
    await prov.resolve(ctx)
    rec = store.get(key)
    assert rec["refresh_token"] == "r-new"
    assert rec["client_secret"] == "shh"


@pytest.mark.asyncio
async def test_provider_raises_without_stored_token():
    prov = OAuth2AuthorizationCodeAuth(store=MemoryTokenStore())
    ctx = AuthContext(scheme=_scheme(), credential={"client_id": "cid"}, owner_id="urn:t")
    with pytest.raises(RuntimeError, match="no stored refresh token"):
        await prov.resolve(ctx)


def test_provider_matches_only_code_flow():
    prov = OAuth2AuthorizationCodeAuth()
    assert prov.matches(_scheme(flow="code"), {})
    assert prov.matches(_scheme(flow="authorization_code"), {})
    assert not prov.matches(_scheme(flow="client_credentials"), {})


def test_registry_routes_code_and_client_credentials():
    code = DEFAULT_AUTH.resolve(_scheme(flow="code"), {"client_id": "x"})
    assert isinstance(code, OAuth2AuthorizationCodeAuth)
    cc = DEFAULT_AUTH.resolve(_scheme(flow="client_credentials"), {"client_id": "x"})
    assert cc.name == "oauth2-client-credentials"


# --------------------------------------------------------------------------- #
# PKCE + consent
# --------------------------------------------------------------------------- #


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = oauth_consent.pkce_pair()
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expect.decode()


def test_build_authorization_url_has_pkce_and_offline():
    url = oauth_consent.build_authorization_url(
        "https://idp/auth",
        client_id="cid",
        redirect_uri="http://127.0.0.1:5000/",
        scopes=["a", "b"],
        state="st",
        code_challenge="ch",
        offline=True,
    )
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q["response_type"] == "code"
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"] == "ch"
    assert q["state"] == "st"
    assert q["access_type"] == "offline"
    assert q["scope"] == "a b"


def test_authorize_code_flow_end_to_end(monkeypatch):
    # Capture the real (port-filled) URL the flow would open, then drive the
    # loopback redirect from a thread as the browser would.
    def fake_open(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        redirect = urllib.parse.urlparse(q["redirect_uri"])
        state = q["state"]

        def hit():
            # http.client, not urllib, so the patched urlopen below (the token
            # exchange seam) does not swallow this loopback request.
            import http.client

            for _ in range(50):
                try:
                    conn = http.client.HTTPConnection(redirect.hostname, redirect.port, timeout=1)
                    conn.request("GET", f"/?code=AUTHCODE&state={state}")
                    conn.getresponse().read()
                    conn.close()
                    return
                except OSError:
                    time.sleep(0.02)

        threading.Thread(target=hit, daemon=True).start()

    monkeypatch.setattr(oauth_consent.webbrowser, "open", fake_open)

    class FakeResp:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["data"] = dict(urllib.parse.parse_qsl(req.data.decode()))
        return FakeResp(b'{"access_token":"AT","refresh_token":"RT","expires_in":3600}')

    monkeypatch.setattr(oauth_consent.urllib.request, "urlopen", fake_urlopen)

    tok = oauth_consent.authorize_code_flow(
        authorization_url="https://idp/auth",
        token_url="https://idp/token",
        client_id="cid",
        client_secret="sec",
        scopes=["a"],
        timeout=5.0,
    )
    assert tok["refresh_token"] == "RT"
    assert seen["data"]["grant_type"] == "authorization_code"
    assert seen["data"]["code"] == "AUTHCODE"
    assert seen["data"]["code_verifier"]  # PKCE verifier sent at exchange


def test_login_persists_refresh_token(monkeypatch):
    monkeypatch.setattr(
        oauth_consent,
        "authorize_code_flow",
        lambda **kw: {"access_token": "AT", "refresh_token": "RT"},
    )
    store = MemoryTokenStore()
    oauth_consent.login(
        authorization_url="https://idp/auth",
        token_url="https://idp/token",
        client_id="cid",
        scopes=["a"],
        owner_id="urn:t",
        store=store,
    )
    rec = store.get(token_key("urn:t", "https://idp/token", ["a"]))
    assert rec["refresh_token"] == "RT"
    assert "client_secret" not in rec  # a public/PKCE client persists no secret


def test_login_persists_client_secret(monkeypatch):
    monkeypatch.setattr(
        oauth_consent,
        "authorize_code_flow",
        lambda **kw: {"access_token": "AT", "refresh_token": "RT"},
    )
    store = MemoryTokenStore()
    oauth_consent.login(
        authorization_url="https://idp/auth",
        token_url="https://idp/token",
        client_id="cid",
        client_secret="sec",
        scopes=["a"],
        owner_id="urn:t",
        store=store,
    )
    rec = store.get(token_key("urn:t", "https://idp/token", ["a"]))
    assert rec["client_secret"] == "sec"


# --------------------------------------------------------------------------- #
# OpenAPI mapping
# --------------------------------------------------------------------------- #


def test_openapi_maps_authorization_code_flow():
    spec = {
        "components": {
            "securitySchemes": {
                "oauth": {
                    "type": "oauth2",
                    "flows": {
                        "authorizationCode": {
                            "authorizationUrl": "https://idp/auth",
                            "tokenUrl": "https://idp/token",
                            "scopes": {"https://example/scope": "desc"},
                        }
                    },
                }
            }
        },
        "security": [{"oauth": []}],
    }
    defs, active = _security_from_spec(spec)
    assert defs["oauth"]["flow"] == "code"
    assert defs["oauth"]["authorization"] == "https://idp/auth"
    assert defs["oauth"]["token"] == "https://idp/token"
    assert defs["oauth"]["scopes"] == ["https://example/scope"]
    assert active == ["oauth"]
