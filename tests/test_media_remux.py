# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Stream-copy (remux) ingest: ``save`` writes the source's compressed packets
to a file without decoding, so the output is bit exact (same codecs, exact frame
rate and frame count, A/V in sync) with no re-encode.

The real-codec cases need ffmpeg (to generate inputs) and av/numpy; they skip
otherwise. The wiring case is offline (fake backend)."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading

import pytest

from thingctx.thing import WoTAction, WoTForm

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

_ACTION = WoTAction(
    name="ingest",
    thing_id="urn:thingctx:studio:test",
    description="",
    input_schema={},
    output_schema=None,
    idempotent=False,
    forms=(),
)


def _probe(path) -> dict:
    out = subprocess.check_output(
        [_FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    )
    return json.loads(out)


def _stream(info: dict, kind: str) -> dict | None:
    return next((s for s in info.get("streams", []) if s.get("codec_type") == kind), None)


# ---- real-codec remux (ffmpeg + av) ----

_real = pytest.mark.skipif(
    not (_FFMPEG and _FFPROBE), reason="ffmpeg/ffprobe required for the remux round trip"
)


def _gen(path, *, fps="30", dur=2.0, audio_rate=None, vcodec="libx264", acodec="aac") -> None:
    args = [_FFMPEG, "-y", "-v", "error"]
    args += ["-f", "lavfi", "-i", f"testsrc=size=320x240:rate={fps}:duration={dur}"]
    if audio_rate:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={audio_rate}:duration={dur}"]
    args += ["-c:v", vcodec, "-pix_fmt", "yuv420p"]
    if audio_rate:
        args += ["-c:a", acodec]
    args += [str(path)]
    subprocess.check_call(args)


def _save(src, target, *, track=None):
    pytest.importorskip("av")
    pytest.importorskip("numpy")
    from thingctx.bindings.builtin.media import MediaBinding
    from thingctx.bindings.builtin.media.backends import PyAVBackend

    inv = MediaBinding(backends=[PyAVBackend()])
    form = WoTForm(href=str(src), raw={"x-thingctx-media": {}})
    asyncio.run(inv.save(_ACTION, form, str(target), track=track))


@_real
def test_save_is_bit_exact_no_reencode(tmp_path):
    pytest.importorskip("av")
    src = tmp_path / "in.mp4"
    out = tmp_path / "out.mp4"
    _gen(src, fps="30", dur=2.0, audio_rate=48000)
    si = _probe(src)
    sv, sa = _stream(si, "video"), _stream(si, "audio")

    _save(src, out)

    oi = _probe(out)
    ov, oa = _stream(oi, "video"), _stream(oi, "audio")
    assert ov is not None and oa is not None
    # Same codecs (no re-encode), exact frame rate and exact frame count.
    assert ov["codec_name"] == sv["codec_name"]
    assert oa["codec_name"] == sa["codec_name"]
    assert ov["r_frame_rate"] == sv["r_frame_rate"]
    assert int(ov["nb_frames"]) == int(sv["nb_frames"])
    # A/V stay in sync: stream durations agree.
    assert float(ov["duration"]) == pytest.approx(float(sv["duration"]), abs=0.05)
    assert float(oa["duration"]) == pytest.approx(float(ov["duration"]), abs=0.1)


@_real
@pytest.mark.parametrize("fps", ["24", "60"])
def test_save_preserves_exact_fps(tmp_path, fps):
    pytest.importorskip("av")
    src = tmp_path / f"in_{fps}.mp4"
    out = tmp_path / f"out_{fps}.mp4"
    _gen(src, fps=fps, dur=2.0)
    sv = _stream(_probe(src), "video")

    _save(src, out)

    ov = _stream(_probe(out), "video")
    assert ov["r_frame_rate"] == sv["r_frame_rate"]  # exact, not rounded
    assert int(ov["nb_frames"]) == int(sv["nb_frames"])


@_real
def test_save_track_video_only(tmp_path):
    pytest.importorskip("av")
    src = tmp_path / "in.mp4"
    out = tmp_path / "out.mp4"
    _gen(src, fps="30", dur=1.0, audio_rate=48000)

    _save(src, out, track="video")

    oi = _probe(out)
    assert _stream(oi, "video") is not None
    assert _stream(oi, "audio") is None


@_real
def test_save_keeps_source_codecs_in_matching_container(tmp_path):
    # A webm source (vp9/opus) copied to .webm stays vp9/opus: the container
    # follows the source and the codecs are untouched.
    pytest.importorskip("av")
    src = tmp_path / "in.webm"
    out = tmp_path / "out.webm"
    _gen(src, fps="30", dur=1.0, audio_rate=48000, vcodec="libvpx-vp9", acodec="libopus")
    sv = _stream(_probe(src), "video")

    _save(src, out)

    ov = _stream(_probe(out), "video")
    assert ov["codec_name"] == sv["codec_name"] == "vp9"
    assert _stream(_probe(out), "audio")["codec_name"] == "opus"


# ---- offline wiring (fake backend, no codecs) ----


class _FakeCopyBackend:
    def __init__(self):
        self.seen: dict | None = None

    def can_open(self, url: str, hint: dict) -> bool:
        return True

    def copy(self, url: str, target: str, *, options: dict, stop: threading.Event) -> None:
        self.seen = {"url": url, "target": target, "options": dict(options)}
        with open(target, "w") as fh:  # prove the worker ran and wrote
            fh.write("copied")


def _client_with(backend) -> object:
    from thingctx.bindings import HttpBinding
    from thingctx.bindings.builtin.media import MediaBinding
    from thingctx.runtime import ThingClient

    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:cam1",
        "title": "cam",
        "actions": {
            "watch": {
                "forms": [
                    {
                        "href": "https://example.com/clip.mp4",
                        "x-thingctx-media": {"container": "mp4"},
                    }
                ]
            }
        },
    }
    return ThingClient(tds=[td], bindings=[HttpBinding(), MediaBinding(backends=[backend])])


def test_client_save_routes_to_backend_copy(tmp_path):
    backend = _FakeCopyBackend()
    client = _client_with(backend)
    out = tmp_path / "saved.mp4"

    asyncio.run(client.save("cam1__watch", str(out)))

    assert out.read_text() == "copied"
    assert backend.seen["url"] == "https://example.com/clip.mp4"
    assert backend.seen["target"] == str(out)
    assert "track" not in backend.seen["options"]  # default copies all streams


def test_client_save_track_passes_through(tmp_path):
    backend = _FakeCopyBackend()
    client = _client_with(backend)
    out = tmp_path / "saved.mp4"

    asyncio.run(client.save("cam1__watch", str(out), track="audio"))

    assert backend.seen["options"]["track"] == "audio"


def test_client_save_unknown_affordance_raises():
    backend = _FakeCopyBackend()
    client = _client_with(backend)
    with pytest.raises(KeyError, match="unknown media affordance"):
        asyncio.run(client.save("cam1__nope", "x.mp4"))


# ---- page (yt-dlp) affordance: resolve first, never feed the selector to av.open ----


def test_extractor_copy_single_stream_copies_resolved_url(monkeypatch):
    # A single chosen stream is copied straight from the resolved URL with its
    # headers; the yt-dlp `format` selector and the auth plan are stripped so the
    # output container is derived from the target, never opened as format="best".
    from thingctx.bindings.builtin.media import backends as b

    monkeypatch.setattr(
        b.ExtractorBackend,
        "_resolve_info",
        lambda self, url, opts: {
            "url": "https://cdn/real.mp4",
            "http_headers": {"User-Agent": "UA"},
        },
    )
    captured: dict = {}

    def _fake_mux(self, sources, target, *, options, stop):
        captured.update(sources=list(sources), target=target, options=dict(options))

    monkeypatch.setattr(b.PyAVBackend, "_mux", _fake_mux)

    b.ExtractorBackend().copy(
        "https://youtube.com/watch?v=x",
        "out.mp4",
        options={
            "resolve": "page",
            "format": "best[ext=mp4]/best",
            "track": "video",
            "auth": object(),
        },
        stop=threading.Event(),
    )

    assert captured["sources"] == [("https://cdn/real.mp4", {"User-Agent": "UA"})]
    assert "format" not in captured["options"]  # selector not forwarded to the open
    assert "auth" not in captured["options"]  # consumed at resolve, not reapplied
    assert captured["options"]["track"] == "video"  # other options survive


def test_extractor_copy_merged_downloads_each_format_then_muxes_local(monkeypatch):
    # A merged selector must NOT read two live streams (the CDN drops the idle
    # one) nor GET them by hand: each resolved format is DOWNLOADED to a temp file
    # by yt-dlp, then the local files are muxed.
    from thingctx.bindings.builtin.media import backends as b

    monkeypatch.setattr(
        b.ExtractorBackend,
        "_resolve_info",
        lambda self, url, opts: {"requested_formats": [{"format_id": "137"}, {"format_id": "140"}]},
    )
    downloaded: list = []

    def _fake_download(self, url, fid, tmpdir, options, stop):
        downloaded.append(fid)
        return f"{tmpdir}/{fid}.mp4"

    monkeypatch.setattr(b.ExtractorBackend, "_download_format", _fake_download)
    captured: dict = {}

    def _fake_mux(self, sources, target, *, options, stop):
        captured["sources"] = list(sources)
        captured["options"] = dict(options)

    monkeypatch.setattr(b.PyAVBackend, "_mux", _fake_mux)

    b.ExtractorBackend().copy(
        "https://youtube.com/watch?v=x",
        "out.mp4",
        options={"resolve": "page", "format": "bestvideo+bestaudio", "auth": object()},
        stop=threading.Event(),
    )

    assert downloaded == ["137", "140"]  # each format downloaded, in order
    # the mux reads the LOCAL downloaded files; no selector, no plan
    assert [s[0].endswith(("137.mp4", "140.mp4")) for s in captured["sources"]] == [True, True]
    assert all(s[1] is None for s in captured["sources"])
    assert "format" not in captured["options"] and "auth" not in captured["options"]


def test_extractor_download_failure_raises_clear_media_error(monkeypatch):
    # A failed stage names the stream and is a MediaError (not a bare Errno).
    from thingctx.bindings.builtin.media import backends as b
    from thingctx.bindings.builtin.media.binding import MediaError

    monkeypatch.setattr(
        b.ExtractorBackend,
        "_resolve_info",
        lambda self, url, opts: {"requested_formats": [{"format_id": "140"}]},
    )

    def _boom(self, url, fid, tmpdir, options, stop):
        raise MediaError(f"failed to stage stream {fid}: broken")

    monkeypatch.setattr(b.ExtractorBackend, "_download_format", _boom)
    monkeypatch.setattr(b.PyAVBackend, "_mux", lambda *a, **k: None)

    with pytest.raises(MediaError, match="stream 140"):
        b.ExtractorBackend().copy(
            "https://youtube.com/watch?v=x",
            "out.mp4",
            options={"resolve": "page", "format": "bestvideo+bestaudio"},
            stop=threading.Event(),
        )


def test_remux_passes_format_http_headers_to_input_open(monkeypatch, tmp_path):
    # The headers a resolver attaches to a stream must reach av.open for THAT
    # input (a CDN like googlevideo rejects the bytes without the matching
    # User-Agent), CRLF-joined into the ffmpeg ``headers`` option.
    av = pytest.importorskip("av")
    from thingctx.bindings.builtin.media import backends as b

    seen: list = []
    real_open = av.open

    def _fake_open(file, mode="r", **kw):
        if mode == "w":
            return real_open(file, mode="w", **kw)
        seen.append(dict(kw.get("options") or {}))
        raise RuntimeError("captured input open")

    monkeypatch.setattr(av, "open", _fake_open)

    with pytest.raises(RuntimeError, match="captured"):
        b.PyAVBackend()._remux(
            [("http://cdn/v.mp4", {"User-Agent": "UA", "Referer": "R"})],
            str(tmp_path / "o.mp4"),
            options={},
            stop=threading.Event(),
        )

    assert seen and seen[0].get("headers") == "User-Agent: UA\r\nReferer: R\r\n"


def test_remux_stages_remote_streams_then_muxes_locally(monkeypatch, tmp_path):
    # A merged remote source must NOT be read interleaved over the network (a CDN
    # drops the idle connection). Each remote stream is staged to a temp file
    # sequentially, with its headers, then muxed from the local files.
    from thingctx.bindings.builtin.media import backends as b

    v = tmp_path / "v.bin"
    v.write_bytes(b"v")
    a = tmp_path / "a.bin"
    a.write_bytes(b"a")
    staged_for = {"https://cdn/v": str(v), "https://cdn/a": str(a)}
    calls: list = []

    def _fake_stage(url, headers, options, stop):
        calls.append((url, headers))
        return staged_for[url]

    monkeypatch.setattr(b.PyAVBackend, "_stage", staticmethod(_fake_stage))
    seen: dict = {}

    def _fake_mux(self, sources, target, *, options, stop):
        seen.update(sources=list(sources), options=dict(options))

    monkeypatch.setattr(b.PyAVBackend, "_mux", _fake_mux)

    b.PyAVBackend()._remux(
        [("https://cdn/v", {"User-Agent": "UA-v"}), ("https://cdn/a", {"User-Agent": "UA-a"})],
        str(tmp_path / "o.mp4"),
        options={"auth": object()},
        stop=threading.Event(),
    )

    # staged sequentially, each with its own headers
    assert calls == [
        ("https://cdn/v", {"User-Agent": "UA-v"}),
        ("https://cdn/a", {"User-Agent": "UA-a"}),
    ]
    # the mux reads the LOCAL staged files; no headers, no plan needed locally
    assert seen["sources"] == [(str(v), None), (str(a), None)]
    assert "auth" not in seen["options"]


def test_direct_copy_drops_ytdlp_format_selector(monkeypatch, tmp_path):
    # A form may carry a yt-dlp `format` selector for the page case. On a DIRECT
    # open (PyAVBackend) the selector is meaningless and must not reach
    # av.open(format=...); copy() strips it before muxing.
    from thingctx.bindings.builtin.media import backends as b

    captured: dict = {}

    def _fake_remux(self, sources, target, *, options, stop):
        captured["options"] = dict(options)
        captured["sources"] = list(sources)

    monkeypatch.setattr(b.PyAVBackend, "_remux", _fake_remux)

    b.PyAVBackend().copy(
        "https://cdn/clip.mp4",
        str(tmp_path / "out.mp4"),
        options={"format": "bestvideo[ext=mp4]+bestaudio/best", "track": "video"},
        stop=threading.Event(),
    )

    assert "format" not in captured["options"]  # selector dropped on the direct open
    assert captured["options"]["track"] == "video"  # other options survive
    assert captured["sources"] == [("https://cdn/clip.mp4", None)]


def test_remux_single_remote_is_not_staged(monkeypatch, tmp_path):
    # One continuous read is fine; the single path must stream directly, no temp.
    from thingctx.bindings.builtin.media import backends as b

    def _boom(*a, **k):
        raise AssertionError("a single source must not be staged")

    monkeypatch.setattr(b.PyAVBackend, "_stage", staticmethod(_boom))
    seen: dict = {}
    monkeypatch.setattr(
        b.PyAVBackend,
        "_mux",
        lambda self, sources, target, *, options, stop: seen.update(sources=list(sources)),
    )

    b.PyAVBackend()._remux(
        [("https://cdn/v", {"User-Agent": "UA"})],
        str(tmp_path / "o.mp4"),
        options={},
        stop=threading.Event(),
    )

    assert seen["sources"] == [("https://cdn/v", {"User-Agent": "UA"})]


def test_remux_merged_local_sources_are_not_staged(monkeypatch, tmp_path):
    # Local files have no idle-drop problem; a merge of two local sources muxes
    # in place without staging.
    from thingctx.bindings.builtin.media import backends as b

    def _boom(*a, **k):
        raise AssertionError("local sources must not be staged")

    monkeypatch.setattr(b.PyAVBackend, "_stage", staticmethod(_boom))
    seen: dict = {}
    monkeypatch.setattr(
        b.PyAVBackend,
        "_mux",
        lambda self, sources, target, *, options, stop: seen.update(sources=list(sources)),
    )

    b.PyAVBackend()._remux(
        [("/tmp/v.mp4", None), ("/tmp/a.m4a", None)],
        str(tmp_path / "o.mp4"),
        options={},
        stop=threading.Event(),
    )

    assert seen["sources"] == [("/tmp/v.mp4", None), ("/tmp/a.m4a", None)]


@_real
def test_save_page_affordance_resolves_and_remuxes(tmp_path, monkeypatch):
    # End to end through ExtractorBackend with a stubbed resolver (no network):
    # a page affordance carrying a yt-dlp selector saves a bit-exact remux, just
    # like a direct URL does.
    pytest.importorskip("av")
    from thingctx.bindings.builtin.media import MediaBinding
    from thingctx.bindings.builtin.media import backends as b

    src = tmp_path / "in.mp4"
    out = tmp_path / "out.mp4"
    _gen(src, fps="30", dur=1.0, audio_rate=48000)
    # A single chosen stream: the resolver yields one URL (the local file here),
    # copied straight through.
    monkeypatch.setattr(
        b.ExtractorBackend,
        "_resolve_info",
        lambda self, url, opts: {"url": str(src), "http_headers": {}},
    )

    inv = MediaBinding(backends=[b.ExtractorBackend(), b.PyAVBackend()])
    form = WoTForm(
        href="https://youtube.com/watch?v=x",
        raw={
            "x-thingctx-media": {
                "resolve": "page",
                "format": "best[acodec!=none][vcodec!=none][ext=mp4]/best",
            }
        },
    )
    asyncio.run(inv.save(_ACTION, form, str(out)))

    sv = _stream(_probe(src), "video")
    ov = _stream(_probe(out), "video")
    assert ov["codec_name"] == sv["codec_name"]  # bit-exact, no re-encode
    assert ov["r_frame_rate"] == sv["r_frame_rate"]
    assert int(ov["nb_frames"]) == int(sv["nb_frames"])


@_real
def test_save_auto_direct_with_format_selector_on_form(tmp_path):
    # One form carries `resolve:auto` AND a yt-dlp `format` selector (needed for
    # the page case). A DIRECT URL through that form must ignore the selector and
    # save bit-exact, not feed it to av.open as a container.
    pytest.importorskip("av")
    from thingctx.bindings.builtin.media import MediaBinding
    from thingctx.bindings.builtin.media import backends as b

    src = tmp_path / "in.mp4"
    out = tmp_path / "out.mp4"
    _gen(src, fps="30", dur=1.0, audio_rate=48000)

    inv = MediaBinding(backends=[b.ExtractorBackend(), b.PyAVBackend()])
    form = WoTForm(
        href=str(src),  # a direct file, so auto picks the direct (PyAV) branch
        raw={
            "x-thingctx-media": {
                "resolve": "auto",
                "format": "bestvideo[ext=mp4]+bestaudio/best",
            }
        },
    )
    asyncio.run(inv.save(_ACTION, form, str(out)))

    sv = _stream(_probe(src), "video")
    ov = _stream(_probe(out), "video")
    assert ov["codec_name"] == sv["codec_name"]
    assert ov["r_frame_rate"] == sv["r_frame_rate"]
    assert int(ov["nb_frames"]) == int(sv["nb_frames"])


def _gen_video_only(path, *, fps="60", dur=1.0, vcodec="libx264") -> None:
    subprocess.check_call(
        [
            _FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate={fps}:duration={dur}",
            "-c:v",
            vcodec,
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(path),
        ]
    )


def _gen_audio_only(path, *, rate=48000, dur=1.0, acodec="aac") -> None:
    subprocess.check_call(
        [
            _FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate={rate}:duration={dur}",
            "-c:a",
            acodec,
            str(path),
        ]
    )


@_real
def test_save_merged_video_and_audio_muxes_bit_exact(tmp_path, monkeypatch):
    # The merged case: a video-only and an audio-only stream (what a 1080p60
    # DASH selector resolves to) mux into one file by stream copy, preserving the
    # high fps that the single muxed rendition would have lost.
    pytest.importorskip("av")
    from thingctx.bindings.builtin.media import MediaBinding
    from thingctx.bindings.builtin.media import backends as b

    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.m4a"
    out = tmp_path / "merged.mp4"
    _gen_video_only(video, fps="60", dur=1.0)
    _gen_audio_only(audio, rate=48000, dur=1.0)
    # A merged selector resolves to two formats; yt-dlp downloads each (stubbed
    # here to the pre-generated local files), then they mux into one target.
    monkeypatch.setattr(
        b.ExtractorBackend,
        "_resolve_info",
        lambda self, url, opts: {"requested_formats": [{"format_id": "137"}, {"format_id": "140"}]},
    )
    staged = {"137": str(video), "140": str(audio)}
    monkeypatch.setattr(
        b.ExtractorBackend,
        "_download_format",
        lambda self, url, fid, tmpdir, options, stop: staged[fid],
    )

    inv = MediaBinding(backends=[b.ExtractorBackend(), b.PyAVBackend()])
    form = WoTForm(
        href="https://youtube.com/watch?v=x",
        raw={"x-thingctx-media": {"resolve": "page", "format": "bestvideo+bestaudio"}},
    )
    asyncio.run(inv.save(_ACTION, form, str(out)))

    info = _probe(out)
    ov, oa = _stream(info, "video"), _stream(info, "audio")
    sv = _stream(_probe(video), "video")
    assert ov is not None and oa is not None  # both streams muxed in
    assert ov["codec_name"] == "h264" and oa["codec_name"] == "aac"  # no re-encode
    assert ov["r_frame_rate"] == sv["r_frame_rate"]  # 60fps preserved, not 30
    assert int(ov["nb_frames"]) == int(sv["nb_frames"])
