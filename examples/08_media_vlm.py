# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""08, thingctx + a vision model: 03's pattern, now with an image.

03 let an LLM drive a Thing's actions. 08 is the same wiring (`from_td(...,
model=...)`), but the Thing is a camera and the model is a VLM: `host.see(frame,
...)` instead of `host.chat(...)`. A frame is pulled off the stream with
`client.frames()` and handed to the model. Only `see` differs from `chat`.

Runs fully local with no API key, using the smallest local Ollama vision model
you have pulled (see pick_vlm_model).

Setup once if you have no vision model::  ollama pull qwen3-vl:2b
Run::  python examples/08_media_vlm.py
"""

from __future__ import annotations

import asyncio

from _pump import pick_vlm_model

import thingctx
from thingctx import HttpBinding
from thingctx.bindings.builtin.media import MediaBinding

# A camera described as a TD: its "watch" form points at a stream by reference
# (x-thingctx-media), so thingctx routes it to the media plane, not HTTP. A camera
# is a concrete device, so its source is fixed in the TD; unlike the parameterized
# "video" services in 07/11, which take any source URL as a call time argument.
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

    # Same as 03: a TD + bindings + model -> a host. Media just adds MediaBinding.
    host = thingctx.from_td(CAMERA_TD, model=model, bindings=[HttpBinding(), MediaBinding()])
    print(f"model: {model}\n")

    # Grab a frame a few seconds in (the first frames are a black fade-in).
    frame = None
    async for fr in await host.client.frames("sample__watch", track="video"):
        frame = fr
        if fr.pts and fr.pts >= 5.0:
            break
    print(f"frame: {frame.width}x{frame.height} at {frame.pts}s")

    # chat() for text, see() for an image. Same host, same tools, same loop.
    answer = await host.see(frame, "Describe this video frame in one sentence.")
    print(f"VLM sees -> {answer}")


if __name__ == "__main__":
    asyncio.run(main())
