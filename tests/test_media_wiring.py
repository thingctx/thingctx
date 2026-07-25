# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Wiring tests: media routing precedence, ThingClient.frames(), the media /
invoke split, and LLMHost.see() image handoff. All offline (fake backend, fake
chat_fn); no network, no real model."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

# The media plane is an optional extra; skip the whole module when its deps
# (numpy, and the media backend stack) are not installed. The thingctx imports
# below follow the skip on purpose (E402), so collection never touches media
# code when the deps are absent.
np = pytest.importorskip("numpy")

from thingctx.bindings import HttpBinding, select_binding  # noqa: E402
from thingctx.bindings.builtin.media import Frame, MediaBinding, is_media_form  # noqa: E402
from thingctx.contrib.llm import LLMHost  # noqa: E402
from thingctx.runtime import ThingClient  # noqa: E402
from thingctx.thing import WoTForm  # noqa: E402


class _FakeBackend:
    def __init__(self, count: int = 4):
        self.count = count

    def can_open(self, url: str, hint: dict) -> bool:
        return True

    def read(self, url: str, *, options: dict, stop: threading.Event) -> Iterator[Frame]:
        kind = options.get("track", "video")
        for i in range(self.count):
            if stop.is_set():
                return
            yield Frame(data=i, kind=kind, pts=float(i))

    def write(self, frames, target, *, options, stop):
        raise NotImplementedError


def _media_td(href: str, hint: dict | None = None) -> dict:
    form: dict = {"href": href}
    if hint is not None:
        form["x-thingctx-media"] = hint
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:cam1",
        "title": "cam",
        "actions": {"watch": {"forms": [form]}},
    }


def _client(href: str, hint: dict | None = None) -> ThingClient:
    return ThingClient(
        tds=[_media_td(href, hint)],
        bindings=[HttpBinding(), MediaBinding(backends=[_FakeBackend()])],
    )


# routing precedence


def test_is_media_form():
    assert is_media_form(WoTForm(href="rtsp://x/y"))
    assert is_media_form(WoTForm(href="https://x/y", raw={"x-thingctx-media": {"a": 1}}))
    assert not is_media_form(WoTForm(href="https://x/y"))


def test_media_wins_over_http_for_hinted_form():
    http = HttpBinding()
    media = MediaBinding(backends=[_FakeBackend()])
    # http(s) href + media hint -> media binding, not http.
    hinted = WoTForm(href="https://x/y", raw={"x-thingctx-media": {"k": 1}})
    assert select_binding([http, media], hinted) is media
    # rtsp scheme -> media even though http is listed first.
    assert select_binding([http, media], WoTForm(href="rtsp://x/y")) is media
    # plain http(s) -> http, untouched.
    assert select_binding([http, media], WoTForm(href="https://x/y")) is http


# ThingClient frames() and media/invoke split


def test_media_action_split_from_invoke_tools():
    client = _client("rtsp://cam/stream")
    assert client.list_actions() == []  # not an invoke tool
    assert client.list_media() == ["cam1__watch"]


def test_invoke_on_media_is_redirected():
    client = _client("rtsp://cam/stream")
    res = asyncio.run(client.invoke("cam1__watch"))
    assert res.get("media") is True
    assert "frames(" in res["error"]


def test_client_frames_yields_video_and_audio():
    client = _client("rtsp://cam/stream")

    async def run():
        vid = [f async for f in await client.frames("cam1__watch", track="video")]
        aud = [f async for f in await client.frames("cam1__watch", track="audio")]
        return vid, aud

    vid, aud = asyncio.run(run())
    assert [f.data for f in vid] == [0, 1, 2, 3]
    assert all(f.kind == "video" for f in vid)
    assert all(f.kind == "audio" for f in aud)


def test_frames_over_http_hinted_form():
    # An http(s) href with a media hint still routes to media via the client.
    client = _client("https://example.com/clip.mp4", hint={"container": "mp4"})
    assert client.list_media() == ["cam1__watch"]

    async def run():
        return [f async for f in await client.frames("cam1__watch")]

    assert len(asyncio.run(run())) == 4


def test_frames_unknown_affordance_is_empty():
    client = _client("rtsp://cam/stream")

    async def run():
        return [f async for f in await client.frames("cam1__nope")]

    assert asyncio.run(run()) == []


# VLM see() handoff


def test_see_passes_frame_as_image():
    saw: dict = {}

    async def chat(messages, tools):
        user = next(m for m in messages if m["role"] == "user")
        saw["parts"] = user["content"]
        return {"role": "assistant", "content": "ok"}

    client = _client("rtsp://cam/stream")
    host = LLMHost(client, chat_fn=chat)
    frame = Frame(data=np.zeros((8, 8, 3), dtype=np.uint8), kind="video")
    answer = asyncio.run(host.see(frame, "what is this?"))

    assert answer == "ok"
    parts = saw["parts"]
    assert any(p.get("type") == "text" for p in parts)
    img = next(p for p in parts if p.get("type") == "image_url")
    assert img["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_see_passes_a_list_of_frames_as_a_clip():
    saw: dict = {}

    async def chat(messages, tools):
        user = next(m for m in messages if m["role"] == "user")
        saw["parts"] = user["content"]
        return {"role": "assistant", "content": "ok"}

    client = _client("rtsp://cam/stream")
    host = LLMHost(client, chat_fn=chat)
    clip = [Frame(data=np.zeros((8, 8, 3), dtype=np.uint8), kind="video") for _ in range(3)]
    asyncio.run(host.see(clip, "describe this clip"))

    imgs = [p for p in saw["parts"] if p.get("type") == "image_url"]
    assert len(imgs) == 3
    assert all(p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in imgs)


def test_see_video_sends_a_native_video_url():
    saw: dict = {}

    async def chat(messages, tools):
        user = next(m for m in messages if m["role"] == "user")
        saw["parts"] = user["content"]
        return {"role": "assistant", "content": "ok"}

    client = _client("rtsp://cam/stream")
    host = LLMHost(client, chat_fn=chat)
    asyncio.run(host.see_video("https://example.com/clip.mp4", "summarize"))

    vid = next(p for p in saw["parts"] if p.get("type") == "video_url")
    assert vid["video_url"]["url"] == "https://example.com/clip.mp4"


def test_sample_frames_spaces_by_pts():
    from thingctx.bindings.builtin.media import sample_frames

    async def gen():
        for i in range(20):
            yield Frame(data=i, kind="video", pts=float(i))

    picked = asyncio.run(sample_frames(gen(), count=4, every=3.0))
    assert [f.pts for f in picked] == [0.0, 3.0, 6.0, 9.0]


# parameterized media source (URL passed at call time)


def test_fill_plus_var_substitutes_url_verbatim():
    # {var} percent-encodes; {+var} (reserved expansion) keeps the URL intact.
    enc = WoTForm(href="https://api/x?u={u}")
    raw = WoTForm(href="{+u}")
    url = "https://www.twitch.tv/c?x=1"
    assert enc.fill({"u": url})[0] == "https://api/x?u=https%3A%2F%2Fwww.twitch.tv%2Fc%3Fx%3D1"
    assert raw.fill({"u": url})[0] == url


def test_frames_with_url_argument_routes_verbatim_to_backend():
    seen: dict = {}

    class _RecordingBackend(_FakeBackend):
        def read(self, url, *, options, stop):
            seen["url"] = url
            yield from super().read(url, options=options, stop=stop)

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:pages",
        "title": "pages",
        "actions": {
            "watch": {
                "uriVariables": {"url": {"type": "string"}},
                "forms": [{"href": "{+url}", "x-thingctx-media": {"resolve": "page"}}],
            }
        },
    }
    client = ThingClient(
        tds=[td], bindings=[HttpBinding(), MediaBinding(backends=[_RecordingBackend()])]
    )
    target = "https://vimeo.com/123?h=abc"

    async def run():
        return [f async for f in await client.frames("pages__watch", {"url": target})]

    frames = asyncio.run(run())
    assert len(frames) == 4
    assert seen["url"] == target  # passed through unencoded, end to end


def test_call_time_options_reach_backend():
    seen: dict = {}

    class _RecordingBackend(_FakeBackend):
        def read(self, url, *, options, stop):
            seen.update(options)
            yield from super().read(url, options=options, stop=stop)

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:pages",
        "title": "pages",
        "actions": {
            "watch": {
                "uriVariables": {"url": {"type": "string"}},
                "forms": [{"href": "{+url}", "x-thingctx-media": {"resolve": "page"}}],
            }
        },
    }
    client = ThingClient(
        tds=[td], bindings=[HttpBinding(), MediaBinding(backends=[_RecordingBackend()])]
    )

    async def run():
        return [
            f
            async for f in await client.frames(
                "pages__watch",
                {"url": "https://youtu.be/x", "cookies_from_browser": "safari", "format": "best"},
                track="video",
            )
        ]

    asyncio.run(run())
    # The non-uriVariable args reached the backend options,
    assert seen["cookies_from_browser"] == "safari"
    assert seen["format"] == "best"
    # the consumed uriVariable did not leak in as an option,
    assert "url" not in seen
    # and the explicit track is reserved (an arg cannot override it).
    assert seen["track"] == "video"


def test_call_time_arg_cannot_override_track():
    seen: dict = {}

    class _RecordingBackend(_FakeBackend):
        def read(self, url, *, options, stop):
            seen.update(options)
            yield from super().read(url, options=options, stop=stop)

    client = ThingClient(
        tds=[_media_td("https://x/y", {"resolve": "page"})],
        bindings=[HttpBinding(), MediaBinding(backends=[_RecordingBackend()])],
    )

    async def run():
        return [
            f async for f in await client.frames("cam1__watch", {"track": "audio"}, track="video")
        ]

    asyncio.run(run())
    assert seen["track"] == "video"
