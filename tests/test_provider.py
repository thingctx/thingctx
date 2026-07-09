# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Part 1 (outbound) proofs for EntraAuth.

Covers: the thingctx CredentialProvider contract; matches() claims an
Entra-flagged oauth2 scheme and rejects a non-Entra one; the .default scope
normalization for several inputs; resolve() with a mocked azure-identity
credential returns a real thingctx BearerToken; and the token reaches an HTTP
binding when driven through a real ThingClient.
"""

from __future__ import annotations

from collections import namedtuple

import pytest

from thingctx import BearerToken, CredentialProvider
from thingctx.auth.context import AuthContext
from thingctx.identity import EntraAuth, make_provider
from thingctx.identity.provider import normalize_default_scope
from thingctx.testing import assert_provider_contract
from thingctx.thing import WoTSecurityScheme

# azure.core's AccessToken is (token, expires_on); a namedtuple is a faithful stand-in.
FakeAccessToken = namedtuple("FakeAccessToken", ["token", "expires_on"])


class FakeCredential:
    """A stand-in for an azure-identity credential: records the scope it was
    asked for and returns a fixed token, so resolve() runs with no Azure."""

    def __init__(self, token: str = "fake-entra-access-token") -> None:
        self.token = token
        self.asked_scopes: list[str] = []

    def get_token(self, *scopes, **kwargs):
        self.asked_scopes.append(scopes[0] if scopes else None)
        import time

        return FakeAccessToken(token=self.token, expires_on=int(time.time()) + 3600)


def entra_scheme(**over) -> WoTSecurityScheme:
    raw = {
        "scheme": "oauth2",
        "flow": "client_credentials",
        "token": "https://login.microsoftonline.com/my-tenant/oauth2/v2.0/token",
        "x-thingctx-auth": "entra",
        **over.pop("raw", {}),
    }
    return WoTSecurityScheme(
        name=over.pop("name", "entra_oauth"),
        scheme="oauth2",
        token=raw["token"],
        scopes=over.pop("scopes", ("https://graph.microsoft.com/.default",)),
        raw=raw,
        **over,
    )


# --------------------------------------------------------------------------- #
# (a) contract
# --------------------------------------------------------------------------- #


def test_provider_satisfies_thingctx_contract():
    p = make_provider()
    assert_provider_contract(p)  # raises AssertionError on any breach
    assert isinstance(p, CredentialProvider)
    assert p.name == "entra"


# --------------------------------------------------------------------------- #
# (b) matches()
# --------------------------------------------------------------------------- #


def test_matches_claims_entra_by_marker():
    p = EntraAuth()
    assert p.matches(entra_scheme(), None) is True


def test_matches_claims_entra_by_token_host():
    p = EntraAuth()
    s = WoTSecurityScheme(
        name="aad",
        scheme="oauth2",
        token="https://login.microsoftonline.com/tid/oauth2/v2.0/token",
        raw={
            "scheme": "oauth2",
            "token": "https://login.microsoftonline.com/tid/oauth2/v2.0/token",
        },
    )
    assert p.matches(s, None) is True


def test_matches_claims_entra_by_tenant_field():
    p = EntraAuth()
    s = WoTSecurityScheme(
        name="aad",
        scheme="oauth2",
        raw={"scheme": "oauth2", "tenant": "my-tenant", "flow": "client_credentials"},
    )
    assert p.matches(s, None) is True


def test_matches_rejects_non_entra_oauth2():
    p = EntraAuth()
    s = WoTSecurityScheme(
        name="github",
        scheme="oauth2",
        token="https://github.com/login/oauth/access_token",
        raw={"scheme": "oauth2", "token": "https://github.com/login/oauth/access_token"},
    )
    assert p.matches(s, None) is False


def test_matches_rejects_non_oauth2_schemes():
    p = EntraAuth()
    for kind in ("basic", "bearer", "apikey", "nosec"):
        s = WoTSecurityScheme(name=kind, scheme=kind, raw={"scheme": kind})
        assert p.matches(s, None) is False


# --------------------------------------------------------------------------- #
# (c) .default scope normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "given,expected",
    [
        # already correct
        ("https://graph.microsoft.com/.default", "https://graph.microsoft.com/.default"),
        # a bare resource URI -> append /.default
        ("https://graph.microsoft.com", "https://graph.microsoft.com/.default"),
        ("https://graph.microsoft.com/", "https://graph.microsoft.com/.default"),
        # an app id uri -> append /.default
        ("api://c0ffee00-1234", "api://c0ffee00-1234/.default"),
        ("api://my-api", "api://my-api/.default"),
        # a delegated scope (misconfig for client credentials) -> resource + .default
        ("https://graph.microsoft.com/User.Read", "https://graph.microsoft.com/.default"),
        ("https://vault.azure.net/user_impersonation", "https://vault.azure.net/.default"),
        # a bare resource id with no scheme
        ("https://management.azure.com", "https://management.azure.com/.default"),
    ],
)
def test_default_scope_normalization(given, expected):
    assert normalize_default_scope(given) == expected


def test_default_scope_rejects_empty():
    with pytest.raises(ValueError):
        normalize_default_scope("")


# --------------------------------------------------------------------------- #
# (d) resolve() with a mocked credential returns a BearerToken
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_returns_bearer_token_from_mocked_credential():
    fake = FakeCredential(token="TOKEN-abc123")
    p = EntraAuth(credential=fake)  # inject the mock; no Azure touched
    ctx = AuthContext(scheme=entra_scheme(), credential=None, owner_id="urn:demo")
    cred = await p.resolve(ctx)

    assert isinstance(cred, BearerToken)
    assert cred.token.get_secret_value() == "TOKEN-abc123"
    # the scope handed to azure-identity was the normalized .default form
    assert fake.asked_scopes == ["https://graph.microsoft.com/.default"]


@pytest.mark.asyncio
async def test_resolve_normalizes_resource_uri_scope():
    fake = FakeCredential()
    p = EntraAuth(credential=fake)
    s = entra_scheme(scopes=("https://vault.azure.net",))  # bare resource URI
    ctx = AuthContext(scheme=s, credential=None, owner_id="urn:demo")
    await p.resolve(ctx)
    assert fake.asked_scopes == ["https://vault.azure.net/.default"]


@pytest.mark.asyncio
async def test_resolve_monkeypatched_default_credential(monkeypatch):
    """Patch DefaultAzureCredential.get_token itself (the task's exact ask):
    the provider builds the default chain and still returns a real BearerToken."""
    import time

    import azure.identity

    def fake_get_token(self, *scopes, **kwargs):
        return FakeAccessToken(token="default-chain-token", expires_on=int(time.time()) + 3600)

    monkeypatch.setattr(azure.identity.DefaultAzureCredential, "get_token", fake_get_token)

    p = EntraAuth()  # no injected credential -> builds DefaultAzureCredential
    ctx = AuthContext(scheme=entra_scheme(), credential=None, owner_id="urn:demo")
    cred = await p.resolve(ctx)
    assert isinstance(cred, BearerToken)
    assert cred.token.get_secret_value() == "default-chain-token"


@pytest.mark.asyncio
async def test_resolve_uses_thingctx_cache_on_second_call():
    fake = FakeCredential(token="cached-token")
    p = EntraAuth(credential=fake)
    shared_cache: dict = {}
    ctx1 = AuthContext(
        scheme=entra_scheme(), credential=None, owner_id="urn:demo", cache=shared_cache
    )
    ctx2 = AuthContext(
        scheme=entra_scheme(), credential=None, owner_id="urn:demo", cache=shared_cache
    )
    await p.resolve(ctx1)
    await p.resolve(ctx2)
    # second call served from thingctx's cache: azure-identity asked only once
    assert len(fake.asked_scopes) == 1


# --------------------------------------------------------------------------- #
# (e) end to end through a real ThingClient + HttpBinding
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_token_reaches_http_binding_through_thingclient(monkeypatch):
    """Drive a real ThingClient over a bearer-secured Entra TD. The mocked
    provider supplies the token; assert it lands in the outgoing Authorization
    header the HTTP binding builds."""
    from thingctx import HttpBinding, ThingClient

    td = {
        "@context": "https://www.w3.org/2019/wot/td/v1.1",
        "id": "urn:thingctx:entra-demo:v1",
        "title": "Entra Demo API",
        "securityDefinitions": {
            "entra_oauth": {
                "scheme": "oauth2",
                "flow": "client_credentials",
                "token": "https://login.microsoftonline.com/my-tenant/oauth2/v2.0/token",
                "scopes": ["https://graph.microsoft.com/.default"],
                "x-thingctx-auth": "entra",
            }
        },
        "security": ["entra_oauth"],
        "actions": {
            "ping": {"forms": [{"href": "https://api.example.com/ping", "htv:methodName": "POST"}]}
        },
    }

    fake = FakeCredential(token="ENTRA-BEARER-XYZ")
    entra = EntraAuth(credential=fake)

    # Capture the request the binding builds, so we can read its Authorization
    # header without a real server. httpx AsyncClient.send is the choke point.
    captured = {}

    import httpx

    async def fake_send(self, request, **kwargs):
        captured["auth"] = request.headers.get("Authorization")
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)

    binding = HttpBinding(extra_auth=[entra])  # register our provider on the binding
    client = ThingClient(tds=[td], bindings=[binding])
    result = await client.invoke("entra-demo.ping", {})
    await client.aclose()

    assert result == {"ok": True}
    assert captured["auth"] == "Bearer ENTRA-BEARER-XYZ"
    # and azure-identity was asked for the normalized scope
    assert fake.asked_scopes == ["https://graph.microsoft.com/.default"]
