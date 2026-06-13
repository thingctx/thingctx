"""The bundled TD 1.1 schema must stay byte-identical to the official W3C
validation schema. This is the drift tripwire: if W3C republishes, this
fails so the bundled copy gets refreshed instead of silently going stale.
"""

from __future__ import annotations

import hashlib

import pytest

from thingctx.validate import _SCHEMA_PATH

# Official W3C WoT TD 1.1 validation schema (302s to the versioned raw file).
W3C_TD_SCHEMA_URL = "https://www.w3.org/2022/wot/td-schema/v1.1"


@pytest.mark.network
def test_bundled_schema_matches_w3c_upstream():
    httpx = pytest.importorskip("httpx")
    try:
        resp = httpx.get(W3C_TD_SCHEMA_URL, follow_redirects=True, timeout=10)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        pytest.skip(f"cannot reach W3C schema source: {e}")

    upstream = resp.content
    bundled = _SCHEMA_PATH.read_bytes()
    if hashlib.sha256(bundled).digest() != hashlib.sha256(upstream).digest():
        pytest.fail(
            "bundled TD 1.1 schema has drifted from the official W3C schema.\n"
            f"  refresh: curl -sL {W3C_TD_SCHEMA_URL} -o {_SCHEMA_PATH}\n"
            "  then review the diff and bump the date in validate.py."
        )
