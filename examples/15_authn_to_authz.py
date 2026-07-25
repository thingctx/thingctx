# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The full chain: a REAL token -> validated claims -> authz enforced.

This answers one question: where does the identity that drives authorization
actually come from? The core authz demo
(``examples/14_authz.py``) writes the claims dict inline and says "assume
an upstream guard validated the token and handed us these". This demo IS that
guard. It closes the loop end to end:

    guard.validate(real_token)  ->  claims dict  ->  ThingClient(pdp, identity)

The two seams, and the line between them:

* AUTHN (this package): :class:`~thingctx.identity.JwtGatewayGuard` validates an
  inbound bearer JWT for real: RS256 signature against the issuer's JWKS key for
  the token's ``kid``, plus issuer / audience / expiry / grant checks. It needs
  crypto (``pyjwt[crypto]``), which is why it lives here, not in core. Its output
  is a plain claims dict.
* AUTHZ (core): :mod:`thingctx.authz` takes that claims dict as the identity and
  decides ``permit`` / ``deny`` per ``(thing, affordance, op)`` against the
  TD-derived vocabulary. No crypto, no network. That is the whole reason core can
  stay dependency-free while still enforcing: it never validates a token, it
  consumes an already-validated identity.

Everything runs offline. We generate a self-signed RSA keypair, publish its
public half as a JWKS, mint a token signed with the private half, and point the
guard at that JWKS. The signature verification is the same code path a real
Entra / Cloudflare token takes; only the key is ours instead of the IdP's. This
mirrors the guard's own test setup (``tests/conftest.py``).

Run (needs the ``authz`` extra: ``pip install -e '.[authz]'``)::

    python examples/15_authn_to_authz.py
"""

from __future__ import annotations

import asyncio
import json
import time

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# AUTHZ seam: the enforcement is CORE thingctx. Same imports as the core demo.
from thingctx import LocalBinding, ThingClient
from thingctx.authz import (
    AuthorizationDenied,
    LocalPolicyGrantSource,
    PolicyDecisionPoint,
    build_vocabulary,
)

# AUTHN seam: the guard lives in THIS package (it needs pyjwt[crypto]).
from thingctx.identity import Grant, JwtGatewayGuard

# --------------------------------------------------------------------------- #
# A self-signed issuer, entirely offline. In production the issuer is a real
# IdP (Entra, Cloudflare Access, ...) and you fetch its JWKS over https; here we
# ARE the issuer so the whole chain runs with no network and no tenant.
# --------------------------------------------------------------------------- #

ISSUER = "https://issuer.example/demo/v2.0"
AUDIENCE = "api://thingctx-gateway"
KID = "demo-key-1"


class Issuer:
    """A self-signed RSA key that can publish a JWKS and mint RS256 tokens.

    Same shape as the guard's test conftest: the guard verifies against the
    PUBLIC half (the JWKS); only the private half signs. Swapping the key would
    flip every validate() from pass to fail, which is the point of a real
    signature check."""

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._private_pem = self._key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def jwks(self) -> dict:
        """The public JWKS the guard verifies against, as an IdP would publish."""
        pub_pem = self._key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        algo = jwt.algorithms.RSAAlgorithm(hashes.SHA256())
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(algo.prepare_key(pub_pem)))
        jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def mint(self, *, roles: list[str], exp_delta: int = 3600) -> str:
        """Sign a real RS256 token carrying ``roles`` in the claims."""
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "nbf": now - 10,
            "exp": now + exp_delta,
            "sub": "alice",
            "roles": roles,
        }
        return jwt.encode(claims, self._private_pem, algorithm="RS256", headers={"kid": KID})


THING_ID = "urn:demo:pump"

TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": THING_ID,
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "properties": {
        "target_rpm": {
            "type": "integer",
            "description": "The pump's target speed. Readable and writable.",
            "forms": [{"href": "local://target_rpm", "op": ["readproperty", "writeproperty"]}],
        }
    },
}


class Pump:
    def __init__(self) -> None:
        self._target_rpm = 1200

    def get_target_rpm(self) -> int:
        return self._target_rpm

    def set_target_rpm(self, value: int) -> dict:
        self._target_rpm = value
        return {"ok": True, "target_rpm": value}


async def main() -> None:
    issuer = Issuer()

    # ------------------------------------------------------------------ #
    # AUTHN: validate a REAL token into claims. This is thingctx.identity.
    # ------------------------------------------------------------------ #
    # The guard is pointed at the issuer's PUBLIC JWKS (offline: no network,
    # like the guard's tests). It requires the 'operator' role via a Grant, so
    # a token without it never even reaches authorization.
    guard = JwtGatewayGuard(
        issuers=[ISSUER],
        audience=AUDIENCE,
        jwks=issuer.jwks(),
        grants=[Grant(claim="roles", values=("operator",), kind="role")],
    )

    token = issuer.mint(roles=["operator"])
    print(f"minted a real RS256 token ({len(token)} chars); validating it...")

    # Real verification: signature (RS256 vs the JWKS key for this kid), issuer,
    # audience, expiry, and the operator-role grant. Any failure raises.
    claims = await guard.validate(token)
    print("token VALIDATED. guard returned claims:")
    print(f"  sub={claims['sub']!r}  roles={claims['roles']}  aud={claims['aud']!r}")
    print(f"  exp={claims['exp']} (a real wall-clock deadline the authz layer re-checks)")

    # Proof the signature check is live: a token from a different key is refused.
    from thingctx.identity import AuthorizationError

    impostor = Issuer()  # different private key, same kid/iss/aud
    forged = impostor.mint(roles=["operator"])
    try:
        await guard.validate(forged)
        print("  [!] forged token accepted (should not happen)")
    except AuthorizationError as err:
        print(f"  forged token (wrong signing key) REJECTED: {err}")

    # ------------------------------------------------------------------ #
    # AUTHZ: drive CORE enforcement with the VALIDATED identity.
    # ------------------------------------------------------------------ #
    # From here down it is exactly the core demo, except `identity` is no longer
    # a hand-written dict: it is `claims`, the guard's real output. The 'operator'
    # role (proven by the token) may READ target_rpm but not WRITE it.
    reader = ThingClient(tds=[TD], bindings=[LocalBinding(Pump())])
    vocabulary = build_vocabulary(reader.things)
    grant_source = LocalPolicyGrantSource({"operator": {(THING_ID, "target_rpm", "readproperty")}})
    pdp = PolicyDecisionPoint(vocabulary=vocabulary, grant_source=grant_source)

    # The validated claims ARE the identity. No inline dict; this is the seam.
    client = ThingClient(
        tds=[TD],
        bindings=[LocalBinding(Pump())],
        pdp=pdp,
        identity=claims,
    )

    value = await client.read_property("pump__target_rpm")
    print(f"\nREAD  target_rpm  -> ALLOWED, device returned {value}")

    try:
        await client.write_property("pump__target_rpm", 3000)
        print("WRITE target_rpm  -> ALLOWED (unexpected)")
    except AuthorizationDenied as denied:
        print(f"WRITE target_rpm  -> DENIED, {denied.reason}")

    after = await client.read_property("pump__target_rpm")
    assert after == value, "denied write must not change device state"
    print(f"\nRE-READ target_rpm -> {after}  (unchanged: the denied write never ran)")
    print("\nchain complete: real token -> validated claims -> authz enforced on that identity")


if __name__ == "__main__":
    asyncio.run(main())
