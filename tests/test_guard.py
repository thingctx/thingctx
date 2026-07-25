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

import time

import jwt
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


# --------------------------------------------------------------------------- #
# iat is enforced (GAP 2 fix): the guard's docstring claims iat is verified, so
# the code must actually verify it, not just say so.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_future_iat_rejected(keypair):
    # invariant ID-3: iat is verified; a token minted in the future (beyond the
    # leeway) is rejected, so a token issued "ahead of time" is not honored early.
    g = guard(keypair)
    ahead = keypair.mint(extra={"iat": int(time.time()) + 3600})  # issued an hour ahead
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(ahead)
    assert "iat" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_normal_iat_passes(keypair):
    # invariant ID-3: a token with a normal (present-time) iat still validates, so
    # enforcing iat does not reject legitimate tokens.
    g = guard(keypair)
    claims = await g.validate(keypair.mint())  # mint sets iat = now
    assert claims["tid"] == TENANT


@pytest.mark.asyncio
async def test_iat_within_leeway_passes(keypair):
    # invariant ID-3: a slightly-future iat within the clock-skew leeway is
    # accepted, so a small clock difference between issuer and gateway is tolerated.
    g = guard(keypair, leeway=60)
    ok = keypair.mint(extra={"iat": int(time.time()) + 30})  # 30s ahead, inside 60s leeway
    assert (await g.validate(ok))["tid"] == TENANT


@pytest.mark.asyncio
async def test_missing_iat_rejected(keypair):
    # invariant ID-3: iat is required; a token that carries no iat at all is
    # refused, so the guard's "tokens carry iat" policy is real.
    g = guard(keypair)
    now = int(time.time())
    no_iat = jwt.encode(
        {
            "iss": ISSUER_V2,
            "aud": AUDIENCE,
            "tid": TENANT,
            "exp": now + 3600,
            "nbf": now - 10,
            "sub": "caller",
        },
        keypair.private_pem,
        algorithm="RS256",
        headers={"kid": keypair.kid},
    )
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(no_iat)
    assert "iat" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# ID-6: a JWKS fetch failure fails closed (never admits an unverifiable token).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_jwks_fetch_failure_fails_closed(keypair, monkeypatch):
    # invariant ID-6: when the JWKS fetch fails (network error, timeout, non-2xx,
    # bad JSON), validate RAISES and never admits the token; a fetch failure is
    # never swallowed into a pass.
    import httpx

    class FailingClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise httpx.ConnectError("no route to the identity provider")

    monkeypatch.setattr(httpx, "AsyncClient", FailingClient)
    g = EntraGatewayGuard(tenant_id=TENANT, audience=AUDIENCE)  # no static jwks -> must fetch
    token = keypair.mint()  # a perfectly valid token; only the key fetch fails
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(token)
    assert "signing keys" in str(ei.value).lower() or "fetch" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_jwks_fetch_non_2xx_fails_closed(keypair, monkeypatch):
    # invariant ID-6: a non-2xx JWKS response also denies; raise_for_status raising
    # must not fall through to admitting the token.
    import httpx

    class ErrorResp:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("500", request=None, response=None)

        def json(self):  # pragma: no cover - never reached; raise_for_status raises first
            return {}

    class ErrorClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return ErrorResp()

    monkeypatch.setattr(httpx, "AsyncClient", ErrorClient)
    g = EntraGatewayGuard(tenant_id=TENANT, audience=AUDIENCE)
    with pytest.raises(AuthorizationError):
        await g.validate(keypair.mint())


# --------------------------------------------------------------------------- #
# ID-7: on a kid miss the JWKS is refetched once (key rotation), rate-limited by
# a cooldown; during cooldown a kid miss is re-raised, never accepted.
# --------------------------------------------------------------------------- #


def _rotating_jwks_client(jwks_by_call: list[dict], counter: dict):
    """A fake httpx AsyncClient whose JWKS GET returns the next set in the list, so
    a test can simulate the provider rotating its keys between fetches. ``counter``
    records how many fetches happened."""

    class Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            i = min(counter["n"], len(jwks_by_call) - 1)
            counter["n"] += 1
            return Resp(jwks_by_call[i])

    return Client


@pytest.mark.asyncio
async def test_kid_miss_refetches_and_picks_up_rotation(keypair, other_keypair, monkeypatch):
    # invariant ID-7: a token whose kid is absent from the cached JWKS triggers a
    # single forced refetch; if the provider rotated, the new key is picked up and
    # the token validates. The old JWKS is served first, the rotated one second.
    import httpx
    from conftest import KeyFixture

    rotated = KeyFixture(kid="rotated-key")
    counter = {"n": 0}
    fake_client = _rotating_jwks_client([keypair.jwks(), rotated.jwks()], counter)
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    g = EntraGatewayGuard(tenant_id=TENANT, audience=AUDIENCE)  # fetches its keys
    # Prime the cache with the old JWKS by validating an old-kid token first.
    assert (await g.validate(keypair.mint()))["tid"] == TENANT
    # A token signed by the rotated key (new kid) is a cache miss -> forced refetch
    # picks up the rotation and the token validates.
    assert (await g.validate(rotated.mint()))["tid"] == TENANT
    assert counter["n"] >= 2  # the rotation refetch actually happened


@pytest.mark.asyncio
async def test_kid_miss_during_cooldown_is_rejected(keypair, monkeypatch):
    # invariant ID-7: a second kid miss inside the cooldown window does NOT refetch
    # again (a flood of random-kid tokens must not amplify into unbounded fetches);
    # the miss is re-raised, never accepted.
    import httpx
    from conftest import KeyFixture

    counter = {"n": 0}
    # The provider never serves a matching key: every fetch returns the same JWKS.
    fake_client = _rotating_jwks_client([keypair.jwks()], counter)
    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    g = EntraGatewayGuard(tenant_id=TENANT, audience=AUDIENCE)
    await g.validate(keypair.mint())  # prime the cache
    before = counter["n"]
    attacker = KeyFixture(kid="never-published")
    # First unknown-kid token: one forced refetch, still a miss -> rejected.
    with pytest.raises(AuthorizationError):
        await g.validate(attacker.mint())
    after_first = counter["n"]
    assert after_first == before + 1  # exactly one forced refetch
    # A second unknown-kid token inside the cooldown must NOT refetch again.
    with pytest.raises(AuthorizationError):
        await g.validate(attacker.mint())
    assert counter["n"] == after_first  # no further fetch during cooldown


async def test_jwks_endpoint_answering_a_bare_list_fails_closed(keypair, monkeypatch):
    """A 2xx says nothing about shape. A provider that answers a bare key array
    must be refused as a 401, not cached: caching it wedges every later request
    for the whole TTL, and the AttributeError that follows escapes past this
    guard's fail-closed boundary as a 500."""
    import httpx

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return keypair.jwks()["keys"]  # the array, not the {"keys": [...]} object

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    g = EntraGatewayGuard(tenant_id=TENANT, audience=AUDIENCE)
    with pytest.raises(AuthorizationError):
        await g.validate(keypair.mint())

    # and the bad shape was never cached, so a later good fetch still works
    assert g._jwks_cache is None


async def test_jwks_with_a_non_list_keys_member_fails_closed(keypair, monkeypatch):
    """The object check is not enough on its own: a str ``keys`` iterates into
    characters before it fails, so the AttributeError lands in _signing_key and
    escapes as a 500. Validate the key list too, and refuse before caching."""
    import httpx

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": "not-a-list"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    g = EntraGatewayGuard(tenant_id=TENANT, audience=AUDIENCE)
    with pytest.raises(AuthorizationError):
        await g.validate(keypair.mint())
    assert g._jwks_cache is None


async def test_jwks_with_non_object_key_entries_fails_closed(keypair, monkeypatch):
    """A list of the right type is still not a key set: a str element reaches
    jwk.get() and raises past the fail-closed boundary."""
    import httpx

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": ["not-a-dict"]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    g = EntraGatewayGuard(tenant_id=TENANT, audience=AUDIENCE)
    with pytest.raises(AuthorizationError):
        await g.validate(keypair.mint())
    assert g._jwks_cache is None


async def test_jwks_with_an_empty_key_list_fails_closed(keypair, monkeypatch):
    """An empty key set verifies nothing. Caching it wedges auth for the whole TTL,
    so refuse it as a 401 rather than serving a cache that can never succeed."""
    import httpx

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": []}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    g = EntraGatewayGuard(tenant_id=TENANT, audience=AUDIENCE)
    with pytest.raises(AuthorizationError):
        await g.validate(keypair.mint())
    assert g._jwks_cache is None
