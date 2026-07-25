# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for the media stream binding: the consume bridge, error
propagation, and backend selection. No network or media codecs."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

from thingctx.bindings.builtin.media import Frame, MediaBinding, MediaError
from thingctx.thing import WoTAction, WoTForm


class _FakeBackend:
    """Yields a fixed number of frames, or raises after a few. Records the
    options it was given (so the track selector can be asserted)."""

    def __init__(self, count: int = 5, *, raise_at: int | None = None):
        self.count = count
        self.raise_at = raise_at
        self.stopped = False
        self.seen_options: dict | None = None

    def can_open(self, url: str, hint: dict) -> bool:
        return True

    def read(self, url: str, *, options: dict, stop: threading.Event) -> Iterator[Frame]:
        self.seen_options = options
        kind = options.get("track", "video")
        for i in range(self.count):
            if stop.is_set():
                self.stopped = True
                return
            if self.raise_at is not None and i == self.raise_at:
                raise RuntimeError("decode boom")
            yield Frame(data=i, kind=kind, pts=float(i))

    def write(self, frames, target, *, options, stop):
        raise NotImplementedError


def _form(href="rtsp://cam.local/stream", hint=None) -> WoTForm:
    raw = {"x-thingctx-media": hint} if hint else {}
    return WoTForm(href=href, raw=raw)


_ACTION = WoTAction(
    name="watch",
    thing_id="urn:thingctx:cam:test",
    description="",
    input_schema={},
    output_schema=None,
    idempotent=True,
    forms=(),
)


async def _collect(inv, form, limit=None, *, track="video"):
    out = []
    async for fr in inv.frames(_ACTION, form, {}, track=track):
        out.append(fr)
        if limit is not None and len(out) >= limit:
            break
    return out


def test_backpressure_all_is_lossless():
    inv = MediaBinding(backends=[_FakeBackend(count=5)], max_queue=2, backpressure="all")
    frames = asyncio.run(_collect(inv, _form()))
    assert [f.data for f in frames] == [0, 1, 2, 3, 4]
    assert all(f.kind == "video" for f in frames)


def test_track_selects_audio():
    fake = _FakeBackend(count=3)
    inv = MediaBinding(backends=[fake], backpressure="all")
    frames = asyncio.run(_collect(inv, _form(), track="audio"))
    assert fake.seen_options["track"] == "audio"
    assert all(f.kind == "audio" for f in frames)


def test_bad_track_rejected():
    inv = MediaBinding(backends=[_FakeBackend()])
    with pytest.raises(ValueError, match="track"):
        asyncio.run(_collect(inv, _form(), track="subtitles"))


def test_backpressure_latest_sheds_when_consumer_lags():
    inv = MediaBinding(backends=[_FakeBackend(count=100)], max_queue=2, backpressure="latest")

    async def _slow():
        out = []
        async for fr in inv.frames(_ACTION, _form(), {}):
            out.append(fr.data)
            await asyncio.sleep(0.005)  # lag behind the producer
        return out

    got = asyncio.run(_slow())
    assert len(got) < 100  # frames were shed under backpressure
    assert got == sorted(got)  # but order is preserved (monotonic)


def test_bad_backpressure_rejected():
    with pytest.raises(ValueError, match="backpressure"):
        MediaBinding(backends=[_FakeBackend()], backpressure="nope")


def test_backend_error_propagates():
    # Backend errors surface as a MediaError (credentials redacted), preserving
    # the original type name and message.
    inv = MediaBinding(backends=[_FakeBackend(count=10, raise_at=3)])
    with pytest.raises(MediaError, match="decode boom"):
        asyncio.run(_collect(inv, _form()))


def test_early_break_sets_stop():
    fake = _FakeBackend(count=1000)
    inv = MediaBinding(backends=[fake], max_queue=2)
    frames = asyncio.run(_collect(inv, _form(), limit=3))
    assert len(frames) == 3
    # the worker observes stop shortly after the consumer leaves the loop
    for _ in range(100):
        if fake.stopped:
            break
        import time

        time.sleep(0.01)
    assert fake.stopped


def test_invoke_is_rejected():
    inv = MediaBinding(backends=[_FakeBackend()])
    with pytest.raises(TypeError, match="no request/response surface"):
        asyncio.run(inv.invoke(_ACTION, _form(), {}))


def test_handles_media_scheme_and_hint():
    inv = MediaBinding(backends=[_FakeBackend()])
    assert inv.handles(_form("rtsp://x/y"))
    assert inv.handles(_form("https://any.site/watch", hint={"resolve": "page"}))
    assert not inv.handles(_form("https://api.example.com/v1"))


def test_extractor_backend_selected_by_hint_not_host():
    from thingctx.bindings.builtin.media.backends import ExtractorBackend

    be = ExtractorBackend()
    # declared intent routes here, for any site; no hostname knowledge
    assert be.can_open("https://www.twitch.tv/x", {"resolve": "page"})
    assert be.can_open("https://vimeo.com/123", {"resolve": "page"})
    assert be.can_open("https://example.com/anything", {"source": "youtube"})  # back-compat
    # a bare youtube URL with no hint is NOT claimed by hostname anymore
    assert not be.can_open("https://www.youtube.com/watch?v=abc", {})


def test_no_backend_raises():
    class _Picky(_FakeBackend):
        def can_open(self, url, hint):
            return False

    inv = MediaBinding(backends=[_Picky()])
    with pytest.raises(LookupError):
        asyncio.run(_collect(inv, _form()))


# publish (produce) path


class _WriterBackend:
    """Records the frames handed to write(), the target, and the options. Can be
    told to raise mid-stream to exercise error propagation."""

    def __init__(self, *, raise_at: int | None = None):
        self.written: list[Frame] = []
        self.target: str | None = None
        self.seen_options: dict | None = None
        self.raise_at = raise_at

    def can_open(self, url: str, hint: dict) -> bool:
        return True

    def read(self, url: str, *, options: dict, stop: threading.Event):
        raise NotImplementedError

    def write(self, frames, target, *, options, stop):
        self.target = target
        self.seen_options = options
        for i, fr in enumerate(frames):
            if self.raise_at is not None and i == self.raise_at:
                raise RuntimeError("encode boom rtmp://h/app/secretkey")
            self.written.append(fr)


async def _source(n: int, *, kind: str = "video", delay: float = 0.0):
    for i in range(n):
        if delay:
            await asyncio.sleep(delay)
        yield Frame(data=i, kind=kind, pts=float(i))


def test_publish_drains_all_frames():
    be = _WriterBackend()
    inv = MediaBinding(backends=[be], max_queue=2)
    asyncio.run(inv.publish(_ACTION, _form("rtmp://h/app/key"), _source(5)))
    assert [f.data for f in be.written] == [0, 1, 2, 3, 4]
    assert be.target == "rtmp://h/app/key"
    assert be.seen_options["track"] == "video"


def test_publish_is_lossless_when_writer_lags():
    class _Slow(_WriterBackend):
        def write(self, frames, target, *, options, stop):
            import time

            self.target = target
            for fr in frames:
                time.sleep(0.005)  # encoder slower than the producer
                self.written.append(fr)

    be = _Slow()
    inv = MediaBinding(backends=[be], max_queue=2)
    asyncio.run(inv.publish(_ACTION, _form("rtmp://h/app/key"), _source(20)))
    assert [f.data for f in be.written] == list(range(20))


def test_publish_error_propagates_redacted():
    be = _WriterBackend(raise_at=2)
    inv = MediaBinding(backends=[be], max_queue=2)
    with pytest.raises(MediaError) as ei:
        asyncio.run(inv.publish(_ACTION, _form("rtmp://h/app/key"), _source(10)))
    msg = str(ei.value)
    assert "encode boom" in msg
    assert "secretkey" not in msg  # the stream key in the echoed URL is scrubbed
    assert "rtmp://h/app/***" in msg


def test_publish_bad_track_rejected():
    inv = MediaBinding(backends=[_WriterBackend()])
    with pytest.raises(ValueError, match="track"):
        asyncio.run(inv.publish(_ACTION, _form(), _source(1), track="subtitles"))
