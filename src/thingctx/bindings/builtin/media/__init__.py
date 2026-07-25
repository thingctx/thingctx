# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Media binding: the continuous-binary (audio/video) plane.

Heavier than the single-file bindings, so it is a package: the binding
(:class:`MediaBinding`), its data type (:class:`Frame`), the engine protocol
(:class:`MediaBackend`), and the blocking backends. Backends are imported lazily
by the binding so the optional media dependencies are never required to import.
"""

from __future__ import annotations

from thingctx.bindings.builtin.media.binding import (
    MEDIA_SCHEMES,
    MediaBinding,
    MediaError,
    is_media_form,
)
from thingctx.bindings.builtin.media.frame import Frame, MediaBackend
from thingctx.bindings.builtin.media.sample import sample_frames

__all__ = [
    "MEDIA_SCHEMES",
    "Frame",
    "MediaBackend",
    "MediaBinding",
    "MediaError",
    "is_media_form",
    "sample_frames",
]
