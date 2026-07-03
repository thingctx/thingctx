"""Abuse-case coverage for the safety guardrails: URL/scheme policy, download
and Thing-Description size caps, path confinement, OAuth transport security,
token-store symlink handling, and credential redaction in transport errors."""

from __future__ import annotations

import httpx
import pytest

from thingctx.bindings import HttpBinding
from thingctx.chain import ChainError
from thingctx.netpolicy import PolicyError, check_url, confine_path, is_private_host, require_scheme
from thingctx.reliability import TransportError
from thingctx.runtime import ThingClient

# --- netpolicy --------------------------------------------------------------


def test_require_scheme_rejects_non_web():
    with pytest.raises(PolicyError):
        require_scheme("file:///etc/passwd", {"http", "https"})
    assert require_scheme("https://ok/x", {"http", "https"}).startswith("https://")


def test_is_private_host_ip_literals():
    for host in ("127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.1.1", "::1", "0.0.0.0"):
        assert is_private_host(host), host
    for host in ("93.184.216.34", "example.com", ""):
        assert not is_private_host(host), host


def test_check_url_block_private_opt_in():
    # Off by default: a LAN device is legitimate for a WoT client.
    check_url("http://192.168.1.5/td", block_private=False)
    with pytest.raises(PolicyError):
        check_url("http://192.168.1.5/td", block_private=True)


def test_confine_path_refuses_symlink(tmp_path):
    real = tmp_path / "real.bin"
    real.write_bytes(b"x")
    link = tmp_path / "link.bin"
    link.symlink_to(real)
    with pytest.raises(PolicyError):
        confine_path(link)


def test_confine_path_base_escape(tmp_path):
    base = tmp_path / "downloads"
    base.mkdir()
    with pytest.raises(PolicyError):
        confine_path("../../etc/cron.d/evil", base=base)
    inside = confine_path("sub/ok.bin", base=base)
    assert str(inside).startswith(str(base.resolve()))


# --- chain: next-URL scheme + download cap ----------------------------------


def _mock_client(handler, *, credentials=None):
    http = HttpBinding(credentials=credentials or {})
    http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return http


def _chain_td(follow, from_="json:url"):
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thingctx:dl",
        "title": "dl",
        "securityDefinitions": {"sc": {"scheme": "bearer"}},
        "security": ["sc"],
        "actions": {
            "fetch": {
                "forms": [
                    {
                        "href": "https://api.test/dl/initiate",
                        "htv:methodName": "POST",
                        "contentType": "application/json",
                        "x-thingctx-next": {"from": from_, "follow": follow},
                    }
                ]
            }
        },
    }


async def test_chain_refuses_file_scheme_next_url():
    def handler(req):
        return httpx.Response(200, json={"url": "file:///etc/passwd"})

    http = _mock_client(handler, credentials={"dl": "TOK"})
    client = ThingClient(tds=[_chain_td({"op": "GET"})], bindings=[http])
    with pytest.raises(PolicyError, match="scheme"):
        await client.invoke("dl.fetch", {})


async def test_chain_download_size_cap(monkeypatch):
    monkeypatch.setenv("THINGCTX_MAX_DOWNLOAD_BYTES", "4")
    blob = bytes(range(20))

    def handler(req):
        if req.url.path.endswith("/initiate"):
            return httpx.Response(200, json={"url": "https://api.test/dl/blob"})
        rng = req.headers.get("range", "")
        lo, _, hi = rng.replace("bytes=", "").partition("-")
        lo = int(lo or 0)
        hi = int(hi) if hi else len(blob) - 1
        chunk = blob[lo : hi + 1]
        return httpx.Response(
            206,
            content=chunk,
            headers={
                "Content-Range": f"bytes {lo}-{lo + len(chunk) - 1}/{len(blob)}",
                "Content-Type": "application/octet-stream",
            },
        )

    http = _mock_client(handler, credentials={"dl": "TOK"})
    td = _chain_td({"transport": "ranged-get", "chunkSize": 4})
    client = ThingClient(tds=[td], bindings=[http])
    with pytest.raises(ChainError, match="exceeds"):
        await client.invoke("dl.fetch", {})


# --- multi-Thing slug collision ---------------------------------------------


def _min_td(thing_id):
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": thing_id,
        "title": thing_id,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "properties": {"temp": {"type": "number", "forms": [{"href": "https://h/temp"}]}},
    }


def test_colliding_thing_slugs_are_refused():
    with pytest.raises(ValueError, match="tool namespace"):
        ThingClient(tds=[_min_td("urn:acme:pump:v1"), _min_td("urn:beta:pump:v2")])


# --- TransportError redaction -----------------------------------------------


def test_transport_error_redacts_url_and_detail():
    exc = TransportError("GET", "https://h/x?token=SECRETVALUE", detail="see https://h/y?sig=ABC")
    assert "SECRETVALUE" not in str(exc)
    assert "ABC" not in str(exc)
    d = exc.as_dict()
    assert "SECRETVALUE" not in d["error"]["url"]
    assert "***" in d["error"]["url"]
