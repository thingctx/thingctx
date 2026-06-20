"""The auth layer is extensible: a user can register a provider to teach
thingctx a brand-new scheme, or override how a built-in scheme is handled.

A provider's ``resolve`` returns neutral credential material: a header (here an
ApiKeyCredential) for token-attachers, or a RequestSigner for schemes that must
mutate the assembled request. These run offline -- they assert the headers a
provider contributes and that a custom request-signer runs on the request.
"""

from __future__ import annotations

import httpx

from thingctx.auth import (
    ApiKeyCredential,
    AuthRegistry,
    AwsSigV4Auth,
    RequestSigner,
    StaticBearerAuth,
    _BaseAuth,
)
from thingctx.invokers import HttpInvoker
from thingctx.runtime import ThingClient


def _td(slug: str, scheme: dict) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": f"urn:thingctx:{slug}",
        "title": slug,
        "securityDefinitions": {"sc": scheme},
        "security": ["sc"],
        "actions": {
            "ping": {"idempotent": True, "forms": [{"href": f"https://api.example/{slug}"}]}
        },
    }


class _DemoHeaderAuth(_BaseAuth):
    """A made-up scheme that attaches a simple custom header. A token-attacher,
    so it resolves to header material (an ApiKeyCredential)."""

    name = "demo-header"

    def matches(self, scheme, credential):
        return getattr(scheme, "scheme", None) == "x-demo"

    async def resolve(self, ctx):
        return ApiKeyCredential(name="X-Demo-Key", value=f"demo {ctx.credential}")


async def test_custom_scheme_via_extra_auth():
    http = HttpInvoker(
        credentials={"thingy": "SEKRET"}, extra_auth=[_DemoHeaderAuth()]
    )
    client = ThingClient(tds=[_td("thingy", {"scheme": "x-demo"})], invokers=[http])
    action = client.action_for("thingy.ping")
    headers, params, signers, _cert = await http._prepare(action.thing_id)
    assert headers["X-Demo-Key"] == "demo SEKRET"
    assert not signers


class _OverrideBearerAuth(_BaseAuth):
    """Overrides the built-in bearer handling to use a different header."""

    name = "override-bearer"

    def matches(self, scheme, credential):
        return getattr(scheme, "scheme", None) == "bearer"

    async def resolve(self, ctx):
        return ApiKeyCredential(name="X-Auth-Token", value=str(ctx.credential))


async def test_user_strategy_overrides_builtin():
    http = HttpInvoker(credentials={"thingy": "TKN"}, extra_auth=[_OverrideBearerAuth()])
    client = ThingClient(tds=[_td("thingy", {"scheme": "bearer"})], invokers=[http])
    action = client.action_for("thingy.ping")
    headers, _params, _signers, _cert = await http._prepare(action.thing_id)
    # The override wins: custom header set, default Authorization not.
    assert headers["X-Auth-Token"] == "TKN"
    assert "Authorization" not in headers


class _StampSigner(_BaseAuth):
    """A custom request-signer: resolves to a RequestSigner that stamps a header
    on the assembled request."""

    name = "stamp"

    def matches(self, scheme, credential):
        return getattr(scheme, "scheme", None) == "x-stamp"

    async def resolve(self, ctx):
        cred = ctx.credential

        def _sign(request):
            request.headers["X-Stamped"] = f"{request.method}:{cred}"

        return RequestSigner(sign=_sign)


async def test_custom_request_signer_is_invoked():
    http = HttpInvoker(credentials={"thingy": "S"}, extra_auth=[_StampSigner()])
    client = ThingClient(tds=[_td("thingy", {"scheme": "x-stamp"})], invokers=[http])
    action = client.action_for("thingy.ping")
    headers, params, signers, _cert = await http._prepare(action.thing_id)
    assert len(signers) == 1  # scheduled as a signer, not a header-attacher
    with httpx.Client() as c:
        req = c.build_request("GET", "https://api.example/thingy", headers=headers, params=params)
    await http._sign_request(signers, req)
    assert req.headers["X-Stamped"] == "GET:S"


def test_registry_resolution_and_precedence():
    reg = AuthRegistry([StaticBearerAuth(), AwsSigV4Auth()])

    class _S:
        scheme = "bearer"
        raw: dict = {}

    assert isinstance(reg.resolve(_S(), "x"), StaticBearerAuth)
    # A front-registered override is resolved first.
    reg.register(_OverrideBearerAuth(), first=True)
    assert isinstance(reg.resolve(_S(), "x"), _OverrideBearerAuth)
    # clone() is independent of the original.
    assert isinstance(reg.clone().resolve(_S(), "x"), _OverrideBearerAuth)
