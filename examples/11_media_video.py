# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""11, native video: hand the model the clip itself, not stills.

10 sent a strip of frames; the model never saw true motion or heard audio. A few
providers take a real clip and sample it themselves: `host.see_video(url, ...)`
sends a native `video_url` part. Same host and wiring as 08/10; only the content
part differs.

This is a service, not a fixed camera: the clip URL is a call-time argument
(``uriVariables`` with ``"{+url}"``), the same parameterized pattern as 07.

Needs a provider with native video input (not local; Ollama is image-only)::

    OPENROUTER_API_KEY=...                          # openrouter/google/gemini-2.5-flash
    THINGCTX_VIDEO_MODEL=hosted_vllm/your-qwen3-vl  # a self-hosted vLLM

Run::  OPENROUTER_API_KEY=... python examples/11_media_video.py
"""

from __future__ import annotations

import asyncio
import os

import thingctx
from thingctx import HttpBinding
from thingctx.bindings.builtin.media import MediaBinding

# A video understanding *service*: the clip is a parameter, not a fixed source.
# `watch` takes a `url` argument (uriVariables) substituted verbatim into the form
# href via "{+url}", so one TD understands any clip, page, or stream URL.
VIDEO_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:thingctx:video:understand",
    "title": "Video understanding",
    "actions": {
        "watch": {
            "description": "Open a video clip by URL.",
            "uriVariables": {"url": {"type": "string", "description": "the clip URL"}},
            "forms": [{"href": "{+url}", "x-thingctx-media": {"container": "mp4"}}],
        }
    },
}

# The clip to understand; passed as an argument, not hardcoded in the TD.
CLIP_URL = "https://media.w3.org/2010/05/sintel/trailer.mp4"


def pick_video_model() -> str | None:
    """A model litellm can drive with a native video clip: an explicit model if
    set, else a video-capable model over OpenRouter, else None."""
    if os.environ.get("THINGCTX_VIDEO_MODEL"):
        return os.environ["THINGCTX_VIDEO_MODEL"]
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter/google/gemini-2.5-flash"
    return None


async def main() -> None:
    model = pick_video_model()
    if model is None:
        print(
            "No native-video model configured (image-only backends can't take a "
            "clip).\nSet OPENROUTER_API_KEY, or THINGCTX_VIDEO_MODEL=hosted_vllm/...\n"
            "For a fully-local frame-strip clip instead, run 10_media_clip.py."
        )
        return

    host = thingctx.from_td(VIDEO_TD, model=model, bindings=[HttpBinding(), MediaBinding()])
    # Parameterized: the clip URL is an argument, resolved through the TD's form
    # ("{+url}" is substituted verbatim); the same TD works for any video.
    url, _ = host.client.media_form("understand__watch").fill({"url": CLIP_URL})
    print(f"model: {model}\nclip: {url}\n")

    # Inline the clip as bytes; more reliable than asking the provider to
    # fetch a remote URL (see_video base64 encodes it into the request).
    import urllib.request

    with urllib.request.urlopen(url, timeout=30) as resp:
        clip = resp.read()

    answer = await host.see_video(clip, "Describe what happens in this clip.")
    print(f"VLM sees -> {answer}")


if __name__ == "__main__":
    asyncio.run(main())
