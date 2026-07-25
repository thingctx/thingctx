# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Encode a decoded video :class:`Frame` to a portable image.

Used to hand a frame to a vision model (VLM) as an ``image_url`` data URL.
Pillow is imported lazily so the rest of the media plane has no image-encoding
dependency.
"""

from __future__ import annotations

import base64
import io

from thingctx.bindings.builtin.media.frame import Frame


def frame_to_jpeg(frame: Frame, *, quality: int = 85) -> bytes:
    """Encode an ``rgb24`` video frame to JPEG bytes."""
    if frame.kind != "video":
        raise ValueError("only video frames can be encoded as images")
    # optional dep, kept local so the core imports without the extra
    from PIL import Image  # noqa: PLC0415

    img = Image.fromarray(frame.data)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def frame_to_data_url(frame: Frame, *, quality: int = 85) -> str:
    """Encode a video frame to a ``data:image/jpeg;base64,...`` URL."""
    payload = base64.b64encode(frame_to_jpeg(frame, quality=quality)).decode("ascii")
    return "data:image/jpeg;base64," + payload
