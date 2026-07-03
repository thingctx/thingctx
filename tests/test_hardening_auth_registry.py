"""Abuse-case coverage for OAuth transport security, token-store symlink
handling, and Thing-Description size caps on the file registry."""

from __future__ import annotations

import json

import pytest

from thingctx.auth.oauth_consent import authorize_code_flow, exchange_code
from thingctx.auth.store import FileTokenStore
from thingctx.registry import FileRegistry


def test_exchange_code_refuses_non_https():
    with pytest.raises(ValueError, match="non-https"):
        exchange_code(
            "http://evil.example/token",
            client_id="cid",
            client_secret="secret",
            code="abc",
            redirect_uri="http://127.0.0.1/",
            code_verifier="v",
        )


def test_exchange_code_allows_loopback_http():
    # A loopback dev endpoint is permitted (it will just fail to connect here);
    # the point is that the TLS guard does not reject it.
    with pytest.raises(Exception) as excinfo:
        exchange_code(
            "http://127.0.0.1:1/token",
            client_id="cid",
            client_secret=None,
            code="abc",
            redirect_uri="http://127.0.0.1/",
            code_verifier="v",
            timeout=0.2,
        )
    assert "non-https" not in str(excinfo.value)


def test_authorize_flow_refuses_non_https_authorization_url():
    with pytest.raises(ValueError, match="authorization endpoint"):
        authorize_code_flow(
            authorization_url="http://evil.example/auth",
            token_url="https://ok.example/token",
            client_id="cid",
            open_browser=False,
        )


def test_token_store_refuses_symlink(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    link = tmp_path / "tokens.json"
    link.symlink_to(target)
    store = FileTokenStore(path=link)
    # Refused either by the O_NOFOLLOW read guard or the write-side symlink check.
    with pytest.raises(OSError, match="symlink|symbolic|refusing"):
        store.set("k", {"refresh_token": "r"})


def test_registry_rejects_oversized_td(tmp_path, monkeypatch):
    monkeypatch.setenv("THINGCTX_MAX_TD_BYTES", "50")
    big = tmp_path / "big.td.json"
    big.write_text(json.dumps({"id": "urn:x", "pad": "y" * 200}))
    with pytest.raises(ValueError, match="limit"):
        FileRegistry(str(big)).fetch()


def test_registry_reads_normal_td(tmp_path):
    ok = tmp_path / "ok.td.json"
    ok.write_text(json.dumps({"id": "urn:x", "title": "x"}))
    assert FileRegistry(str(ok)).fetch()[0]["id"] == "urn:x"
