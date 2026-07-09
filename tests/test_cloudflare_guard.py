# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Proofs for CloudflareAccessGuard, mirroring the Entra guard tests.

Same rigor, Cloudflare-shaped tokens, entirely offline: a self-signed RSA key
plus a matching JWKS the guard is pointed at (no live Cloudflare, no network).

The security assertions, one to one with the Entra suite:
  (a) a valid Cloudflare-Access-shaped token passes validate() and returns claims,
  (b) a wrong-audience (wrong AUD tag) token is rejected,
  (c) an expired token is rejected,
  (d) a token signed by the WRONG key is rejected (signature check works),
  (e) the authorization grant is enforced (a caller lacking it is rejected).

Plus: an 'alg: none' downgrade is refused, a token from the wrong team (issuer)
is rejected, and signature verification is provably on (swap the key alone,
pass->fail).

Cloudflare token facts exercised here (RS256 JWTs):
  * iss  = https://<team>.cloudflareaccess.com
  * aud  = ARRAY containing the Access app's AUD tag (a hex string)
  * user token  -> email / sub
  * service token (agent) -> common_name, sub empty
The guard's audience check requires the configured AUD tag to be a MEMBER of the
aud array; that is the Cloudflare shape, and the base handles it because pyjwt's
audience check accepts an array claim.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from thingctx.identity import AuthorizationError, CloudflareAccessGuard

TEAM = "testteam"
ISSUER = f"https://{TEAM}.cloudflareaccess.com"
# A Cloudflare AUD tag is a 64-char hex string tied to the Access application.
AUD_TAG = "4714c1358e65fe4b408ad6d432a5f878f08194bdb4752441fd56faefa9b2b6f2"
OTHER_AUD_TAG = "deadbeef" * 8
SERVICE_TOKEN_CN = "my-agent.access"


# --------------------------------------------------------------------------- #
# Offline key + Cloudflare-shaped token minter (a self-signed RSA key and its
# JWKS, exactly like conftest does for Entra, but minting Cloudflare claims and
# publishing a TWO-key JWKS the way Cloudflare's /cdn-cgi/access/certs does).
# --------------------------------------------------------------------------- #


def _private_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _jwk_from_public(key: rsa.RSAPrivateKey, kid: str) -> dict:
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    algo = jwt.algorithms.RSAAlgorithm(hashes.SHA256())
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(algo.prepare_key(pub_pem)))
    d["kid"] = kid
    d["use"] = "sig"
    d["alg"] = "RS256"
    return d


class CfKey:
    """One signing key + its JWKS entry + a Cloudflare-Access token minter."""

    def __init__(self, kid: str = "cf-key-1") -> None:
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = _private_pem(self._key)
        self.jwk = _jwk_from_public(self._key, kid)

    def jwks(self, *, extra_jwks: list[dict] | None = None) -> dict:
        # Cloudflare publishes TWO keys (current + rotated); include a decoy so
        # the guard must select by kid, not fall back to a lone key.
        keys = [self.jwk] + list(extra_jwks or [])
        return {"keys": keys}

    def mint(
        self,
        *,
        aud=None,
        iss: str = ISSUER,
        email: str | None = "person@example.com",
        common_name: str | None = None,
        sub: str = "cf-user-id",
        exp_delta: int = 3600,
        nbf_delta: int = -10,
        extra: dict | None = None,
        kid: str | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict = {
            "iss": iss,
            # Cloudflare puts the AUD tag(s) in an ARRAY.
            "aud": aud if aud is not None else [AUD_TAG],
            "iat": now,
            "nbf": now + nbf_delta,
            "exp": now + exp_delta,
            "sub": sub,
            "type": "app",
            "identity_nonce": "abc123",
        }
        if email is not None:
            claims["email"] = email
        if common_name is not None:
            # A service token (the app-only / agent case) carries common_name and
            # an empty sub, per Cloudflare.
            claims["common_name"] = common_name
            claims["sub"] = ""
        if extra:
            claims.update(extra)
        return jwt.encode(
            claims,
            self.private_pem,
            algorithm="RS256",
            headers={"kid": kid or self.kid},
        )


@pytest.fixture
def cfkey() -> CfKey:
    return CfKey()


@pytest.fixture
def other_cfkey() -> CfKey:
    """A second, unrelated key: tokens it signs must be rejected as forged."""
    return CfKey(kid="cf-attacker-key")


def guard(cfkey: CfKey, **over) -> CloudflareAccessGuard:
    """A Cloudflare guard pointed at the test JWKS (offline: no network)."""
    kw = dict(team_domain=TEAM, audience=AUD_TAG, jwks=cfkey.jwks())
    kw.update(over)
    return CloudflareAccessGuard(**kw)


# --------------------------------------------------------------------------- #
# (a) a valid Cloudflare-Access-shaped token passes and returns claims
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_valid_token_passes(cfkey):
    g = guard(cfkey)
    token = cfkey.mint()
    claims = await g.validate(token)
    assert AUD_TAG in claims["aud"]
    assert claims["iss"] == ISSUER
    assert claims["email"] == "person@example.com"


@pytest.mark.asyncio
async def test_valid_service_token_passes(cfkey):
    """A service token (the agent equivalent) carries common_name; it validates
    the same way, authentication-only, when no per-action grant is required."""
    g = guard(cfkey)
    token = cfkey.mint(email=None, common_name=SERVICE_TOKEN_CN)
    claims = await g.validate(token)
    assert claims["common_name"] == SERVICE_TOKEN_CN


# --------------------------------------------------------------------------- #
# (b) wrong audience (wrong AUD tag) is rejected
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wrong_audience_rejected(cfkey):
    g = guard(cfkey)
    token = cfkey.mint(aud=[OTHER_AUD_TAG])  # a token for a different Access app
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(token)
    assert "audience" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# (c) expired token is rejected
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_expired_token_rejected(cfkey):
    g = guard(cfkey)
    token = cfkey.mint(exp_delta=-3600)  # expired an hour ago
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(token)
    assert "expired" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# (d) a token signed by the WRONG key is rejected -- signature verification
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wrong_key_signature_rejected(cfkey, other_cfkey):
    """The attacker signs with their own key but presents a kid that matches the
    team's published key. The guard verifies with the TEAM's public key, so the
    signature check fails. This is the core security property."""
    g = guard(cfkey)  # trusts only cfkey's JWKS
    forged = other_cfkey.mint(kid=cfkey.kid)  # attacker key, claims team's kid
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(forged)
    assert "signature" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_wrong_key_unknown_kid_rejected(cfkey, other_cfkey):
    """Attacker signs with their own key AND their own kid, absent from the team
    JWKS: rejected because no trusted key matches the kid. Cloudflare's JWKS has
    two keys, so the lone-key fallback never fires."""
    g = guard(cfkey, jwks=cfkey.jwks(extra_jwks=[CfKey(kid="rotated").jwk]))
    forged = other_cfkey.mint()  # attacker-key kid, not in the JWKS
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(forged)
    assert "kid" in str(ei.value).lower() or "signature" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# (e) the authorization grant is enforced
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_service_token_permission_grant_enforced(cfkey):
    """Per-action grant via the service-token mapping: the gateway is configured
    with common_name -> permissions, and requires Thing1.Write. A service token
    whose common_name maps to that permission passes; one that does not is
    rejected. This is the honest Cloudflare shape: the grant is NOT in the token,
    the gateway derives it from the token's identity (common_name)."""
    g = guard(
        cfkey,
        required_permissions=["Thing1.Write"],
        service_token_permissions={
            SERVICE_TOKEN_CN: ["Thing1.Write", "Thing1.Read"],
            "read-only-agent.access": ["Thing1.Read"],
        },
    )

    ok = cfkey.mint(email=None, common_name=SERVICE_TOKEN_CN)
    claims = await g.validate(ok)
    assert claims["common_name"] == SERVICE_TOKEN_CN

    # A service token that maps only to Thing1.Read lacks the required grant.
    denied = cfkey.mint(email=None, common_name="read-only-agent.access")
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(denied)
    assert "permission" in str(ei.value).lower()

    # An unknown common_name maps to no permissions at all.
    unknown = cfkey.mint(email=None, common_name="ghost.access")
    with pytest.raises(AuthorizationError):
        await g.validate(unknown)


@pytest.mark.asyncio
async def test_custom_claim_permission_grant_enforced(cfkey):
    """Per-action grant via a custom claim an upstream IdP / Access policy stamps
    (the closest Cloudflare gets to Entra app roles): the guard reads the claim
    directly. A token carrying the permission passes; one without is rejected."""
    g = guard(
        cfkey,
        required_permissions=["Thing1.Write"],
        permission_claim="permissions",
    )

    ok = cfkey.mint(extra={"permissions": ["Thing1.Write", "Thing1.Read"]})
    claims = await g.validate(ok)
    assert "Thing1.Write" in claims["permissions"]

    missing = cfkey.mint(extra={"permissions": ["Thing1.Read"]})
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(missing)
    assert "permission" in str(ei.value).lower()

    # No permissions claim at all -> rejected when a permission is required.
    none = cfkey.mint()
    with pytest.raises(AuthorizationError):
        await g.validate(none)


@pytest.mark.asyncio
async def test_required_permissions_needs_a_source():
    """Requiring a permission with no source (no custom claim, no service-token
    map) is a configuration error: Cloudflare will not put app-role-style
    permissions in the token by itself, so the guard refuses to be constructed in
    a state that would silently authorize everything."""
    with pytest.raises(ValueError) as ei:
        CloudflareAccessGuard(
            team_domain=TEAM,
            audience=AUD_TAG,
            required_permissions=["Thing1.Write"],
            jwks={"keys": []},
        )
    assert "source" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# Extra hardening (mirrors the Entra suite)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wrong_team_issuer_rejected(cfkey):
    """A token minted for a different Cloudflare team (issuer) is rejected."""
    g = guard(cfkey)
    token = cfkey.mint(iss="https://otherteam.cloudflareaccess.com")
    with pytest.raises(AuthorizationError):
        await g.validate(token)


@pytest.mark.asyncio
async def test_alg_none_downgrade_refused(cfkey):
    """An 'alg: none' unsigned token must be refused before any decode."""
    unsigned = jwt.encode(
        {"aud": [AUD_TAG], "iss": ISSUER, "exp": 9999999999},
        key="",
        algorithm="none",
    )
    g = guard(cfkey)
    with pytest.raises(AuthorizationError) as ei:
        await g.validate(unsigned)
    assert "algorithm" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_signature_verification_is_actually_on(cfkey):
    """Guard against a future regression that disables verification: swapping the
    signing key alone (everything else identical) must flip pass->fail."""
    g = guard(cfkey)
    good = cfkey.mint()
    assert AUD_TAG in (await g.validate(good))["aud"]  # passes with the right key

    impostor = CfKey(kid=cfkey.kid)  # same kid, different key
    bad = impostor.mint()
    with pytest.raises(AuthorizationError):
        await g.validate(bad)  # must fail: only the signature differs


@pytest.mark.asyncio
async def test_team_domain_accepts_full_host(cfkey):
    """team_domain accepts a bare name or a full host; both fix the same issuer."""
    g = CloudflareAccessGuard(
        team_domain=f"https://{TEAM}.cloudflareaccess.com",
        audience=AUD_TAG,
        jwks=cfkey.jwks(),
    )
    claims = await g.validate(cfkey.mint())
    assert claims["iss"] == ISSUER


@pytest.mark.asyncio
async def test_jwks_fetched_from_cloudflare_certs_endpoint(cfkey, monkeypatch):
    """With no static jwks, the guard fetches the team's certs endpoint over
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
            return FakeResp(cfkey.jwks())

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    g = CloudflareAccessGuard(team_domain=TEAM, audience=AUD_TAG)  # no jwks -> fetch
    claims = await g.validate(cfkey.mint())
    assert claims["iss"] == ISSUER
    assert called["url"] == f"{ISSUER}/cdn-cgi/access/certs"
