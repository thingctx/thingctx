# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""13, extending thingctx: one programming model for every part you can extend.

thingctx has four parts you can extend, and they all work the same way, so what
you learn for one applies to the rest. For each:

    * a ``typing.Protocol`` is the contract (match the methods, inherit nothing);
    * ``@implements(Contract)`` checks that match at import time;
    * an optional public base saves boilerplate;
    * a conformance kit checks the runtime behaviour a type checker cannot.

    Part        Contract              Optional base   Conformance kit
    transport   ProtocolBinding          AuthMixin (*)   assert_binding_contract
    auth        CredentialProvider    BaseAuth        assert_provider_contract
    discovery   Registry              -               assert_registry_contract
    media       MediaBackend          -               assert_media_backend_contract
    (*) only when the transport authenticates.

``ProtocolBinding`` names the device-facing side of a transport.

The built-in http / mqtt / media / local bindings implement these same contracts;
an out-of-tree package uses these exact APIs plus the entry-point discovery shown
at the end.

It drives one composite Thing: a pump-camera whose single TD spans the control
plane (an action over a custom transport) and the media plane (a live camera),
from one client, with a different security scheme per plane (WoT form-level
security). Fully offline.

    PYTHONPATH=src python examples/13_custom_stack.py
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

from thingctx import (
    AuthMixin,
    BaseAuth,
    BearerToken,
    CredentialProvider,
    Frame,
    MediaBackend,
    MediaBinding,
    ProtocolBinding,
    ThingClient,
    discover_auth,
    discover_bindings,
    implements,
)
from thingctx.registry import Registry
from thingctx.testing import (
    assert_binding_contract,
    assert_media_backend_contract,
    assert_provider_contract,
    assert_registry_contract,
    binding_capabilities,
)


# --------------------------------------------------------------------------- #
# Discovery: where Thing Descriptions come from.
# --------------------------------------------------------------------------- #
@implements(Registry)
class FleetRegistry:
    """A discovery source: one ``fetch`` method. A real one queries mDNS, a Thing
    Description Directory, or an asset database; this holds an in-memory fleet."""

    def __init__(self, things: list[dict]) -> None:
        self._things = list(things)

    def fetch(self) -> list[dict]:
        return list(self._things)


# --------------------------------------------------------------------------- #
# Auth: a brand-new security scheme, transport-neutral.
# --------------------------------------------------------------------------- #
@implements(CredentialProvider)
class FleetTokenAuth(BaseAuth):
    """Exchange a per-device key for a bearer token (a real one calls a token
    endpoint). Returns neutral ``BearerToken``, so it works behind any transport.
    The TD declares ``scheme: auto`` plus a namespaced hint to stay W3C-valid; we
    match on the hint."""

    name = "fleet-token"

    def matches(self, scheme, credential) -> bool:
        return (getattr(scheme, "raw", {}) or {}).get("x-thingctx-auth") == "fleet-token"

    async def resolve(self, ctx):
        key = ctx.credential
        return BearerToken(token=f"{ctx.owner_id}:{key}") if key else None  # owner-scoped


# --------------------------------------------------------------------------- #
# Transport: a brand-new scheme that authenticates like a built-in.
# --------------------------------------------------------------------------- #
@implements(ProtocolBinding)
class FleetSimBinding(AuthMixin):
    """A new ``sim`` transport. It inherits ``AuthMixin`` (what the built-ins use),
    resolves whatever security each affordance declares, and would map it onto its
    wire; here it just reports the resolved material. Built-in schemes need nothing;
    pass ``extra_auth`` for a custom one."""

    scheme = "sim"

    def __init__(self, handlers: dict, *, credentials=None, extra_auth=None) -> None:
        self._handlers = dict(handlers)
        self._init_auth(credentials=credentials, auth=None, extra_auth=extra_auth, timeout=30.0)

    async def invoke(self, action, form, arguments):
        # form-level security (if any) overrides the Thing's
        creds = await self._resolve_credentials(getattr(action, "thing_id", None), form)
        name = form.href.rsplit("/", 1)[-1]  # sim://pump-a/set_speed -> set_speed
        result = self._handlers[name](**(arguments or {}))
        return {"auth": [type(c).__name__ for c in creds], **result}


# --------------------------------------------------------------------------- #
# Media: the engine behind the media binding (a second contract, same pattern).
# --------------------------------------------------------------------------- #
@implements(MediaBackend)
class SimCameraBackend:
    """An offline media engine. ``read`` yields a fixed run of frames; ``write``
    collects them. Both are synchronous and honour ``stop`` (the binding runs
    them in a worker thread). A real engine wraps FFmpeg/GStreamer or a device."""

    def __init__(self, n: int = 4) -> None:
        self._n = n
        self.written: list[Frame] = []
        self.saw_auth: object | None = None  # the auth plan the binding resolved

    def can_open(self, url: str, hint: dict) -> bool:
        return True  # this demo's binding carries only this backend

    def read(self, url: str, *, options: dict, stop: threading.Event) -> Iterator[Frame]:
        self.saw_auth = options.get("auth")  # proves the media plane authenticated
        for i in range(self._n):
            if stop.is_set():
                return
            yield Frame(
                data=bytes(4), kind="video", pts=i / 30, width=2, height=2, encoding="gray8"
            )

    def write(
        self, frames: Iterator[Frame], target: str, *, options: dict, stop: threading.Event
    ) -> None:
        for frame in frames:
            if stop.is_set():
                return
            self.written.append(frame)


# --------------------------------------------------------------------------- #
# One composite Thing whose single TD spans both planes: a control action over the
# sim transport and a live camera over the media transport. Control uses the unit's
# default built-in scheme; the camera overrides it with the custom fleet-token
# scheme (WoT form-level security). Secrets are named in the TD, supplied at runtime.
# --------------------------------------------------------------------------- #
def _pump_cam_td(slug: str) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": f"urn:thingctx:{slug}:v1",
        "title": slug,
        "securityDefinitions": {
            "basic_sc": {"scheme": "basic"},  # built-in scheme
            "fleet_sc": {"scheme": "auto", "x-thingctx-auth": "fleet-token"},  # custom scheme
        },
        "security": ["basic_sc"],  # the unit's default (control plane)
        "actions": {
            # control plane: inherits the Thing's scheme
            "set_speed": {
                "input": {"type": "object", "properties": {"rpm": {"type": "number"}}},
                "forms": [{"href": f"sim://{slug}/set_speed"}],
            },
            # media plane: overrides with its own scheme
            "watch": {
                "description": "Live camera on the unit.",
                "forms": [{"href": f"rtsp://{slug}.cam.local/stream", "security": ["fleet_sc"]}],
            },
        },
    }


async def main() -> None:
    def set_speed(rpm: float = 0.0) -> dict:
        return {"ok": True, "rpm": rpm}

    camera = SimCameraBackend()

    # Control uses a built-in scheme, so the sim binding needs no provider; media
    # uses the custom scheme, so the media binding carries it. Each binding holds
    # only its plane's secret, keyed by scheme name.
    sim = FleetSimBinding(
        {"set_speed": set_speed}, credentials={"basic_sc": ("operator", "s3cret")}
    )
    media = MediaBinding(
        backends=[camera],
        extra_auth=[FleetTokenAuth()],
        credentials={"fleet_sc": "fleet-key"},
    )
    tds = [_pump_cam_td("pump-a"), _pump_cam_td("pump-b")]
    registry = FleetRegistry(tds)

    # 0) The composite TD is valid W3C WoT TD 1.1, form-level security and all.
    #    validate_td returns the problems ([] when valid). Needs the [validate]
    #    extra; without it the example still runs and skips the check.
    try:
        from thingctx.validate import validate_td

        problems = validate_td(tds[0])
        print("composite TD valid WoT TD 1.1:", "yes" if not problems else problems)
    except ImportError:
        print("composite TD validation skipped (install thingctx[validate]).")

    # 1) Prove every part against its conformance kit before wiring; the same check
    #    works for a built-in, a custom, or an out-of-tree extension.
    assert_binding_contract(sim)
    assert_provider_contract(FleetTokenAuth())
    assert_registry_contract(registry)
    assert_media_backend_contract(camera)
    print("all four contracts pass.")
    print("sim binding capabilities:", binding_capabilities(sim))

    # 2) One client over the fleet. Each affordance routes to the binding that serves
    #    its form (set_speed -> sim, watch -> media).
    client = ThingClient.from_registry(registry, bindings=[sim, media])
    print("\ndiscovered actions:", [t["function"]["name"] for t in client.list_actions()])
    print("media affordances:", client.list_media())

    # 3) Control plane: the built-in scheme resolves to a BasicCredential.
    out = await client.invoke("pump-a__set_speed", {"rpm": 1200})
    print(f"\npump-a__set_speed -> {out}   (control plane: basic)")

    # 4) Media plane, same Thing: the watch form overrode security with the custom
    #    scheme. The media binding resolved it and the backend saw the auth plan.
    frames = [f async for f in await client.frames("pump-a__watch", track="video")]
    print(
        f"pump-a__watch     -> {len(frames)} frames; backend saw auth: "
        f"{camera.saw_auth is not None}   (media plane: fleet-token)"
    )

    # 5) Out-of-tree, the same objects come from entry points instead of being built
    #    by hand. The calls are identical; empty here since nothing is installed.
    print("\ninstalled transports:", [type(b).__name__ for b in discover_bindings()])
    print("installed providers :", [p.name for p in discover_auth()])

    print("\nOK: one composite Thing, a different security scheme per plane, one model.")


if __name__ == "__main__":
    asyncio.run(main())
