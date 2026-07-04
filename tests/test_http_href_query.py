# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The HTTP binding must preserve a query string declared in a form href.

httpx drops a URL's existing query as soon as ``params=`` is passed, and the
binding always passes ``params=``; so a TD whose href is the full endpoint
(``.../videos?part=snippet``) would silently lose ``part``. These tests assert
the href query survives across invoke/read/write, with call-time params layered
on top.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from thingctx import HttpBinding
from thingctx.bindings.builtin.http import _merge_href_query


def _af(method="POST", href="https://api.local/v3/videos?part=snippet", content_type=None):
    action = SimpleNamespace(thing_id=None, idempotent=method.upper() in ("GET", "HEAD"))
    form = SimpleNamespace(href=href, raw={"htv:methodName": method}, content_type=content_type)
    return action, form


@pytest.fixture
def routed(monkeypatch):
    state = {"responses": [], "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        nxt = state["responses"].pop(0)
        return nxt(request) if callable(nxt) else nxt

    real = httpx.AsyncClient

    def fake(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake)
    return state


# --- _merge_href_query unit ------------------------------------------------


def test_merge_no_query_is_unchanged():
    url, params = _merge_href_query("https://x/y", {"a": "1"})
    assert url == "https://x/y"
    assert params == {"a": "1"}


def test_merge_keeps_href_query():
    url, params = _merge_href_query("https://x/y?part=snippet&uploadType=resumable", None)
    assert url == "https://x/y"
    assert params == {"part": "snippet", "uploadType": "resumable"}


def test_merge_call_params_win_over_href():
    url, params = _merge_href_query("https://x/y?part=snippet", {"part": "status"})
    assert url == "https://x/y"
    assert params == {"part": "status"}


# --- integration through the binding ---------------------------------------


@pytest.mark.asyncio
async def test_invoke_post_preserves_href_query(routed):
    routed["responses"] = [httpx.Response(200, json={"ok": True})]
    action, form = _af(method="PUT")
    async with HttpBinding() as b:
        await b.invoke(action, form, {"id": "vid1"})
    req = routed["requests"][0]
    assert req.url.params.get("part") == "snippet"
    assert str(req.url).startswith("https://api.local/v3/videos?")


@pytest.mark.asyncio
async def test_invoke_get_merges_href_query_and_args(routed):
    routed["responses"] = [httpx.Response(200, json={"ok": True})]
    action, form = _af(method="GET", href="https://api.local/list?fields=id")
    async with HttpBinding() as b:
        await b.invoke(action, form, {"q": "cats"})
    req = routed["requests"][0]
    assert req.url.params.get("fields") == "id"
    assert req.url.params.get("q") == "cats"


@pytest.mark.asyncio
async def test_read_preserves_href_query(routed):
    routed["responses"] = [httpx.Response(200, json={"v": 1})]
    prop = SimpleNamespace(thing_id=None)
    form = SimpleNamespace(href="https://api.local/state?view=full", raw={}, content_type=None)
    async with HttpBinding() as b:
        await b.read(prop, form)
    req = routed["requests"][0]
    assert req.url.params.get("view") == "full"


@pytest.mark.asyncio
async def test_write_preserves_href_query(routed):
    routed["responses"] = [httpx.Response(200, json={"ok": True})]
    prop = SimpleNamespace(thing_id=None)
    form = SimpleNamespace(href="https://api.local/state?unit=c", raw={}, content_type=None)
    async with HttpBinding() as b:
        await b.write(prop, form, 42)
    req = routed["requests"][0]
    assert req.url.params.get("unit") == "c"


@pytest.mark.asyncio
async def test_call_arg_overrides_href_query_on_get(routed):
    routed["responses"] = [httpx.Response(200, json={"ok": True})]
    action, form = _af(method="GET", href="https://api.local/list?part=snippet")
    async with HttpBinding() as b:
        await b.invoke(action, form, {"part": "status"})
    req = routed["requests"][0]
    assert req.url.params.get("part") == "status"
