# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""12, the publish (produce) path: thingctx pushes frames out to a media target.

The reverse of consuming a camera. A producer yields frames on the event loop;
thingctx encodes and muxes them off it and pushes them to the target named by a
TD form (a file, an ingest URL, or a live endpoint). The target is just a form
href, so one `client.publish(...)` call drives any of them.

Part A always runs (encode a clip to a file, then read it back to prove the round
trip). Part B runs if `mediamtx` is on PATH (publish a live RTSP stream and
consume it back). An ingest stream key is passed as a uriVariable at call time,
so it stays out of the stored TD and is scrubbed from surfaced errors.

Needs PyAV (`pip install 'thingctx[media]'`), plus `mediamtx` for Part B.
Run::  python examples/12_media_publish.py
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

from thingctx.bindings.builtin.media import Frame, MediaBinding
from thingctx.runtime import ThingClient

WIDTH, HEIGHT, FPS = 320, 240, 25
RTSP_URL = "rtsp://127.0.0.1:8554/live"


def _frame(i: int) -> np.ndarray:
    """A moving diagonal gradient; cheap to generate, obvious when decoded."""
    x = np.linspace(0, 255, WIDTH, dtype=np.uint8)
    row = np.roll(x, (i * 6) % WIDTH)
    img = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint8)
    img[:, :, 0] = row
    img[:, :, 1] = row[::-1]
    img[:, :, 2] = (i * 3) % 256
    return img


async def _source(n: int, *, realtime: bool = False):
    """Yield n synthetic frames. ``realtime`` paces to FPS so a live consumer
    has time to attach (the publish path is lossless, so the source sets the
    rate)."""
    for i in range(n):
        if realtime:
            await asyncio.sleep(1 / FPS)
        yield Frame(data=_frame(i), kind="video", encoding="rgb24")


def _publish_td(href: str) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thingctx:studio",
        "title": "Studio broadcaster",
        "security": "nosec_sc",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "actions": {
            "broadcast": {
                "description": "Encode and push frames to the target.",
                "forms": [{"href": href, "x-thingctx-media": {"fps": FPS}}],
            }
        },
    }


def _view_td(href: str) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thingctx:viewer",
        "title": "Stream viewer",
        "security": "nosec_sc",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "actions": {
            "watch": {
                "description": "Open the stream and decode frames.",
                "forms": [{"href": href, "x-thingctx-media": {"container": "rtsp"}}],
            }
        },
    }


async def _part_a_file() -> None:
    out = Path(tempfile.mkdtemp()) / "clip.mp4"
    client = ThingClient(tds=[_publish_td(str(out)), _view_td(str(out))], bindings=[MediaBinding()])
    try:
        print(f"A. publishing 60 frames to {out.name}")
        await client.publish("studio__broadcast", _source(60))
        size = out.stat().st_size
        print(f"   wrote {size} bytes; reading it back ...")

        got = []
        async for fr in await client.frames("viewer__watch"):
            got.append(fr)
        print(f"   decoded {len(got)} frames, {got[0].width}x{got[0].height} {got[0].encoding}")
        print("   OK: produce, file, consume round trip\n")
    finally:
        await client.aclose()


def _wait_port(host: str, port: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


async def _part_b_rtsp() -> None:
    if not shutil.which("mediamtx"):
        print("B. (skipped) install mediamtx (brew install mediamtx) for the RTSP round trip")
        return

    cfg = Path(tempfile.mkdtemp()) / "mediamtx.yml"
    cfg.write_text("paths:\n  all_others:\n")
    server = subprocess.Popen(
        ["mediamtx", str(cfg)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    client = ThingClient(tds=[_publish_td(RTSP_URL), _view_td(RTSP_URL)], bindings=[MediaBinding()])
    try:
        if not _wait_port("127.0.0.1", 8554):
            print("B. RTSP server did not come up on :8554")
            return
        print(f"B. publishing a live stream to {RTSP_URL}")
        pub = asyncio.create_task(client.publish("studio__broadcast", _source(250, realtime=True)))
        await asyncio.sleep(2.0)  # let the stream register before reading

        got = []

        async def pull() -> None:
            async for fr in await client.frames("viewer__watch"):
                got.append(fr)
                if len(got) >= 10:
                    break

        try:
            await asyncio.wait_for(pull(), timeout=20.0)
            print(f"   consumed {len(got)} live frames back, {got[0].width}x{got[0].height}")
            print("   OK: produce, RTSP, consume, live\n")
        finally:
            pub.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pub
    finally:
        await client.aclose()
        server.terminate()
        with contextlib.suppress(Exception):
            server.wait(timeout=5)


async def main() -> None:
    try:
        import av  # noqa: F401
    except ImportError:
        print("Needs PyAV: pip install 'thingctx[media]'")
        return
    await _part_a_file()
    await _part_b_rtsp()


if __name__ == "__main__":
    asyncio.run(main())
