"""Authorization on core only: read allowed, write denied, end to end.

The authorization check lives INSIDE ``ThingClient``. You pass a ``pdp`` and an
``identity``, and every device-reaching call authorizes the resolved
``(thing_id, affordance, op)`` before it selects a binding. Here the same
identity is permitted to READ a property and refused to WRITE it, decided
against the TD-derived vocabulary. Everything runs on the CORE install, no
extra: ``LocalPolicyGrantSource`` is the dependency-free reference grant source,
and the device is an in-process object behind a ``LocalBinding``.

Where does the identity come from? A claims dict, exactly like a token guard's
``validate()`` returns. In a real deployment an upstream gateway (or the
``thingctx-identity`` guard) validates the caller's bearer token and hands you
these claims; core takes it as given. Core does NOT validate tokens, and that is
the point of the split: authn (prove who the caller is) lives in the guard
package, authz (decide what that caller may do) lives here in core with no
crypto and no network. This demo writes the claims inline so it needs neither.
For the full chain (a real RS256 token validated into these claims), see
``thingctx-identity/examples/authn_to_authz.py``.

The pieces, smallest first:

* a Thing with one read+write property (``target_rpm``), reachable in-process;
* ``build_vocabulary(thing)`` -> the closed set of grantable tuples the TD
  declares (here both ``readproperty`` and ``writeproperty`` on the property);
* a ``LocalPolicyGrantSource`` mapping the ``operator`` role to READ only;
* a ``PolicyDecisionPoint`` over that vocabulary and grant source;
* ``ThingClient(tds=[...], pdp=pdp, identity=claims)`` -> a native client whose
  own dispatch methods enforce the decision.

Run::  python examples/14_authz.py
"""

from __future__ import annotations

import asyncio

from thingctx import LocalBinding, ThingClient
from thingctx.authz import (
    AuthorizationDenied,
    LocalPolicyGrantSource,
    PolicyDecisionPoint,
    build_vocabulary,
)

THING_ID = "urn:demo:pump"

# A minimal TD: one property, both readable and writable, over a local form.
# The op list declares BOTH ops, so the vocabulary contains readproperty AND
# writeproperty for target_rpm; the split below is a POLICY choice, not a
# capability limit.
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
            "forms": [
                {
                    "href": "local://target_rpm",
                    "op": ["readproperty", "writeproperty"],
                }
            ],
        }
    },
}


class Pump:
    """The in-process device the LocalBinding drives. ``get_``/``set_`` map to
    the property's read/write."""

    def __init__(self) -> None:
        self._target_rpm = 1200

    def get_target_rpm(self) -> int:
        return self._target_rpm

    def set_target_rpm(self, value: int) -> dict:
        self._target_rpm = value
        return {"ok": True, "target_rpm": value}


async def main() -> None:
    # 1. Parse the TD once (unguarded) so we can read the vocabulary off it.
    #    This client is only used to derive the grantable tuples below; the
    #    guarded client is built in step 6.
    reader = ThingClient(tds=[TD], bindings=[LocalBinding(Pump())])

    # 2. The closed vocabulary the TD declares: read AND write are grantable.
    vocabulary = build_vocabulary(reader.things)
    print("vocabulary (grantable tuples the TD declares):")
    for tid, aff, op in sorted(vocabulary):
        print(f"  ({tid}, {aff}, {op})")

    # 3. Policy: the 'operator' role may READ target_rpm, and nothing else.
    #    Write is simply absent from the grant, so it will be denied.
    grant_source = LocalPolicyGrantSource({"operator": {(THING_ID, "target_rpm", "readproperty")}})

    # 4. The PDP decides against the vocabulary + grant source.
    pdp = PolicyDecisionPoint(vocabulary=vocabulary, grant_source=grant_source)

    # 5. The identity: a claims dict, exactly what a token guard's validate()
    #    returns. Assume an upstream gateway (or the thingctx-identity guard)
    #    already validated the caller's bearer token and handed us these claims;
    #    core takes the identity as given and never checks a signature. Written
    #    inline here so the demo runs on core alone (no guard, no IdP, no token).
    identity = {"sub": "alice", "roles": ["operator"]}

    # 6. The NATIVE guarded client: authorization is a constructor concern, not a
    #    wrapper. Every device-reaching call on THIS client authorizes against
    #    the pdp for this identity before it touches a binding.
    client = ThingClient(
        tds=[TD],
        bindings=[LocalBinding(Pump())],
        pdp=pdp,
        identity=identity,
    )

    # READ: granted -> the real device value comes back.
    value = await client.read_property("pump.target_rpm")
    print(f"\nREAD  target_rpm  -> ALLOWED, device returned {value}")

    # WRITE: not granted -> the client raises BEFORE the device is touched.
    try:
        await client.write_property("pump.target_rpm", 3000)
        print("WRITE target_rpm  -> ALLOWED (unexpected)")
    except AuthorizationDenied as denied:
        print(f"WRITE target_rpm  -> DENIED, {denied.reason}")

    # Proof the write never reached the device: the value is unchanged.
    after = await client.read_property("pump.target_rpm")
    assert after == value, "denied write must not change device state"
    print(f"\nRE-READ target_rpm -> {after}  (unchanged: the denied write never ran)")


if __name__ == "__main__":
    asyncio.run(main())
