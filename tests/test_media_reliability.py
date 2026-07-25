# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Media reliability: transient classification, retry/resume on read, the
normalized TransportError, and muxed A/V publish."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

from thingctx.bindings.builtin.media import Frame, MediaBinding, MediaError
from thingctx.bindings.builtin.media.binding import _is_transient_media, _media_status
from thingctx.reliability import RetryPolicy, TransportError
from thingctx.thing import WoTAction, WoTForm

_FAST = RetryPolicy(retries=2, backoff=0.0, jitter=0.0)


class HTTPForbiddenError(Exception):  # name matches PyAV's 403 error class
    pass


class HTTPNotFoundError(Exception):  # name matches PyAV's 404 error class
    pass


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


async def _collect(inv, form, *, track="video"):
    out = []
    async for fr in inv.frames(_ACTION, form, {}, track=track):
        out.append(fr)
    return out


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def test_classifier_transient_vs_fatal():
    assert _is_transient_media(HTTPForbiddenError("403"))
    assert _is_transient_media(TimeoutError())
    assert _is_transient_media(ConnectionResetError())
    assert _is_transient_media(Exception("Server returned 403 Forbidden"))
    assert not _is_transient_media(HTTPNotFoundError("404"))
    assert not _is_transient_media(RuntimeError("decode boom"))


def test_media_status_mapping():
    assert _media_status(HTTPForbiddenError()) == 403
    assert _media_status(HTTPNotFoundError()) == 404
    assert _media_status(TimeoutError()) is None


# --------------------------------------------------------------------------- #
# retry / resume on read
# --------------------------------------------------------------------------- #


class _FlakyBackend:
    """Re-openable source. ``fail_calls`` read-attempts each raise after
    yielding ``yield_before`` frames; later attempts succeed. Records every
    options dict it was opened with (to assert re-resolve / seek)."""

    def __init__(self, *, fail_calls: int, total: int, yield_before: int = 0, exc=None):
        self.fail_calls = fail_calls
        self.total = total
        self.yield_before = yield_before
        self.exc = exc or TimeoutError("blip")
        self.calls = 0
        self.opened: list[dict] = []

    def can_open(self, url: str, hint: dict) -> bool:
        return True

    def read(self, url: str, *, options: dict, stop: threading.Event) -> Iterator[Frame]:
        self.opened.append(dict(options))
        call = self.calls
        self.calls += 1
        if call < self.fail_calls:
            for i in range(self.yield_before):
                yield Frame(data=i, kind="video", pts=float(i))
            raise self.exc
        for i in range(self.total):
            yield Frame(data=i, kind="video", pts=float(i))

    def write(self, frames, target, *, options, stop):
        raise NotImplementedError


def test_gap_continue_reopens_and_resumes():
    be = _FlakyBackend(fail_calls=1, total=3)
    inv = MediaBinding(backends=[be], backpressure="all", retry=_FAST)
    frames = asyncio.run(_collect(inv, _form()))
    assert [f.data for f in frames] == [0, 1, 2]  # resumed after the first failure
    assert be.calls == 2  # re-opened once


def test_retry_exhausted_raises_transport_error():
    be = _FlakyBackend(fail_calls=99, total=3)  # never recovers
    inv = MediaBinding(backends=[be], backpressure="all", retry=RetryPolicy(retries=1, backoff=0.0))
    with pytest.raises(TransportError) as ei:
        asyncio.run(_collect(inv, _form()))
    assert ei.value.attempts == 2  # first try + one retry


def test_fatal_error_is_media_error_not_retried():
    be = _FlakyBackend(fail_calls=99, total=3, exc=RuntimeError("decode boom"))
    inv = MediaBinding(backends=[be], backpressure="all", retry=_FAST)
    with pytest.raises(MediaError, match="decode boom"):
        asyncio.run(_collect(inv, _form()))
    assert be.calls == 1  # not retried


def test_progress_resets_retry_budget():
    # Each of the first 3 opens yields one frame then fails; the 4th completes.
    # With retries=1 this only survives if progress resets the budget.
    be = _FlakyBackend(fail_calls=3, total=2, yield_before=1)
    inv = MediaBinding(backends=[be], backpressure="all", retry=RetryPolicy(retries=1, backoff=0.0))
    frames = asyncio.run(_collect(inv, _form()))
    assert be.calls == 4
    assert len(frames) == 3 + 2  # one per failed open, then the full final read


def test_seek_resume_passes_last_pts_on_reopen():
    be = _FlakyBackend(fail_calls=1, total=2, yield_before=2)
    inv = MediaBinding(backends=[be], backpressure="all", retry=_FAST, resume="seek")
    asyncio.run(_collect(inv, _form()))
    assert be.calls == 2
    assert "_resume_pts" not in be.opened[0]
    assert be.opened[1]["_resume_pts"] == 1.0  # last pts seen before the failure


def test_resume_rejects_bad_value():
    with pytest.raises(ValueError, match="resume"):
        MediaBinding(backends=[_FlakyBackend(fail_calls=0, total=1)], resume="rewind")


# --------------------------------------------------------------------------- #
# muxed A/V publish (real codecs)
# --------------------------------------------------------------------------- #

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")

from thingctx.bindings.builtin.media.backends import PyAVBackend  # noqa: E402


def _rgb(i: int, w: int = 160, h: int = 120):
    a = np.empty((h, w, 3), dtype=np.uint8)
    a[:, :, 0] = (i * 8) % 256
    a[:, :, 1] = (i * 4) % 256
    a[:, :, 2] = 32
    return a


async def _video(n: int, fps: int = 25):
    for i in range(n):
        yield Frame(data=_rgb(i), kind="video", pts=i / fps, encoding="rgb24")


async def _audio(n: int, sr: int = 48000, block: int = 1920):
    # s16 mono blocks; a quiet sine so the encoder has real samples.
    for i in range(n):
        t = (np.arange(block) + i * block) / sr
        samples = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).reshape(1, block)
        yield Frame(
            data=samples,
            kind="audio",
            pts=i * block / sr,
            sample_rate=sr,
            channels=1,
            encoding="pcm",
            meta={"format": "s16", "layout": "mono"},
        )


def test_publish_muxed_av_has_both_streams(tmp_path):
    out = tmp_path / "av.mp4"
    form = WoTForm(href=str(out), raw={"x-thingctx-media": {"fps": 25}})
    inv = MediaBinding(backends=[PyAVBackend()], backpressure="all", max_queue=8)

    asyncio.run(inv.publish(_ACTION, form, _video(25), audio=_audio(25)))

    assert out.exists() and out.stat().st_size > 0
    container = av.open(str(out))
    try:
        kinds = {s.type for s in container.streams}
        assert "video" in kinds and "audio" in kinds
        v = sum(1 for _ in container.decode(video=0))
        assert v > 0
    finally:
        container.close()


def test_write_av_video_only_mp4_muxes(tmp_path):
    # Regression: 480x360 at fps=24 over enough frames triggered an mp4 mux
    # EINVAL when the frame time base diverged from the stream's. ~60 frames
    # reproduces it; it must write cleanly and decode back.
    import threading

    out = tmp_path / "o.mp4"
    frames = [Frame(data=_rgb(i, 480, 360), kind="video", encoding="rgb24") for i in range(60)]
    PyAVBackend().write_av(
        iter(frames), None, str(out), options={"fps": 24}, stop=threading.Event()
    )
    assert out.exists() and out.stat().st_size > 0
    container = av.open(str(out))
    try:
        assert sum(1 for _ in container.decode(video=0)) == 60
    finally:
        container.close()


def test_publish_audio_only_track(tmp_path):
    out = tmp_path / "audio.m4a"
    form = WoTForm(href=str(out), raw={})
    inv = MediaBinding(backends=[PyAVBackend()], backpressure="all", max_queue=8)

    asyncio.run(inv.publish(_ACTION, form, _audio(20), track="audio"))

    assert out.exists() and out.stat().st_size > 0
    container = av.open(str(out))
    try:
        assert any(s.type == "audio" for s in container.streams)
    finally:
        container.close()


def test_publish_audio_needs_capable_backend():
    class _NoAv:
        def can_open(self, url, hint):
            return True

        def read(self, url, *, options, stop):
            return iter(())

        def write(self, frames, target, *, options, stop):
            pass

    inv = MediaBinding(backends=[_NoAv()])
    with pytest.raises(TypeError, match="write_av"):
        asyncio.run(inv.publish(_ACTION, _form("rtmp://h/app/k"), _video(2), audio=_audio(2)))
