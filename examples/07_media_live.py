# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""07, the media plane: consume a Thing's audio/video, end to end, from a TD.

A media affordance is a Thing's continuous audio/video stream. Its form points at
a stream by reference (an ``x-thingctx-media`` hint, since W3C has no RTSP/WebRTC
binding), so thingctx routes it to the MediaBinding. Media is consumed with
``client.frames()``, not ``invoke()``; the same call serves both tracks
(``track="video"|"audio"``).

Two patterns: a direct MP4 over HTTPS, and one parameterized "video pages" Thing
whose ``watch(url)`` takes any page URL yt-dlp resolves (``href`` is ``"{+url}"``,
media hint ``resolve: "page"``). One Thing covers every site; no per-site code.

Private content flows through the same auth layer as HTTP/MQTT: a ``basic`` scheme
whose secret is handed to the client (never in the TD), with cookie access as a
media-hint option. See the optional authenticated section at the end.

Run:

    python 07_media_live.py
    THINGCTX_VIDEO="https://www.twitch.tv/<channel>" python 07_media_live.py
    # optional: a private/members video
    THINGCTX_VIDEO_PRIVATE="https://www.youtube.com/watch?v=..." \\
        THINGCTX_VIDEO_COOKIES=cookies.txt python 07_media_live.py
"""

from __future__ import annotations

import asyncio
import os

from thingctx.bindings import HttpBinding
from thingctx.bindings.builtin.media import MediaBinding
from thingctx.runtime import ThingClient

SAMPLE_MP4 = "https://media.w3.org/2010/05/sintel/trailer.mp4"
# Any page yt-dlp supports; override with THINGCTX_VIDEO (a Twitch/Vimeo/... URL).
VIDEO_URL = os.environ.get("THINGCTX_VIDEO", "https://www.youtube.com/watch?v=aqz-KE-bpKQ")

# A direct stream: the href IS the media (an http(s) mp4), marked for the media
# plane so it is decoded, not fetched by HttpBinding.
SAMPLE_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:thingctx:cam:sample",
    "title": "Sample video source",
    "actions": {
        "watch": {
            "description": "Open the media stream and decode frames.",
            "forms": [{"href": SAMPLE_MP4, "x-thingctx-media": {"container": "mp4"}}],
        }
    },
}

# One Thing for every video site: the page URL is an argument, not baked in.
# ``{+url}`` substitutes the URL verbatim; ``resolve: "page"`` routes it to the
# extractor backend (yt-dlp), which resolves YouTube/Twitch/Vimeo/... alike.
PAGES_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:thingctx:video:pages",
    "title": "Video pages",
    "actions": {
        "watch": {
            "description": "Resolve a video page URL and decode frames.",
            "uriVariables": {"url": {"type": "string", "description": "the page URL"}},
            "forms": [{"href": "{+url}", "x-thingctx-media": {"resolve": "page"}}],
        }
    },
}


async def _take(
    client: ThingClient, name: str, args: dict | None = None, *, track: str, n: int
) -> list:
    out = []
    async for frame in await client.frames(name, args, track=track):
        out.append(frame)
        if len(out) >= n:
            break
    return out


async def main() -> None:
    client = ThingClient(tds=[SAMPLE_TD, PAGES_TD], bindings=[HttpBinding(), MediaBinding()])

    print("invoke tools (list_actions):", [t["function"]["name"] for t in client.list_actions()])
    print("media affordances (list_media):", client.list_media())
    sample = "sample__watch"
    pages = "pages__watch"

    # Media is a stream, not an action: invoke() refuses it and points to frames().
    print("invoke() on media ->", (await client.invoke(sample)).get("error"))

    print(f"\n[{sample}] video track:")
    video = await _take(client, sample, track="video", n=5)
    for i, fr in enumerate(video):
        h, w = fr.data.shape[:2]
        print(f"  frame {i}: {w}x{h} {fr.encoding} pts={fr.pts}")
    assert video and video[0].kind == "video"

    print(f"\n[{sample}] audio track:")
    audio = await _take(client, sample, track="audio", n=3)
    for i, fr in enumerate(audio):
        print(
            f"  block {i}: {fr.encoding} sample_rate={fr.sample_rate} "
            f"channels={fr.channels} shape={getattr(fr.data, 'shape', None)}"
        )
    assert audio and audio[0].kind == "audio"

    # Same surface, parameterized source: the page URL is passed at call time and
    # resolved by yt-dlp behind the same frames() call. Best-effort (yt-dlp can
    # be rate-limited). Swap in a Twitch/Vimeo URL and nothing else changes.
    print(f"\n[{pages}] video track (url={VIDEO_URL}):")
    try:
        frames = await _take(client, pages, {"url": VIDEO_URL}, track="video", n=3)
        for i, fr in enumerate(frames):
            h, w = fr.data.shape[:2]
            print(f"  frame {i}: {w}x{h} {fr.encoding} pts={fr.pts}")
        assert frames and frames[0].kind == "video"
    except Exception as exc:  # network/yt-dlp failure shouldn't sink the demo
        print(f"  skipped (could not resolve/decode): {type(exc).__name__}: {exc}")

    await client.aclose()

    await _run_authenticated()
    print("\nOK: thingctx drove a direct stream and a parameterized page source.")


async def _run_authenticated() -> None:
    """Optional: a private or members video. The secret never lives in the TD;
    an account login is supplied to the client as a ``basic`` credential, and/or
    a cookie file is named on the form's media hint. Set THINGCTX_VIDEO_PRIVATE
    plus THINGCTX_VIDEO_USER/THINGCTX_VIDEO_PASS and/or THINGCTX_VIDEO_COOKIES."""
    url = os.environ.get("THINGCTX_VIDEO_PRIVATE")
    if not url:
        print(
            "\n[private] set THINGCTX_VIDEO_PRIVATE + THINGCTX_VIDEO_USER/PASS "
            "(login) and/or THINGCTX_VIDEO_COOKIES (cookie file) to try a private "
            "source. Skipping."
        )
        return

    hint = {"resolve": "page"}
    cookies = os.environ.get("THINGCTX_VIDEO_COOKIES")
    if cookies:
        hint["cookiefile"] = cookies  # extractor option, not a credential
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thingctx:video:private",
        "title": "Private video",
        "actions": {
            "watch": {
                "uriVariables": {"url": {"type": "string"}},
                "forms": [{"href": "{+url}", "x-thingctx-media": hint}],
            }
        },
    }
    # Declare a `basic` login only when one is supplied; cookies alone need no
    # security scheme. (A declared scheme with no secret resolves to nothing.)
    creds: dict = {}
    user, pw = os.environ.get("THINGCTX_VIDEO_USER"), os.environ.get("THINGCTX_VIDEO_PASS")
    if user and pw:
        td["securityDefinitions"] = {"login": {"scheme": "basic"}}
        td["security"] = "login"
        creds = {"private": (user, pw)}

    client = ThingClient(tds=[td], bindings=[HttpBinding(), MediaBinding(credentials=creds)])
    print(f"\n[private__watch] authenticated source (url={url}):")
    try:
        frames = await _take(client, "private__watch", {"url": url}, track="video", n=3)
        for i, fr in enumerate(frames):
            h, w = fr.data.shape[:2]
            print(f"  frame {i}: {w}x{h} {fr.encoding} pts={fr.pts}")
    except Exception as exc:  # network/login/cookie failure shouldn't sink the demo
        print(f"  skipped ({type(exc).__name__}: {exc})")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
