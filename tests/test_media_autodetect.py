"""Auto-detection (direct stream vs page) and source validation in the media
plane, so a caller needs one "save this URL" affordance and no routing or URL
hygiene of its own. All offline: routing and validation are pure decisions, no
network and no codecs required."""

from __future__ import annotations

import asyncio

import pytest

from thingctx.bindings.builtin.media.backends import (
    ExtractorBackend,
    PyAVBackend,
    _looks_direct,
)
from thingctx.bindings.builtin.media.binding import MediaBinding, MediaError, _clean_source
from thingctx.thing import WoTAction, WoTForm

_ACTION = WoTAction(
    name="ingest",
    thing_id="urn:thingctx:studio:test",
    description="",
    input_schema={},
    output_schema=None,
    idempotent=False,
    forms=(),
)


@pytest.mark.parametrize(
    "url,direct",
    [
        ("https://site/clip.mp4", True),
        ("https://site/a/b/play.m3u8", True),
        ("https://site/track.m4a?sig=x", True),
        ("https://site/manifest.mpd", True),
        ("https://youtube.com/watch?v=abc", False),
        ("https://youtu.be/abc", False),
        ("https://vimeo.com/12345", False),
        ("rtsp://cam/stream", True),
        ("srt://host:9000", True),
        ("file:///tmp/x.mp4", True),
        ("/tmp/x.mp4", True),
    ],
)
def test_looks_direct(url, direct):
    assert _looks_direct(url) is direct


def test_auto_routes_page_to_extractor_and_direct_to_pyav():
    ext, pyav = ExtractorBackend(), PyAVBackend()
    hint = {"resolve": "auto"}

    # A page: the extractor claims it, PyAV declines.
    assert ext.can_open("https://youtube.com/watch?v=x", hint) is True
    assert pyav.can_open("https://youtube.com/watch?v=x", hint) is False

    # A direct http(s) stream: PyAV claims it, the extractor declines.
    assert ext.can_open("https://site/clip.mp4", hint) is False
    assert pyav.can_open("https://site/clip.mp4", hint) is True

    # A non-http scheme is always direct.
    assert ext.can_open("rtsp://cam/s", hint) is False
    assert pyav.can_open("rtsp://cam/s", hint) is True


def test_explicit_modes_force_routing():
    ext, pyav = ExtractorBackend(), PyAVBackend()
    # Force page on a URL that looks direct.
    assert ext.can_open("https://site/clip.mp4", {"resolve": "page"}) is True
    assert pyav.can_open("https://site/clip.mp4", {"resolve": "page"}) is False
    # Force direct on a URL that looks like a page.
    assert ext.can_open("https://youtube.com/watch?v=x", {"resolve": "direct"}) is False
    assert pyav.can_open("https://youtube.com/watch?v=x", {"resolve": "direct"}) is True


def test_unset_mode_preserves_legacy_direct():
    # No resolve hint: an http(s) URL opens directly (legacy), a page resolves
    # only when explicitly hinted; auto is opt-in, so nothing existing reroutes.
    ext, pyav = ExtractorBackend(), PyAVBackend()
    assert ext.can_open("https://cam/stream", {}) is False
    assert pyav.can_open("https://cam/stream", {}) is True
    assert ext.can_open("https://cam/stream", {"source": "youtube"}) is True
    assert pyav.can_open("https://cam/stream", {"source": "youtube"}) is False


@pytest.mark.parametrize("bad", ["", "   ", "\n", "\t", "https://", "rtsp://", "line1\nline2"])
def test_clean_source_rejects_malformed(bad):
    with pytest.raises(MediaError):
        _clean_source(bad)


@pytest.mark.parametrize(
    "ok",
    [
        "https://a/b.mp4",
        "  https://a/b.mp4  ",
        "rtsp://cam/s",
        "/tmp/my video.mp4",  # a local path may contain spaces
        "file:///x.mp4",
    ],
)
def test_clean_source_accepts_and_strips(ok):
    assert _clean_source(ok) == ok.strip()


def test_save_rejects_empty_source_with_clear_error():
    inv = MediaBinding(backends=[PyAVBackend()])
    form = WoTForm(href="", raw={"x-thingctx-media": {}})
    with pytest.raises(MediaError):
        asyncio.run(inv.save(_ACTION, form, "/tmp/out.mp4"))
