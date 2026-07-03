"""Response chaining engine.

Offline, via httpx MockTransport mounted on the binding's pooled client. Covers
the next-address extractors, the three follow modes (op / resumable / poll),
multi-hop recursion, the same-origin security default, and uniform auth resolved
from the TD security scheme (no hand-passed token).
"""

from __future__ import annotations

import httpx
import pytest

from thingctx.bindings import HttpBinding
from thingctx.bindings.builtin.http import HttpResult
from thingctx.chain import (
    ChainError,
    extract_next,
    json_path,
    same_origin,
)
from thingctx.reliability import TransportError
from thingctx.runtime import ThingClient


def _mock_client(handler, *, credentials=None):
    http = HttpBinding(credentials=credentials or {})
    http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return http


def _td(slug, action_name, form_extra, *, scheme="bearer"):
    form = {"href": f"https://api.test/{slug}/initiate", "htv:methodName": "POST"}
    form.update(form_extra)
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": f"urn:thingctx:{slug}",
        "title": slug,
        "securityDefinitions": {"sc": {"scheme": scheme}},
        "security": ["sc"],
        "actions": {action_name: {"forms": [form]}},
    }


# --- extractors -------------------------------------------------------------


def test_extract_header_case_insensitive():
    res = HttpResult(status=200, headers={"location": "https://x/s"}, body=None, url="")
    assert extract_next("header:Location", res) == "https://x/s"
    res2 = HttpResult(status=200, headers={"Location": "https://x/s"}, body=None, url="")
    assert extract_next("header:location", res2) == "https://x/s"


def test_extract_json_dotted_and_index():
    body = {"a": {"b": [{"url": "https://x/0"}, {"url": "https://x/1"}]}}
    res = HttpResult(status=200, headers={}, body=body, url="")
    assert extract_next("json:a.b[1].url", res) == "https://x/1"
    assert extract_next("json:$.a.b[0].url", res) == "https://x/0"
    assert json_path(body, "a.missing.x") is None


def test_unknown_extractor_raises():
    res = HttpResult(status=200, headers={}, body={}, url="")
    with pytest.raises(ChainError):
        extract_next("xpath:/a", res)


def test_same_origin():
    assert same_origin("https://h.test/a", "https://h.test/b?x=1")
    assert not same_origin("https://h.test/a", "https://other.test/b")
    assert not same_origin("https://h.test/a", "http://h.test/a")


# --- op mode: presigned upload (cross-origin, allowlisted, no creds) ---------


async def test_op_presigned_upload():
    seen = {}

    def handler(req):
        if req.url.path.endswith("/initiate"):
            assert req.headers["authorization"] == "Bearer TOK"
            return httpx.Response(200, json={"upload_url": "https://blob.test/o/1?sig=abc"})
        if req.url.host == "blob.test":
            # cross-origin presigned target: credentials must NOT be forwarded,
            # and the signed query must survive.
            seen["auth"] = "authorization" in req.headers
            seen["sig"] = req.url.params.get("sig")
            seen["body"] = req.content
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(req.url)

    http = _mock_client(handler, credentials={"up": "TOK"})
    td = _td(
        "up",
        "upload",
        {
            "contentType": "application/json",
            "x-thingctx-next": {
                "from": "json:upload_url",
                "allowOrigins": ["blob.test"],
                "follow": {
                    "op": "PUT",
                    "body": "{media}",
                    "contentType": "application/octet-stream",
                },
            },
        },
    )
    client = ThingClient(tds=[td], bindings=[http])
    out = await client.invoke("up.upload", {"media": b"PAYLOAD"})
    assert out == {"ok": True}
    assert seen == {"auth": False, "sig": "abc", "body": b"PAYLOAD"}


async def test_op_cross_origin_refused_without_allowlist():
    def handler(req):
        if req.url.path.endswith("/initiate"):
            return httpx.Response(200, json={"upload_url": "https://blob.test/o/1"})
        raise AssertionError("must not follow cross-origin")

    http = _mock_client(handler, credentials={"up": "TOK"})
    td = _td(
        "up",
        "upload",
        {
            "contentType": "application/json",
            "x-thingctx-next": {
                "from": "json:upload_url",
                "follow": {"op": "PUT", "body": "{media}"},
            },
        },
    )
    client = ThingClient(tds=[td], bindings=[http])
    with pytest.raises(ChainError, match="cross-origin"):
        await client.invoke("up.upload", {"media": b"X"})


# --- resumable mode (same-origin session, auth forwarded) -------------------


async def test_resumable_mode_chunks_and_finalizes():
    ranges = []

    def handler(req):
        if req.url.path.endswith("/initiate"):
            assert req.headers["authorization"] == "Bearer TOK"
            return httpx.Response(
                200, headers={"Location": "https://api.test/up/session/9"}, json={}
            )
        if req.url.path.endswith("/session/9"):
            assert req.headers["authorization"] == "Bearer TOK"  # same-origin: forwarded
            ranges.append(req.headers["content-range"])
            total = int(req.headers["content-range"].split("/")[1])
            end = int(req.headers["content-range"].split("-")[1].split("/")[0])
            if end + 1 >= total:
                return httpx.Response(200, json={"id": "vid123"})
            return httpx.Response(308)
        raise AssertionError(req.url)

    http = _mock_client(handler, credentials={"up": "TOK"})
    td = _td(
        "up",
        "upload",
        {
            "contentType": "application/json",
            "x-thingctx-next": {
                "from": "header:Location",
                "follow": {"transport": "resumable", "media": "{media}", "chunkSize": 4},
            },
        },
    )
    client = ThingClient(tds=[td], bindings=[http])
    out = await client.invoke("up.upload", {"media": b"0123456789"})
    assert out == {"id": "vid123"}
    assert ranges == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]


# --- poll mode (async job) --------------------------------------------------


async def test_poll_until_done():
    calls = {"n": 0}

    def handler(req):
        if req.url.path.endswith("/initiate"):
            return httpx.Response(200, headers={"Location": "https://api.test/up/job/7"}, json={})
        if req.url.path.endswith("/job/7"):
            calls["n"] += 1
            status = "DONE" if calls["n"] >= 3 else "RUNNING"
            return httpx.Response(200, json={"status": status, "result": 42})
        raise AssertionError(req.url)

    http = _mock_client(handler, credentials={"up": "TOK"})
    td = _td(
        "up",
        "run",
        {
            "x-thingctx-next": {
                "from": "header:Location",
                "follow": {
                    "op": "GET",
                    "until": {"path": "status", "in": ["DONE", "FAILED"]},
                    "error": {"path": "status", "equals": "FAILED"},
                    "interval": 0.0,
                    "timeout": 5,
                },
            }
        },
    )
    client = ThingClient(tds=[td], bindings=[http])
    out = await client.invoke("up.run", {})
    assert out["status"] == "DONE" and out["result"] == 42
    assert calls["n"] == 3


async def test_poll_error_condition_raises():
    def handler(req):
        if req.url.path.endswith("/initiate"):
            return httpx.Response(200, headers={"Location": "https://api.test/up/job/8"}, json={})
        return httpx.Response(200, json={"status": "FAILED"})

    http = _mock_client(handler, credentials={"up": "TOK"})
    td = _td(
        "up",
        "run",
        {
            "x-thingctx-next": {
                "from": "header:Location",
                "follow": {
                    "op": "GET",
                    "until": {"path": "status", "in": ["DONE"]},
                    "error": {"path": "status", "equals": "FAILED"},
                    "interval": 0.0,
                },
            }
        },
    )
    client = ThingClient(tds=[td], bindings=[http])
    with pytest.raises(ChainError, match="failure"):
        await client.invoke("up.run", {})


async def test_poll_timeout_raises():
    def handler(req):
        if req.url.path.endswith("/initiate"):
            return httpx.Response(200, headers={"Location": "https://api.test/up/job/9"}, json={})
        return httpx.Response(200, json={"status": "RUNNING"})

    http = _mock_client(handler, credentials={"up": "TOK"})
    td = _td(
        "up",
        "run",
        {
            "x-thingctx-next": {
                "from": "header:Location",
                "follow": {
                    "op": "GET",
                    "until": {"path": "status", "in": ["DONE"]},
                    "interval": 0.0,
                    "timeout": 0.0,
                },
            }
        },
    )
    client = ThingClient(tds=[td], bindings=[http])
    with pytest.raises(ChainError, match="timed out"):
        await client.invoke("up.run", {})


# --- multi-hop recursion ----------------------------------------------------


async def test_multi_hop_recursion():
    def handler(req):
        p = req.url.path
        if p.endswith("/initiate"):
            return httpx.Response(200, json={"next": "https://api.test/up/s2"})
        if p.endswith("/s2"):
            return httpx.Response(200, json={"next": "https://api.test/up/s3"})
        if p.endswith("/s3"):
            return httpx.Response(200, json={"done": True})
        raise AssertionError(req.url)

    http = _mock_client(handler, credentials={"up": "TOK"})
    td = _td(
        "up",
        "chain",
        {
            "contentType": "application/json",
            "x-thingctx-next": {
                "from": "json:next",
                "follow": {
                    "op": "GET",
                    "next": {"from": "json:next", "follow": {"op": "GET"}},
                },
            },
        },
    )
    client = ThingClient(tds=[td], bindings=[http])
    out = await client.invoke("up.chain", {})
    assert out == {"done": True}


# --- resumable upload: failure paths via the engine -------------------------


async def test_resumable_from_path_media(tmp_path):
    p = tmp_path / "v.bin"
    p.write_bytes(b"abcdef")

    def handler(req):
        if req.url.path.endswith("/initiate"):
            return httpx.Response(200, headers={"Location": "https://api.test/up/s"}, json={})
        return httpx.Response(201, json={"done": True})

    http = _mock_client(handler, credentials={"up": "TOK"})
    td = _td(
        "up",
        "upload",
        {
            "contentType": "application/json",
            "x-thingctx-next": {
                "from": "header:Location",
                "follow": {"transport": "resumable", "media": "{media}"},
            },
        },
    )
    client = ThingClient(tds=[td], bindings=[http])
    assert await client.invoke("up.upload", {"media": p}) == {"done": True}


@pytest.mark.parametrize("as_uri", [False, True])
async def test_resumable_media_accepts_str_path(tmp_path, as_uri):
    p = tmp_path / "v.bin"
    p.write_bytes(b"0123456789")
    ranges = []

    def handler(req):
        if req.url.path.endswith("/initiate"):
            return httpx.Response(200, headers={"Location": "https://api.test/up/s"}, json={})
        ranges.append(req.headers["content-range"])
        if "8-9" in req.headers["content-range"]:
            return httpx.Response(200, json={"done": True})
        return httpx.Response(308)

    http = _mock_client(handler, credentials={"up": "TOK"})
    td = _td(
        "up",
        "upload",
        {
            "contentType": "application/json",
            "x-thingctx-next": {
                "from": "header:Location",
                "follow": {"transport": "resumable", "media": "{media}", "chunkSize": 4},
            },
        },
    )
    client = ThingClient(tds=[td], bindings=[http])
    # An agent driving this over MCP can only pass JSON, i.e. a string path.
    media = p.as_uri() if as_uri else str(p)
    assert await client.invoke("up.upload", {"media": media}) == {"done": True}
    assert ranges == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]


async def test_resumable_initiate_error_raises():
    http = _mock_client(lambda req: httpx.Response(403, text="denied"), credentials={"up": "TOK"})
    td = _td(
        "up",
        "upload",
        {
            "contentType": "application/json",
            "x-thingctx-next": {
                "from": "header:Location",
                "follow": {"transport": "resumable", "media": "{media}"},
            },
        },
    )
    client = ThingClient(tds=[td], bindings=[http])
    with pytest.raises(TransportError):
        await client.invoke("up.upload", {"media": b"x"})


async def test_resumable_missing_location_raises():
    http = _mock_client(lambda req: httpx.Response(200, json={}), credentials={"up": "TOK"})
    td = _td(
        "up",
        "upload",
        {
            "contentType": "application/json",
            "x-thingctx-next": {
                "from": "header:Location",
                "follow": {"transport": "resumable", "media": "{media}"},
            },
        },
    )
    client = ThingClient(tds=[td], bindings=[http])
    with pytest.raises(ChainError, match="no next address"):
        await client.invoke("up.upload", {"media": b"x"})


# --- ranged-get (resumable download) ----------------------------------------


def _blob_server(blob, *, ranges=True, drop_at=None):
    """A MockTransport handler serving ``blob`` with Range support. ``drop_at``
    (a byte offset) raises a transport error once when a range starting there is
    first requested, to exercise resume."""
    state = {"dropped": set()}

    def handler(req):
        if req.url.path.endswith("/initiate"):
            return httpx.Response(200, json={"url": "https://api.test/dl/blob"})
        rng = req.headers.get("range")
        if not ranges or rng is None:
            return httpx.Response(
                200, content=blob, headers={"Content-Type": "application/octet-stream"}
            )
        lo, hi = rng.replace("bytes=", "").split("-")
        lo = int(lo)
        hi = int(hi) if hi else len(blob) - 1
        if drop_at is not None and lo == drop_at and lo not in state["dropped"]:
            state["dropped"].add(lo)
            raise httpx.ConnectError("connection dropped", request=req)
        chunk = blob[lo : hi + 1]
        return httpx.Response(
            206,
            content=chunk,
            headers={
                "Content-Range": f"bytes {lo}-{lo + len(chunk) - 1}/{len(blob)}",
                "Content-Type": "application/octet-stream",
            },
        )

    return handler


def _download_td(follow_extra=None):
    follow = {"transport": "ranged-get", "chunkSize": 4}
    follow.update(follow_extra or {})
    return _td(
        "dl",
        "fetch",
        {
            "contentType": "application/json",
            "x-thingctx-next": {"from": "json:url", "follow": follow},
        },
    )


async def test_ranged_get_assembles_full_blob():
    blob = bytes(range(20))
    http = _mock_client(_blob_server(blob), credentials={"dl": "TOK"})
    client = ThingClient(tds=[_download_td()], bindings=[http])
    out = await client.invoke("dl.fetch", {})
    assert out == blob


async def test_ranged_get_resumes_after_drop():
    blob = bytes(range(20))
    # Drop the chunk starting at byte 8 once; the engine must retry that range
    # and resume, not restart from zero.
    http = _mock_client(_blob_server(blob, drop_at=8), credentials={"dl": "TOK"})
    client = ThingClient(tds=[_download_td({"backoff": 0.0})], bindings=[http])
    out = await client.invoke("dl.fetch", {})
    assert out == blob


async def test_ranged_get_to_dest_file(tmp_path):
    blob = b"abcdefghij"
    dest = tmp_path / "out.bin"
    http = _mock_client(_blob_server(blob), credentials={"dl": "TOK"})
    client = ThingClient(tds=[_download_td({"dest": "{out}"})], bindings=[http])
    out = await client.invoke("dl.fetch", {"out": str(dest)})
    assert out == {"path": str(dest), "bytes": 10, "contentType": "application/octet-stream"}
    assert dest.read_bytes() == blob


async def test_ranged_get_falls_back_when_no_range_support():
    blob = b"no-range-support-body"
    http = _mock_client(_blob_server(blob, ranges=False), credentials={"dl": "TOK"})
    client = ThingClient(tds=[_download_td()], bindings=[http])
    out = await client.invoke("dl.fetch", {})
    assert out == blob
