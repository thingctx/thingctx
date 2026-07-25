# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Sample a few frames from a media stream.

Image-only models (and MCP, which has no video content type) can't take a clip
directly. A handful of frames spaced over time is the portable stand-in: the
model sees motion across stills, on any vision-capable backend.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from thingctx.bindings.builtin.media.frame import Frame


async def sample_frames(
    frames: AsyncIterator[Frame], *, count: int = 8, every: float = 1.0
) -> list[Frame]:
    """Collect up to ``count`` frames spaced about ``every`` seconds apart by
    presentation timestamp. Frames without a pts are taken consecutively.

        clip = await sample_frames(await client.frames("cam__watch"), count=6)
    """
    if count < 1:
        raise ValueError("count must be >= 1")
    out: list[Frame] = []
    next_at: float | None = None
    async for fr in frames:
        if fr.pts is None:
            out.append(fr)
        elif next_at is None or fr.pts >= next_at:
            out.append(fr)
            next_at = fr.pts + every
        if len(out) >= count:
            break
    return out
