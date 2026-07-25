# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""10, a vision model over a clip: 08, but several frames over time.

08 showed one frame; a single still can't show motion. Image-only models (and
MCP) can't take a video container, so the portable clip is a handful of frames
sampled over time and sent as a sequence of stills. `see()` takes the list and
reads it as motion; no new model capability required.

Runs fully local with no API key (see pick_vlm_model). For a native video clip
with audio, see 11_media_video.py.

Setup once if you have no vision model::  ollama pull qwen3-vl:2b
Run::  python examples/10_media_clip.py
"""

from __future__ import annotations

import asyncio

from _pump import pick_vlm_model

import thingctx
from thingctx import HttpBinding
from thingctx.bindings.builtin.media import MediaBinding, sample_frames

# A concrete device: a camera has one stream, so its source is fixed in the TD
# (unlike the parameterized "video" services in 07/11, which take a source URL arg).
CAMERA_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:thingctx:cam:sample",
    "title": "Sample camera",
    "actions": {
        "watch": {
            "description": "Open the camera stream and decode frames.",
            "forms": [
                {
                    "href": "https://media.w3.org/2010/05/sintel/trailer.mp4",
                    "x-thingctx-media": {"container": "mp4"},
                }
            ],
        }
    },
}


async def main() -> None:
    model = pick_vlm_model()
    if model is None:
        print("No vision model reachable. Pull a small one: ollama pull qwen3-vl:2b")
        return

    host = thingctx.from_td(CAMERA_TD, model=model, bindings=[HttpBinding(), MediaBinding()])
    print(f"model: {model}\n")

    # Sample a few frames spaced ~2s apart; a clip the model reads as stills.
    clip = await sample_frames(
        await host.client.frames("sample__watch", track="video"), count=5, every=2.0
    )
    spans = ", ".join(f"{f.pts:.1f}s" for f in clip if f.pts is not None)
    print(f"clip: {len(clip)} frames ({clip[0].width}x{clip[0].height}) at {spans}")

    answer = await host.see(clip, "These frames are a short clip over time. Describe what happens.")
    print(f"\nVLM sees -> {answer}")


if __name__ == "__main__":
    asyncio.run(main())
