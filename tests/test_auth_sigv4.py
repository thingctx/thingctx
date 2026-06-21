"""AWS SigV4 request signing.

Three layers:
* structural / property checks on the pure ``sigv4_sign`` (always run),
* an independent cross-check against botocore's S3 signer (skipped if botocore
  is absent -- thingctx itself never depends on it),
* end-to-end wiring through HttpInvoker (the ``aws-sigv4`` scheme produces a
  signed request).
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from thingctx.auth import AwsSigV4Auth, _aws_region_service, sigv4_sign
from thingctx.invokers import HttpInvoker
from thingctx.runtime import ThingClient

KEY = "AKIDEXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
FIXED = datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)


def _sign(**over):
    args = dict(
        method="GET",
        url="https://example.amazonaws.com/",
        headers={},
        body=b"",
        access_key=KEY,
        secret_key=SECRET,
        region="us-east-1",
        service="service",
        now=FIXED,
    )
    args.update(over)
    return sigv4_sign(**args)


def test_authorization_is_well_formed():
    out = _sign()
    auth = out["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 ")
    assert f"Credential={KEY}/20150830/us-east-1/service/aws4_request" in auth
    # host is always signed; we always sign the payload hash header and date.
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
    assert out["X-Amz-Date"] == "20150830T123600Z"
    assert len(out["x-amz-content-sha256"]) == 64


def test_signing_is_deterministic_for_fixed_inputs():
    assert _sign()["Authorization"] == _sign()["Authorization"]


def test_body_change_changes_signature():
    a = _sign(method="POST", body=b"")["Authorization"]
    b = _sign(method="POST", body=b"hello")["Authorization"]
    assert a != b


def test_region_service_and_key_each_affect_signature():
    base = _sign()["Authorization"]
    assert _sign(region="eu-west-1")["Authorization"] != base
    assert _sign(service="s3")["Authorization"] != base
    assert _sign(secret_key="wrong")["Authorization"] != base


def test_session_token_is_signed_and_emitted():
    out = _sign(session_token="SESSION==")
    assert out["X-Amz-Security-Token"] == "SESSION=="
    assert "x-amz-security-token" in out["Authorization"]  # in SignedHeaders


def test_region_service_derived_from_host():
    class _S:
        raw: dict = {}

    assert _aws_region_service(_S(), {}, "s3.us-west-2.amazonaws.com") == ("us-west-2", "s3")
    # global endpoint -> us-east-1
    assert _aws_region_service(_S(), {}, "sts.amazonaws.com") == ("us-east-1", "sts")
    # explicit credential overrides derivation
    cred = {"region": "ap-south-1", "service": "execute-api"}
    assert _aws_region_service(_S(), cred, "x.us-east-1.amazonaws.com") == (
        "ap-south-1",
        "execute-api",
    )


def test_matches_botocore_s3_signer():
    """Independent oracle: our signature must equal botocore's S3 SigV4 signer
    (which, like us, signs x-amz-content-sha256). Skipped if botocore absent.

    We let botocore choose the timestamp, then sign with that exact instant, so
    the two implementations are compared on identical inputs."""
    auth_mod = pytest.importorskip("botocore.auth")
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    url = "https://examplebucket.s3.amazonaws.com/test.txt?a=1&b=two"
    body = b"some body"
    req = AWSRequest(method="PUT", url=url, data=body)
    auth_mod.S3SigV4Auth(Credentials(KEY, SECRET), "s3", "us-east-1").add_auth(req)
    expected = req.headers["Authorization"]
    when = datetime.strptime(req.headers["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    )

    ours = sigv4_sign(
        method="PUT",
        url=url,
        headers={},
        body=body,
        access_key=KEY,
        secret_key=SECRET,
        region="us-east-1",
        service="s3",
        now=when,
    )
    assert ours["Authorization"] == expected


def _aws_td(host: str, scheme: dict) -> dict:
    return {
        "@context": [
            "https://www.w3.org/2022/wot/td/v1.1",
            {"htv": "http://www.w3.org/2011/http#"},
        ],
        "@type": "Thing",
        "id": "urn:thingctx:awsthing",
        "title": "awsthing",
        "securityDefinitions": {"sc": scheme},
        "security": ["sc"],
        "actions": {
            "list": {
                "idempotent": True,
                "forms": [{"href": f"https://{host}/", "htv:methodName": "GET"}],
            }
        },
    }


# The strictly W3C-conformant way to declare AWS SigV4: a standard "auto" scheme
# plus a namespaced hint. The bare "aws-sigv4" form also works but won't validate.
AWS_SCHEME = {"scheme": "auto", "x-thingctx-auth": "aws-sigv4", "service": "sts"}


def test_canonical_aws_td_is_w3c_valid():
    validate_td = pytest.importorskip("thingctx.validate").validate_td
    assert validate_td(_aws_td("sts.amazonaws.com", AWS_SCHEME)) == []


async def test_invoker_signs_request_end_to_end():
    """The aws-sigv4 scheme makes HttpInvoker emit a SigV4-signed request."""
    http = HttpInvoker(
        credentials={
            "awsthing": {
                "aws_access_key_id": KEY,
                "aws_secret_access_key": SECRET,
                "aws_session_token": "TOK==",
            }
        }
    )
    client = ThingClient(
        tds=[_aws_td("sts.amazonaws.com", AWS_SCHEME)],
        invokers=[http],
    )
    action = client.action_for("awsthing.list")
    headers, params, signers, _cert = await http._prepare(action.thing_id)
    assert signers, "expected an AWS SigV4 signer to be scheduled"
    with httpx.Client() as c:
        req = c.build_request("GET", "https://sts.amazonaws.com/", headers=headers, params=params)
    await http._sign_request(signers, req)
    assert req.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert "/us-east-1/sts/aws4_request" in req.headers["Authorization"]
    assert req.headers["X-Amz-Security-Token"] == "TOK=="
    assert "x-amz-date" in {k.lower() for k in req.headers}


def test_strategy_matches_extension_hint():
    strat = AwsSigV4Auth()

    class _S:
        scheme = "auto"
        raw = {"x-thingctx-auth": "aws-sigv4"}

    assert strat.matches(_S(), {"aws_access_key_id": "a", "aws_secret_access_key": "b"})
