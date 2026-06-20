"""AWS Signature Version 4: a pure signing primitive plus credential helpers.

``sigv4_sign`` does no I/O and is exposed as ``thingctx.sigv4_sign``. Signing is
HTTP-specific (it canonicalizes method/path/query/body), so the applier that uses
it lives with the HTTP transport; this module only provides the computation.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

__all__ = ["sigv4_sign"]

# Scheme words we accept as "this is AWS SigV4" (besides the conformant
# ``scheme: auto`` + ``x-thingctx-auth: aws-sigv4`` form).
_AWS_SCHEMES = {"aws-sigv4", "aws_sigv4", "awssigv4", "sigv4"}


def _aws_creds(cred: Any) -> tuple[str | None, str | None, str | None]:
    """Normalize an AWS credential into (access_key, secret_key, session_token)."""
    if isinstance(cred, dict):
        ak = cred.get("aws_access_key_id") or cred.get("access_key") or cred.get("access_key_id")
        sk = (
            cred.get("aws_secret_access_key")
            or cred.get("secret_key")
            or cred.get("secret_access_key")
        )
        st = cred.get("aws_session_token") or cred.get("session_token") or cred.get("token")
        return ak, sk, st
    if isinstance(cred, (tuple, list)) and len(cred) >= 2:
        st = cred[2] if len(cred) > 2 else None
        return cred[0], cred[1], st
    return None, None, None


def _region_service(region: str | None, service: str | None, host: str) -> tuple[str, str]:
    """Fill in (region, service) from the AWS host when not given explicitly,
    e.g. ``s3.us-east-1.amazonaws.com`` or ``sts.amazonaws.com`` (global ->
    us-east-1)."""
    labels = host.split(".")
    if region is None or service is None:
        if len(labels) >= 4 and labels[-2] == "amazonaws" and labels[-1] == "com":
            service = service or labels[0]
            region = region or labels[-3]
        elif len(labels) >= 3 and labels[-2] == "amazonaws":
            service = service or labels[0]
            region = region or "us-east-1"
    return region or "us-east-1", service or (labels[0] if labels else "")


def _aws_region_service(scheme: Any, cred: Any, host: str) -> tuple[str, str]:
    """Resolve (region, service): explicit on the credential or scheme wins,
    otherwise derive from the AWS host."""
    raw = getattr(scheme, "raw", {}) or {}
    cred = cred if isinstance(cred, dict) else {}
    return _region_service(cred.get("region") or raw.get("region"),
                           cred.get("service") or raw.get("service"), host)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_sign(
    *,
    method: str,
    url: str,
    headers: dict,
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    session_token: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Compute AWS Signature Version 4 headers for one request.

    Pure function (no I/O). Returns the headers to add/overwrite:
    ``Authorization``, ``X-Amz-Date``, ``x-amz-content-sha256`` (and
    ``X-Amz-Security-Token`` when a session token is present).
    """
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    parsed = urlparse(url)
    host = parsed.netloc
    canonical_uri = quote(parsed.path or "/", safe="/-_.~")

    # Canonical query string: sorted by key, each key and value RFC3986-encoded.
    pairs = []
    if parsed.query:
        for part in parsed.query.split("&"):
            if not part:
                continue
            k, _, v = part.partition("=")
            pairs.append((quote(k, safe="-_.~"), quote(v, safe="-_.~")))
    canonical_qs = "&".join(f"{k}={v}" for k, v in sorted(pairs))

    payload_hash = _sha256_hex(body or b"")
    signed = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if session_token:
        signed["x-amz-security-token"] = session_token
    signed_headers = ";".join(sorted(signed))
    canonical_headers = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))

    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_qs,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, _sha256_hex(canonical_request.encode("utf-8"))]
    )

    k_date = _hmac(f"AWS4{secret_key}".encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    out = {
        "Authorization": authorization,
        "X-Amz-Date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if session_token:
        out["X-Amz-Security-Token"] = session_token
    return out
