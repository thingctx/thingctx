# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""HTTP binding request bodies (binary/form/multipart).

Three layers:

* unit tests for ``_http_body`` (how a contentType maps to httpx kwargs);
* integration through ``HttpBinding.invoke`` over a MockTransport, asserting the
  bytes and headers actually sent, and that a one-shot stream disables retries;
* ``from_openapi`` declaring the right form contentType + body argument.

The resumable transports (upload and download) are exercised through the
response-chaining engine in ``test_response_chain.py``.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import httpx
import pytest

from thingctx import HttpBinding, TransportError, from_openapi
from thingctx.bindings.builtin.http import _file_part, _http_body, _part_content


def _af(method="POST", href="https://api.local/up", content_type=None):
    action = SimpleNamespace(thing_id=None, idempotent=method.upper() in ("GET", "HEAD"))
    form = SimpleNamespace(href=href, raw={"htv:methodName": method}, content_type=content_type)
    return action, form


@pytest.fixture
def routed(monkeypatch):
    """Drive every AsyncClient through a handler the test controls; record each
    request so the body and headers can be asserted."""
    state = {"responses": [], "requests": [], "calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        state["requests"].append(request)
        nxt = state["responses"].pop(0)
        return nxt(request) if callable(nxt) else nxt

    real = httpx.AsyncClient

    def fake(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake)
    return state


# --- _http_body unit -------------------------------------------------------


def test_body_json_default_and_explicit():
    kw, hdr, stream = _http_body(None, {"a": 1})
    assert kw == {"json": {"a": 1}} and hdr == {} and stream is False
    kw, _, _ = _http_body("application/json", {"a": 1})
    assert kw == {"json": {"a": 1}}
    kw, _, _ = _http_body("application/merge-patch+json", {"a": 1})
    assert kw == {"json": {"a": 1}}


def test_body_form_urlencoded():
    kw, hdr, stream = _http_body("application/x-www-form-urlencoded", {"a": "b"})
    assert kw == {"data": {"a": "b"}} and stream is False


def test_body_multipart_splits_files_and_fields():
    kw, _, stream = _http_body("multipart/form-data", {"caption": "hi", "file": b"PNGDATA"})
    assert kw["data"] == {"caption": "hi"}
    assert kw["files"] == {"file": b"PNGDATA"}
    assert stream is True


def test_part_content_coerces_str_path_bytes(tmp_path):
    # A str with no matching file is inline text; a real path / file:// URL is
    # read; bytes and a file-like object pass through.
    assert _part_content("<srt text>") == b"<srt text>"
    p = tmp_path / "c__srt"
    p.write_bytes(b"SRTDATA")
    assert _part_content(str(p)) == b"SRTDATA"
    assert _part_content(p) == b"SRTDATA"
    assert _part_content(p.as_uri()) == b"SRTDATA"
    assert _part_content(b"raw") == b"raw"
    fh = io.BytesIO(b"y")
    assert _part_content(fh) is fh


def test_file_part_list_inline_and_path(tmp_path):
    # A [filename, content, content-type?] part from JSON arrives as a list; its
    # content element must be coerced to bytes for httpx files=.
    name, content, ctype = _file_part(["c__srt", "<srt>", "application/octet-stream"])
    assert (name, content, ctype) == ("c__srt", b"<srt>", "application/octet-stream")
    p = tmp_path / "s.srt"
    p.write_bytes(b"FILE")
    _, content, _ = _file_part(["c__srt", str(p), "application/octet-stream"])
    assert content == b"FILE"
    name, content = _file_part(["c__srt", "hi"])  # two-element part, no content type
    assert (name, content) == ("c__srt", b"hi")


def test_body_raw_octet_from_bytes_and_sets_header():
    kw, hdr, stream = _http_body("application/octet-stream", b"\x00\x01")
    assert kw == {"content": b"\x00\x01"}
    assert hdr == {"Content-Type": "application/octet-stream"}
    assert stream is False


def test_body_raw_from_body_key_and_single_key():
    kw, _, _ = _http_body("video/mp4", {"body": b"abc", "ignored_is_error": None})
    assert kw == {"content": b"abc"}
    kw, _, _ = _http_body("video/mp4", {"only": b"xyz"})
    assert kw == {"content": b"xyz"}


def test_decode_empty_2xx_body_is_not_parsed():
    from thingctx.bindings.base import _decode

    # A 204 (or any empty 2xx) must not be JSON-decoded, even when it still
    # declares a JSON content type; an empty body yields `empty`.
    resp = httpx.Response(204, headers={"content-type": "application/json"})
    assert _decode(resp) is None
    assert _decode(resp, empty={}) == {}


def test_body_raw_ambiguous_mapping_errors():
    with pytest.raises(ValueError, match="single value"):
        _http_body("application/octet-stream", {"a": b"1", "b": b"2"})


def test_body_raw_path_is_streamed(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"hello")
    kw, hdr, stream = _http_body("application/octet-stream", p)
    assert stream is True
    assert hdr == {"Content-Type": "application/octet-stream"}
    # content is an async byte stream (not the Path, not a sync file)
    assert hasattr(kw["content"], "__aiter__")


def test_body_raw_filelike_is_streamed():
    kw, _, stream = _http_body("application/octet-stream", io.BytesIO(b"z"))
    assert stream is True


# --- HttpBinding.invoke integration ---------------------------------------


@pytest.mark.asyncio
async def test_invoke_octet_stream_sends_raw_bytes(routed):
    routed["responses"] = [httpx.Response(200, json={"ok": True})]
    action, form = _af(content_type="application/octet-stream")
    async with HttpBinding() as b:
        out = await b.invoke(action, form, {"body": b"RAWBYTES"})
    assert out == {"ok": True}
    req = routed["requests"][0]
    assert req.content == b"RAWBYTES"
    assert req.headers["content-type"] == "application/octet-stream"


@pytest.mark.asyncio
async def test_invoke_multipart_sends_file(routed):
    routed["responses"] = [httpx.Response(201, json={"id": "v1"})]
    action, form = _af(content_type="multipart/form-data")
    async with HttpBinding() as b:
        await b.invoke(action, form, {"title": "clip", "file": b"FILEBYTES"})
    req = routed["requests"][0]
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b"FILEBYTES" in req.content
    assert b'name="title"' in req.content


@pytest.mark.asyncio
async def test_invoke_multipart_list_part_from_json(routed):
    # The shape a CLI/JSON caller produces: a list part with inline str content.
    routed["responses"] = [httpx.Response(200, json={"ok": True})]
    action, form = _af(content_type="multipart/form-data")
    async with HttpBinding() as b:
        out = await b.invoke(
            action, form, {"caption": ["c__srt", "<srt text>", "application/octet-stream"]}
        )
    assert out == {"ok": True}
    req = routed["requests"][0]
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'filename="c__srt"' in req.content
    assert b"<srt text>" in req.content


@pytest.mark.asyncio
async def test_invoke_multipart_list_part_path_from_json(routed, tmp_path):
    p = tmp_path / "sk.srt"
    p.write_bytes(b"CAPTIONBYTES")
    routed["responses"] = [httpx.Response(200, json={"ok": True})]
    action, form = _af(content_type="multipart/form-data")
    async with HttpBinding() as b:
        await b.invoke(action, form, {"caption": ["c__srt", str(p), "application/octet-stream"]})
    req = routed["requests"][0]
    assert b"CAPTIONBYTES" in req.content
    assert str(p).encode() not in req.content  # the path was read, not sent literally


@pytest.mark.asyncio
async def test_invoke_form_urlencoded(routed):
    routed["responses"] = [httpx.Response(200, json={"ok": True})]
    action, form = _af(content_type="application/x-www-form-urlencoded")
    async with HttpBinding() as b:
        await b.invoke(action, form, {"grant": "x", "code": "y"})
    req = routed["requests"][0]
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    assert req.content == b"grant=x&code=y"


@pytest.mark.asyncio
async def test_invoke_json_still_default(routed):
    routed["responses"] = [httpx.Response(200, json={"ok": True})]
    action, form = _af(content_type=None)
    async with HttpBinding() as b:
        await b.invoke(action, form, {"a": 1})
    req = routed["requests"][0]
    assert req.headers["content-type"] == "application/json"
    assert req.content == b'{"a":1}' or req.content == b'{"a": 1}'


@pytest.mark.asyncio
async def test_stream_body_disables_retry_on_idempotent(routed, tmp_path):
    # A PUT is normally retried, but a one-shot file body must not be re-sent.
    p = tmp_path / "blob.bin"
    p.write_bytes(b"x" * 32)
    routed["responses"] = [
        httpx.Response(503),  # would be retried for a reusable body
        httpx.Response(200, json={"ok": True}),
    ]
    action, form = _af(method="PUT", content_type="application/octet-stream")
    async with HttpBinding(retries=3, backoff=0.0) as b:
        with pytest.raises(TransportError):
            await b.invoke(action, form, {"body": p})
    assert routed["calls"] == 1  # single attempt, no retry


@pytest.mark.asyncio
async def test_inmemory_body_still_retries_on_idempotent(routed):
    routed["responses"] = [
        httpx.Response(503),
        httpx.Response(200, json={"ok": True}),
    ]
    action, form = _af(method="PUT", content_type="application/octet-stream")
    async with HttpBinding(retries=3, backoff=0.0) as b:
        out = await b.invoke(action, form, {"body": b"reusable"})
    assert out == {"ok": True}
    assert routed["calls"] == 2  # retried, body re-sent from bytes


@pytest.mark.asyncio
async def test_invoke_204_no_content_succeeds(routed):
    # A successful DELETE returns 204 with an empty body; invoke returns None
    # rather than raising a JSON decode error.
    routed["responses"] = [httpx.Response(204, headers={"content-type": "application/json"})]
    action, form = _af(method="DELETE")
    async with HttpBinding() as b:
        out = await b.invoke(action, form, {})
    assert out is None


# --- from_openapi body modeling -------------------------------------------


def _spec_with_body(media_type: str, schema: dict | None = None):
    content = {media_type: {"schema": schema or {"type": "string", "format": "binary"}}}
    return {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "https://api.local"}],
        "paths": {
            "/up": {
                "post": {
                    "operationId": "upload",
                    "requestBody": {"content": content},
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }


def _action(td):
    return next(iter(td["actions"].values()))


def test_openapi_octet_stream_form_and_body_arg():
    td = from_openapi(_spec_with_body("application/octet-stream"))
    act = _action(td)
    form = act["forms"][0]
    assert form["contentType"] == "application/octet-stream"
    assert "body" in act["input"]["properties"]
    assert act["input"]["properties"]["body"]["format"] == "binary"


def test_openapi_urlencoded_form_contenttype():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    td = from_openapi(_spec_with_body("application/x-www-form-urlencoded", schema))
    act = _action(td)
    assert act["forms"][0]["contentType"] == "application/x-www-form-urlencoded"
    assert "a" in act["input"]["properties"]


def test_openapi_json_unchanged():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    td = from_openapi(_spec_with_body("application/json", schema))
    act = _action(td)
    assert act["forms"][0]["contentType"] == "application/json"
