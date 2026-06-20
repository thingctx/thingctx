"""Extensible auth: teach thingctx a brand-new security scheme, and use the
built-in AWS SigV4 signer -- all offline, no network.

thingctx resolves auth through a registry of strategies. Built-ins cover
bearer / basic / apikey / OAuth2 client-credentials / OAuth2 JWT-bearer (Google
service accounts) / AWS SigV4. Register your own to override a built-in or add a
scheme the SDK has never heard of -- without forking it.

    PYTHONPATH=src python examples/06_custom_auth.py
"""

from __future__ import annotations

import asyncio

import httpx

import thingctx
from thingctx import HttpInvoker, RequestSigner, ThingClient
from thingctx.auth import _BaseAuth


class HmacHeaderAuth(_BaseAuth):
    """A custom HMAC scheme: sign each request with a header derived from the
    method + path. It resolves to a RequestSigner -- neutral material carrying a
    callable -- so the auth layer stays transport-agnostic and the invoker just
    runs the signer.

    The conformant way to model a non-standard scheme in a TD is "scheme: auto"
    (a valid W3C value) plus a namespaced hint, so the document still passes
    strict TD validation. We match on that hint rather than a made-up scheme."""

    name = "hmac-demo"

    def matches(self, scheme, credential):
        return (getattr(scheme, "raw", {}) or {}).get("x-thingctx-auth") == "hmac-demo"

    async def resolve(self, ctx):
        secret = str(ctx.credential)

        def _sign(request):
            import hashlib
            import hmac

            msg = f"{request.method}\n{request.url.path}".encode()
            request.headers["X-Signature"] = hmac.new(
                secret.encode(), msg, hashlib.sha256
            ).hexdigest()

        return RequestSigner(sign=_sign)


def _td(slug: str, scheme: dict, host: str) -> dict:
    return {
        "@context": ["https://www.w3.org/2022/wot/td/v1.1", {"htv": "http://www.w3.org/2011/http#"}],
        "@type": "Thing",
        "id": f"urn:thingctx:{slug}",
        "title": slug,
        "securityDefinitions": {"sc": scheme},
        "security": ["sc"],
        "actions": {
            "ping": {
                "idempotent": True,
                "forms": [{"href": f"https://{host}/ping", "htv:methodName": "GET"}],
            }
        },
    }


async def main() -> None:
    # 1) A custom scheme, registered just for this invoker via extra_auth. The
    #    TD declares "auto" + a namespaced hint, so it stays W3C-valid.
    custom = HttpInvoker(credentials={"acme": "shared-secret"}, extra_auth=[HmacHeaderAuth()])
    acme_scheme = {"scheme": "auto", "x-thingctx-auth": "hmac-demo"}
    client = ThingClient(tds=[_td("acme", acme_scheme, "api.acme.test")], invokers=[custom])
    action = client.action_for("acme.ping")
    headers, params, signers, _cert = await custom._prepare(action.thing_id)
    with httpx.Client() as c:
        req = c.build_request("GET", "https://api.acme.test/ping", headers=headers, params=params)
    await custom._sign_request(signers, req)
    print("custom scheme -> X-Signature:", req.headers["X-Signature"][:24], "...")

    # 2) The built-in AWS SigV4 signer, from an 'aws-sigv4' TD scheme.
    aws = HttpInvoker(
        credentials={
            "awsthing": {"aws_access_key_id": "AKIDEXAMPLE", "aws_secret_access_key": "secret"}
        }
    )
    aws_scheme = {"scheme": "auto", "x-thingctx-auth": "aws-sigv4", "service": "sts"}
    aclient = ThingClient(
        tds=[_td("awsthing", aws_scheme, "sts.amazonaws.com")],
        invokers=[aws],
    )
    aact = aclient.action_for("awsthing.ping")
    aheaders, aparams, asigners, _acert = await aws._prepare(aact.thing_id)
    with httpx.Client() as c:
        areq = c.build_request(
            "GET", "https://sts.amazonaws.com/ping", headers=aheaders, params=aparams
        )
    await aws._sign_request(asigners, areq)
    print("aws-sigv4   -> Authorization:", areq.headers["Authorization"][:48], "...")

    # 3) The same SigV4 core is exposed as a pure function for any use.
    out = thingctx.sigv4_sign(
        method="GET",
        url="https://sts.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        headers={},
        body=b"",
        access_key="AKIDEXAMPLE",
        secret_key="secret",
        region="us-east-1",
        service="sts",
    )
    print("sigv4_sign  -> X-Amz-Date:", out["X-Amz-Date"])

    print("\nOK: registered a new scheme and used the built-in AWS SigV4 signer, no SDK fork.")


if __name__ == "__main__":
    asyncio.run(main())
