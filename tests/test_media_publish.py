# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Round-trip test for the media publish path: encode synthetic frames to a
file with the real PyAV backend, then decode them back. Skipped without the
``av``/``numpy`` codecs installed."""

from __future__ import annotations

import asyncio

import pytest

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")

from thingctx.bindings.builtin.media import Frame, MediaBinding  # noqa: E402
from thingctx.bindings.builtin.media.backends import PyAVBackend  # noqa: E402
from thingctx.thing import WoTAction, WoTForm  # noqa: E402

_ACTION = WoTAction(
    name="broadcast",
    thing_id="urn:thingctx:studio:test",
    description="",
    input_schema={},
    output_schema=None,
    idempotent=False,
    forms=(),
)


def _rgb(i: int, w: int = 320, h: int = 240) -> np.ndarray:
    """A solid color that shifts per frame; distinct, easy to encode."""
    frame = np.empty((h, w, 3), dtype=np.uint8)
    frame[:, :, 0] = (i * 8) % 256
    frame[:, :, 1] = (i * 4) % 256
    frame[:, :, 2] = 64
    return frame


async def _source(n: int):
    for i in range(n):
        yield Frame(data=_rgb(i), kind="video", encoding="rgb24")


def test_publish_then_consume_file_roundtrip(tmp_path):
    out = tmp_path / "clip.mp4"
    form = WoTForm(href=str(out), raw={"x-thingctx-media": {"fps": 25}})
    inv = MediaBinding(backends=[PyAVBackend()], backpressure="all", max_queue=4)

    asyncio.run(inv.publish(_ACTION, form, _source(30)))

    assert out.exists() and out.stat().st_size > 0

    async def _read_back():
        got = []
        async for fr in inv.frames(_ACTION, form):
            got.append(fr)
        return got

    frames = asyncio.run(_read_back())
    # Every encoded frame decodes back (h264 is lossy, so pixels differ, but the
    # count and geometry survive the round trip).
    assert len(frames) == 30
    assert frames[0].width == 320
    assert frames[0].height == 240


async def _source_30fps_with_pts(n: int):
    """Frames as the read path produces them: each carries its source time and
    the source frame rate (here 30 fps)."""
    for i in range(n):
        yield Frame(data=_rgb(i), kind="video", pts=i / 30.0, encoding="rgb24", meta={"rate": 30.0})


def test_publish_preserves_source_frame_rate_over_hint(tmp_path):
    # A 30 fps source must round-trip to a 30 fps file of the same duration even
    # when the form hint names a different fps. Re-clocking the frames to the
    # hinted 25 fps would stretch the video 30/25 = 1.2x and drift it out of
    # sync with its audio.
    out = tmp_path / "clip.mp4"
    form = WoTForm(href=str(out), raw={"x-thingctx-media": {"fps": 25}})
    inv = MediaBinding(backends=[PyAVBackend()], backpressure="all", max_queue=4)

    n = 60
    asyncio.run(inv.publish(_ACTION, form, _source_30fps_with_pts(n)))

    container = av.open(str(out))
    try:
        vstream = container.streams.video[0]
        rate = float(vstream.average_rate)
        duration = float(vstream.duration * vstream.time_base)
    finally:
        container.close()

    # Source rate (30) wins over the hint (25): no 1.2x stretch.
    assert rate == pytest.approx(30.0, abs=0.5)
    assert duration == pytest.approx(n / 30.0, abs=0.1)
