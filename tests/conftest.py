# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Shared test fixtures: a self-signed RSA keypair, a fake JWKS the guard can be
pointed at, and a token minter carrying realistic Entra claims. These let the
guard's real signature verification run entirely offline (no Azure tenant)."""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

TENANT = "11111111-2222-3333-4444-555555555555"
AUDIENCE = "api://thingctx-gateway"
ISSUER_V2 = f"https://login.microsoftonline.com/{TENANT}/v2.0"


def _private_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _jwk_from_public(key: rsa.RSAPrivateKey, kid: str) -> dict:
    """A public JWK (RSA) as Entra would publish it in its JWKS."""
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    # PyJWT round-trips a PEM public key into a JWK dict for us.
    algo = jwt.algorithms.RSAAlgorithm(hashes.SHA256())
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(algo.prepare_key(pub_pem)))
    d["kid"] = kid
    d["use"] = "sig"
    d["alg"] = "RS256"
    return d


class KeyFixture:
    """One signing key + its JWKS entry + a token minter."""

    def __init__(self, kid: str = "test-key-1") -> None:
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = _private_pem(self._key)
        self.jwk = _jwk_from_public(self._key, kid)

    def jwks(self) -> dict:
        return {"keys": [self.jwk]}

    def mint(
        self,
        *,
        aud: str = AUDIENCE,
        iss: str = ISSUER_V2,
        tid: str = TENANT,
        scp: str | None = "Things.Invoke",
        roles: list[str] | None = None,
        exp_delta: int = 3600,
        nbf_delta: int = -10,
        extra: dict | None = None,
        kid: str | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict = {
            "iss": iss,
            "aud": aud,
            "tid": tid,
            "iat": now,
            "nbf": now + nbf_delta,
            "exp": now + exp_delta,
            "sub": "caller-object-id",
            "appid": "caller-app-id",
            "ver": "2.0",
        }
        if scp is not None:
            claims["scp"] = scp
        if roles is not None:
            claims["roles"] = roles
        if extra:
            claims.update(extra)
        return jwt.encode(
            claims,
            self.private_pem,
            algorithm="RS256",
            headers={"kid": kid or self.kid},
        )


@pytest.fixture
def keypair() -> KeyFixture:
    return KeyFixture()


@pytest.fixture
def other_keypair() -> KeyFixture:
    """A second, unrelated key: tokens it signs must be rejected as forged."""
    return KeyFixture(kid="attacker-key")
