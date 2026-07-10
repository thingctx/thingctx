# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Part 2 (inbound) proofs for EntraGatewayGuard.

The five security assertions:
  (a) a valid token passes validate() and returns claims,
  (b) a wrong-audience token is rejected,
  (c) an expired token is rejected,
  (d) a token signed by the WRONG key is rejected (signature check works),
  (e) a token missing a required scope is rejected.

Plus: real signature verification is enabled (not disabled), a tampered
payload is caught, an 'alg: none' downgrade is refused, roles are enforced,
and the JWKS fetch is exercised against a monkeypatched Entra endpoint.
"""

from __future__ import annotations

import pytest
from conftest import AUDIENCE, ISSUER_V2, TENANT

from thingctx.identity import AuthorizationError, EntraGatewayGuard


def guard(keypair, **over) -> EntraGatewayGuard:
    """A guard pointed at the test JWKS (offline: no network)."""
    kw = dict(tenant_id=TENANT, audience=AUDIENCE, jwks=keypair.jwks())
    kw.update(over)
    return EntraGatewayGuard(**kw)


# --------------------------------------------------------------------------- #
# (a) a valid token passes and returns claims
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_valid_token_passes(keypair):
    g = guard(keypair, required_scopes=["Things.Invoke"])
    token = keypair.mint(scp="Things.Invoke")
    claims = await g.validate(token)
    assert claims["aud"] == AUDIENCE
    assert claims["tid"] == TENANT
    assert claims["iss"] == ISSUER_V2
    assert "Things.Invoke" in claims["scp"]


# --------------------------------------------------------------------------- #
# (b) wrong audience is rejected
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wrong_audience_rejected(keypair):
    g = guard(keypair)
    token = keypair.mint(aud="api://some-other-api")
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(token)
    assert "audience" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# (c) expired token is rejected
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_expired_token_rejected(keypair):
    g = guard(keypair)
    token = keypair.mint(exp_delta=-3600)  # expired an hour ago
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(token)
    assert "expired" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# (d) a token signed by the WRONG key is rejected -- signature verification
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wrong_key_signature_rejected(keypair, other_keypair):
    """The attacker signs with their own key but presents a kid that matches the
    tenant's published key. The guard uses the TENANT's public key to verify, so
    the signature check fails. This is the core security property."""
    g = guard(keypair)  # trusts only keypair's JWKS
    # attacker signs with other_keypair's private key, but claims keypair's kid
    forged = other_keypair.mint(kid=keypair.kid)
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(forged)
    assert "signature" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_wrong_key_unknown_kid_rejected(keypair, other_keypair):
    """The attacker signs with their own key AND presents their own kid, absent
    from the tenant JWKS: rejected because no trusted key matches the kid."""
    g = guard(keypair)
    forged = other_keypair.mint()  # uses attacker-key kid, not in keypair.jwks()
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(forged)
    assert "kid" in str(ei.value).lower() or "signature" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_tampered_payload_rejected(keypair):
    """Flip a byte in the token payload: the signature no longer matches."""
    g = guard(keypair)
    token = keypair.mint()
    header, payload, sig = token.split(".")
    # corrupt the payload segment (still base64url-ish so decoding gets far
    # enough to reach signature verification)
    bad_payload = payload[:-4] + ("AAAA" if payload[-4:] != "AAAA" else "BBBB")
    tampered = f"{header}.{bad_payload}.{sig}"
    with pytest.raises(AuthorizationError):
        await g.validate(tampered)


# --------------------------------------------------------------------------- #
# (e) missing required scope is rejected
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_required_scope_rejected(keypair):
    g = guard(keypair, required_scopes=["Things.Invoke"])
    token = keypair.mint(scp="Things.Read")  # has a scope, but not the needed one
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(token)
    assert "scope" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_no_scp_claim_rejected_when_scope_required(keypair):
    g = guard(keypair, required_scopes=["Things.Invoke"])
    token = keypair.mint(scp=None)  # app-only token with no scp at all
    with pytest.raises(AuthorizationError):
        await g.validate(token)


# --------------------------------------------------------------------------- #
# Extra hardening
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wrong_issuer_rejected(keypair):
    g = guard(keypair)
    token = keypair.mint(iss="https://login.microsoftonline.com/OTHER-TENANT/v2.0")
    with pytest.raises(AuthorizationError):
        await g.validate(token)


@pytest.mark.asyncio
async def test_alg_none_downgrade_refused(keypair):
    """An 'alg: none' unsigned token must be refused before any decode."""
    import jwt

    unsigned = jwt.encode(
        {"aud": AUDIENCE, "iss": ISSUER_V2, "tid": TENANT, "exp": 9999999999},
        key="",
        algorithm="none",
    )
    g = guard(keypair)
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(unsigned)
    assert "algorithm" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_app_role_enforced(keypair):
    g = guard(keypair, required_roles=["Device.Drive"])
    ok = keypair.mint(scp=None, roles=["Device.Drive"])
    claims = await g.validate(ok)
    assert "Device.Drive" in claims["roles"]

    missing = keypair.mint(scp=None, roles=["Device.Read"])
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(missing)
    assert "role" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_v1_issuer_accepted(keypair):
    """A v1 access token uses the sts.windows.net issuer form; accept it."""
    g = guard(keypair)
    token = keypair.mint(iss=f"https://sts.windows.net/{TENANT}/")
    claims = await g.validate(token)
    assert claims["tid"] == TENANT


@pytest.mark.asyncio
async def test_jwks_fetched_from_entra_endpoint(keypair, monkeypatch):
    """With no static jwks, the guard fetches the tenant's discovery keys over
    https; monkeypatch httpx to serve the test JWKS and confirm the right URL."""
    import httpx

    called = {}

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            called["url"] = url
            return FakeResp(keypair.jwks())

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    g = EntraGatewayGuard(tenant_id=TENANT, audience=AUDIENCE)  # no jwks -> fetches
    token = keypair.mint()
    claims = await g.validate(token)
    assert claims["tid"] == TENANT
    assert called["url"] == (f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys")


@pytest.mark.asyncio
async def test_signature_verification_is_actually_on(keypair):
    """Guard against a future regression that disables verification: prove that
    swapping the signing key alone (everything else identical) flips pass->fail."""
    g = guard(keypair)
    good = keypair.mint()
    assert (await g.validate(good))["tid"] == TENANT  # passes with the right key

    from conftest import KeyFixture

    impostor = KeyFixture(kid=keypair.kid)  # same kid, different key
    bad = impostor.mint()
    with pytest.raises(AuthorizationError):
        await g.validate(bad)  # must fail: only the signature differs
