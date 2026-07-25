# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""GAP 4 fix: a media open always carries a timeout, so a source that accepts the
connection but never sends a byte cannot wedge the worker thread with no bound.

The binding's timeout is the default; a caller-declared timeout still wins. All
offline (a fake backend records the options it is handed); no network, no av."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator

import pytest

np = pytest.importorskip("numpy")

from thingctx.bindings.builtin.media import Frame, MediaBinding  # noqa: E402
from thingctx.runtime import ThingClient  # noqa: E402


class _RecordingBackend:
    """Records the options each entry point hands it, so a test can assert the
    timeout that reached the open."""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def can_open(self, url: str, hint: dict) -> bool:
        return True

    def read(self, url: str, *, options: dict, stop: threading.Event) -> Iterator[Frame]:
        self.seen.append(dict(options))
        yield Frame(data=0, kind=options.get("track", "video"), pts=0.0)

    def write(
        self, frames: Iterator[Frame], target: str, *, options: dict, stop: threading.Event
    ) -> None:
        self.seen.append(dict(options))
        for _ in frames:  # drain so the producer is not wedged
            pass

    def copy(self, url: str, target: str, *, options: dict, stop: threading.Event) -> None:
        self.seen.append(dict(options))


def _media_td(href: str = "rtsp://cam.example/stream") -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:cam1",
        "title": "cam",
        "actions": {"watch": {"forms": [{"href": href}]}},
    }


def _client(backend: _RecordingBackend, **binding_kw) -> ThingClient:
    return ThingClient(
        tds=[_media_td()],
        bindings=[MediaBinding(backends=[backend], **binding_kw)],
    )


async def _one_video_frame() -> AsyncIterator[Frame]:
    yield Frame(data=np.zeros((2, 2, 3), dtype=np.uint8), kind="video", pts=0.0)


async def test_frames_open_carries_a_default_timeout():
    # GAP 4 (invariant NET-16): with no caller timeout, frames() still hands the
    # backend a bounded timeout (the binding default), so a stalled source cannot
    # block forever.
    backend = _RecordingBackend()
    client = _client(backend, timeout=17.0)
    async for _ in await client.frames("cam1__watch"):
        break
    assert backend.seen, "backend was never opened"
    assert backend.seen[0]["timeout"] == 17.0


async def test_caller_timeout_wins_over_the_default():
    # GAP 4 (invariant NET-16): a caller-declared timeout is not overridden by the
    # default, so an operator can still tune it per call.
    backend = _RecordingBackend()
    client = _client(backend, timeout=17.0)
    async for _ in await client.frames("cam1__watch", {"timeout": 3.0}):
        break
    assert backend.seen[0]["timeout"] == 3.0


async def test_save_copy_open_carries_a_default_timeout(tmp_path):
    # GAP 4 (invariant NET-16): the remux (save/copy) open is bounded too.
    backend = _RecordingBackend()
    client = _client(backend, timeout=11.0)
    await client.save("cam1__watch", str(tmp_path / "out.mp4"))
    assert backend.seen[0]["timeout"] == 11.0
