# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Blocking media backends for :class:`~thingctx.bindings.builtin.media.MediaBinding`.

Each backend opens a source and yields decoded :class:`Frame` objects. They run
in a worker thread, never on the event loop. Heavy dependencies (``av``,
``yt_dlp``) are imported lazily so this module imports without them installed.
"""

from __future__ import annotations

import contextlib
import fractions
import logging
import os
import shutil
import tempfile
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from thingctx.auth import av_auth_options, redact_url, ytdlp_auth_options
from thingctx.bindings.builtin.media.binding import MediaError
from thingctx.bindings.builtin.media.frame import Frame, MediaBackend
from thingctx.contracts import implements

_PYAV_SCHEMES = ("rtsp", "rtsps", "srt", "rtmp", "rtmps", "http", "https", "file", "")
_NOT_PYAV_SOURCES = ("webrtc", "genicam")

# Output URL scheme to muxer for the publish path. A file target (no scheme)
# lets the muxer be inferred from the extension.
_OUTPUT_FORMATS = {
    "rtmp": "flv",
    "rtmps": "flv",
    "rtsp": "rtsp",
    "rtsps": "rtsp",
    "srt": "mpegts",
}


def _output_format(url: str) -> str | None:
    return _OUTPUT_FORMATS.get(urlparse(url).scheme)


def _headers_to_ffmpeg(headers: dict | None) -> str | None:
    """FFmpeg needs request headers as one CRLF-joined string on an http(s)
    input. yt-dlp's per-format ``http_headers`` carry a User-Agent matching the
    client it resolved with; a CDN like googlevideo rejects the bytes without
    them, so they ride into ``av.open`` for that stream."""
    if not headers:
        return None
    for k, v in headers.items():
        if any(c in "\r\n" for c in str(k)) or any(c in "\r\n" for c in str(v)):
            raise ValueError(f"illegal newline in request header {k!r}")
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


# A direct media URL ends in a recognized media or playlist extension. Under the
# auto resolve mode an http(s) URL without one is treated as a page to resolve.
_DIRECT_MEDIA_EXTS = (
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv", ".wmv",
    ".ts", ".m2ts", ".mts", ".3gp", ".ogv", ".mpg", ".mpeg",
    ".m4a", ".mp3", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav",
    ".m3u8", ".mpd",
)  # fmt: skip


def _looks_direct(url: str) -> bool:
    """Auto-mode heuristic: a directly-openable stream vs a page to resolve. A
    non-http(s) scheme (rtsp/srt/file/local path) is always direct; an http(s)
    URL is direct only when its path ends in a known media or playlist extension,
    otherwise it is a page for yt-dlp. A caller override forces the mode with
    ``resolve: "direct"`` / ``"page"``."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return True
    return p.path.lower().endswith(_DIRECT_MEDIA_EXTS)


class _RedactingHandler(logging.Handler):
    """Redacts credentials from a record in place. Attached to the ``libav``
    logger, its ``handle()`` runs during propagation, before the host app's root
    handlers see the record, and scrubs any URL the message carries. It emits
    nothing itself, so it never suppresses or duplicates logs."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        redacted = redact_url(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()


def _install_libav_redaction() -> None:
    """Attach the redacting handler to the ``libav`` logger (where PyAV routes
    FFmpeg output) so a raised log level can never print a credentialed URL.
    Idempotent; mutates records in place, so it scrubs without suppressing logs."""
    log = logging.getLogger("libav")
    if getattr(log, "_thingctx_redacted", False):
        return
    log.addHandler(_RedactingHandler())
    log._thingctx_redacted = True  # type: ignore[attr-defined]


@implements(MediaBackend)
class PyAVBackend:
    """Decode the FFmpeg-reachable schemes (see ``_PYAV_SCHEMES``) to frames via
    PyAV, video (RGB) or audio (PCM) per the ``track`` option. Cannot handle WebRTC
    or GigE, those need a gateway or Aravis."""

    def can_open(self, url: str, hint: dict) -> bool:
        if hint.get("source") in _NOT_PYAV_SOURCES:
            return False
        mode = hint.get("resolve")
        if mode == "page" or hint.get("source") == "youtube":
            return False  # the extractor owns a page affordance
        if urlparse(url).scheme not in _PYAV_SCHEMES:
            return False
        if mode == "auto":
            # One affordance for any URL: open it directly only when it looks
            # like a direct stream; a page is left to the extractor.
            return _looks_direct(url)
        return True  # unset or explicit "direct": open directly

    def read(self, url: str, *, options: dict, stop: threading.Event) -> Iterator[Frame]:
        # optional dep, kept local so the core imports without the extra
        import av  # noqa: PLC0415

        _install_libav_redaction()
        av_options = dict(options.get("av_options") or {})
        plan = options.get("auth")
        if plan is not None:
            # Map the neutral auth plan onto FFmpeg (URL userinfo, headers,
            # query, TLS). All credential-to-engine logic lives in the applier.
            url, extra = av_auth_options(plan, url)
            av_options.update(extra)
        if urlparse(url).scheme in ("rtsp", "rtsps"):
            # TCP interleaving avoids UDP packet loss on most networks.
            av_options.setdefault("rtsp_transport", "tcp")

        track = options.get("track", "video")
        container = av.open(url, options=av_options, timeout=options.get("timeout"))
        # Seek-resume (opt in, seekable sources only): after a transient failure
        # the binding may re-open and ask to continue near the last pts. A live
        # source cannot seek its edge, so a failed seek falls through to a gap.
        resume_pts = options.get("_resume_pts")
        if resume_pts:
            with contextlib.suppress(Exception):
                container.seek(int(float(resume_pts) * 1_000_000))  # AV_TIME_BASE micros
        try:
            if track == "audio":
                for frame in container.decode(audio=0):
                    if stop.is_set():
                        break
                    yield self._audio(frame)
            else:
                vstream = container.streams.video[0]
                # Carry the source frame rate on each frame so the publish path
                # re-encodes at the source rate; a fixed output fps would
                # time stretch the video (a 30 fps source written at 25 fps runs
                # 30/25 = 1.2x long and drifts out of sync with its audio).
                src_rate = vstream.average_rate or vstream.guessed_rate
                rate = float(src_rate) if src_rate else None
                for frame in container.decode(vstream):
                    if stop.is_set():
                        break
                    yield self._video(frame, rate)
        finally:
            with contextlib.suppress(Exception):
                container.close()

    @staticmethod
    def _video(frame: Any, rate: float | None = None) -> Frame:
        return Frame(
            data=frame.to_ndarray(format="rgb24"),
            kind="video",
            pts=float(frame.time) if frame.time is not None else None,
            width=frame.width,
            height=frame.height,
            encoding="rgb24",
            meta={"rate": rate} if rate else {},
        )

    @staticmethod
    def _audio(frame: Any) -> Frame:
        layout = getattr(frame, "layout", None)
        fmt = getattr(getattr(frame, "format", None), "name", None)
        # The sample format and layout are kept in meta so the publish path can
        # reconstruct an encodable AudioFrame (passthrough or dub).
        return Frame(
            data=frame.to_ndarray(),
            kind="audio",
            pts=float(frame.time) if frame.time is not None else None,
            sample_rate=frame.sample_rate,
            channels=len(layout.channels) if layout else None,
            encoding="pcm",
            meta={"format": fmt, "layout": layout.name if layout else None},
        )

    def write(
        self, frames: Iterator[Frame], target: str, *, options: dict, stop: threading.Event
    ) -> None:
        """Encode and mux a single track of ``frames`` to ``target``. ``track``
        (``video``/``audio``) picks which stream the frames feed."""
        if options.get("track", "video") == "audio":
            self.write_av(None, frames, target, options=options, stop=stop)
        else:
            self.write_av(frames, None, target, options=options, stop=stop)

    def write_av(
        self,
        video: Iterator[Frame] | None,
        audio: Iterator[Frame] | None,
        target: str,
        *,
        options: dict,
        stop: threading.Event,
    ) -> None:
        """Encode and mux video and/or audio to ``target`` (an ingest URL or a
        file). The muxer is chosen from the URL scheme or the file extension.
        Streams are created lazily from the first frame of each track. Packets
        are interleaved by presentation time: a frame's ``pts`` (seconds) when
        the producer supplies it, else a per-track clock (video frame count over
        ``fps``; audio sample count over the sample rate). The shared timeline is
        what keeps a muxed A/V output in sync."""
        # optional dep, kept local so the core imports without the extra
        import av  # noqa: PLC0415

        _install_libav_redaction()
        av_options = dict(options.get("av_options") or {})
        plan = options.get("auth")
        if plan is not None:
            target, extra = av_auth_options(plan, target)
            av_options.update(extra)
        if urlparse(target).scheme in ("rtsp", "rtsps"):
            av_options.setdefault("rtsp_transport", "tcp")

        fmt = options.get("format") or _output_format(target)
        # A network ingest target that accepts the connection but never drains can
        # wedge the writer; the same timeout that bounds a read bounds the open.
        container = av.open(
            target, mode="w", format=fmt, options=av_options, timeout=options.get("timeout")
        )
        vstate: dict = {"stream": None, "count": 0, "last_pts": None}
        astate: dict = {"stream": None, "resampler": None, "fifo": None, "samples": 0}
        try:
            vid_it = iter(video) if video is not None else None
            aud_it = iter(audio) if audio is not None else None
            pv = next(vid_it, None) if vid_it is not None else None
            pa = next(aud_it, None) if aud_it is not None else None
            # The source frame rate (carried on the frame) drives the output so a
            # frames -> publish round trip preserves duration and A/V sync. The
            # fps option is only a fallback for raw frames with no source rate
            # (e.g. a producer feeding synthetic frames); a fixed fps is never
            # assumed when the source rate is known.
            rate = self._encode_rate(pv, options)
            # All streams must be added before the first packet is muxed (the
            # container header is written on first mux), so create them from the
            # first frame of each track up front, not lazily mid-stream.
            if pv is not None:
                self._ensure_video_stream(container, vstate, pv, rate, options)
            if pa is not None:
                self._ensure_audio_stream(container, astate, pa)
            while not stop.is_set() and (pv is not None or pa is not None):
                # Emit whichever pending frame has the earlier timeline position.
                take_video = pa is None or (
                    pv is not None
                    and self._pts_seconds(pv, vstate["count"], rate)
                    <= self._pts_seconds(pa, astate["samples"], pa.sample_rate or 48000)
                )
                if take_video:
                    # take_video implies pv is not None (either pa is None, so the
                    # loop guard forces pv, or the conjunct above tested pv), and
                    # pv came from vid_it so that is non-None too.
                    assert pv is not None  # noqa: S101 (loop invariant)
                    assert vid_it is not None  # noqa: S101 (loop invariant)
                    self._encode_video(container, vstate, pv, rate, options)
                    pv = next(vid_it, None)
                else:
                    # not take_video implies pa is not None (and pa came from aud_it).
                    assert pa is not None  # noqa: S101 (loop invariant)
                    assert aud_it is not None  # noqa: S101 (loop invariant)
                    self._encode_audio(container, astate, pa)
                    pa = next(aud_it, None)
            self._flush(container, vstate.get("stream"))
            self._flush_audio(container, astate)
            self._flush(container, astate.get("stream"))
        finally:
            with contextlib.suppress(Exception):
                container.close()

    @staticmethod
    def _encode_rate(fr: Frame | None, options: dict) -> int:
        """The video frame rate to encode at. The source rate (carried on the
        frame by the read path) wins so a re-encode preserves the source timing;
        the ``fps`` option is only a fallback."""
        src_rate = fr.meta.get("rate") if (fr is not None and fr.meta) else None
        if src_rate:
            return max(1, round(float(src_rate)))
        opt = options.get("fps")
        return max(1, int(opt)) if opt else 30

    @staticmethod
    def _pts_seconds(fr: Frame, count: int, rate: int) -> float:
        if fr.pts is not None:
            return float(fr.pts)
        if fr.kind == "audio":
            return count / float(rate or 48000)
        return count / float(rate or 30)

    @staticmethod
    def _ensure_video_stream(
        container: Any, vstate: dict, fr: Frame, fps: int, options: dict
    ) -> None:
        # optional dep, kept local so the core imports without the extra
        import numpy as np  # noqa: PLC0415

        if vstate["stream"] is not None:
            return
        h, w = np.ascontiguousarray(fr.data).shape[:2]
        stream = container.add_stream(options.get("video_codec", "libx264"), rate=fps)
        stream.width = w
        stream.height = h
        stream.pix_fmt = options.get("pix_fmt", "yuv420p")
        if options.get("bitrate"):
            stream.bit_rate = int(options["bitrate"])
        stream.options = {  # low latency defaults for live ingest
            "preset": options.get("preset", "veryfast"),
            "tune": options.get("tune", "zerolatency"),
        }
        vstate["stream"] = stream

    @classmethod
    def _encode_video(
        cls, container: Any, vstate: dict, fr: Frame, fps: int, options: dict
    ) -> None:
        # optional dep, kept local so the core imports without the extra
        import av  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        cls._ensure_video_stream(container, vstate, fr, fps, options)
        stream = vstate["stream"]
        arr = np.ascontiguousarray(fr.data)
        src_fmt = fr.encoding if fr.encoding in ("rgb24", "bgr24", "gray") else "rgb24"
        vframe = av.VideoFrame.from_ndarray(arr, format=src_fmt)
        # Stamp the source presentation time in the stream's time base (1/fps),
        # so a re-encode preserves the source timing (duration and A/V sync)
        # rather than re-clocking every frame to a fixed rate. A finer base here
        # (e.g. 1/90000) does not rescale cleanly to the mp4 stream on mux and
        # fails with EINVAL for some frame counts, so keep 1/fps. Frames with no
        # source pts (a raw producer) fall back to the frame index.
        pts = round(float(fr.pts) * fps) if fr.pts is not None else vstate["count"]
        # The muxer requires strictly increasing pts; nudge forward if a rate
        # mismatch (source pts vs chosen fps) would repeat or regress a value.
        if vstate["last_pts"] is not None and pts <= vstate["last_pts"]:
            pts = vstate["last_pts"] + 1
        vframe.pts = pts
        vframe.time_base = fractions.Fraction(1, fps)
        vstate["last_pts"] = pts
        for packet in stream.encode(vframe):
            container.mux(packet)
        vstate["count"] += 1

    @staticmethod
    def _ensure_audio_stream(container: Any, astate: dict, fr: Frame) -> None:
        # optional dep, kept local so the core imports without the extra
        import av  # noqa: PLC0415

        if astate["stream"] is not None:
            return
        stream = container.add_stream("aac", rate=fr.sample_rate or 48000)
        astate["stream"] = stream
        # Resample to the encoder's format/layout; an AAC encoder needs fixed
        # size frames, so a FIFO chunks the resampled audio to frame_size.
        astate["resampler"] = av.AudioResampler(
            format=stream.format, layout=stream.layout, rate=stream.rate
        )
        astate["fifo"] = av.AudioFifo()

    @classmethod
    def _encode_audio(cls, container: Any, astate: dict, fr: Frame) -> None:
        # optional dep, kept local so the core imports without the extra
        import av  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        cls._ensure_audio_stream(container, astate, fr)
        stream = astate["stream"]
        arr = np.ascontiguousarray(fr.data)
        rate = fr.sample_rate or 48000
        fmt = (fr.meta or {}).get("format") or "s16"
        layout = (fr.meta or {}).get("layout") or ("stereo" if (fr.channels or 1) > 1 else "mono")
        aframe = av.AudioFrame.from_ndarray(arr, format=fmt, layout=layout)
        aframe.sample_rate = rate
        pts_s = fr.pts if fr.pts is not None else astate["samples"] / float(rate)
        aframe.pts = round(pts_s * rate)
        aframe.time_base = fractions.Fraction(1, rate)
        astate["samples"] += arr.shape[-1]
        for resampled in astate["resampler"].resample(aframe):
            astate["fifo"].write(resampled)
        frame_size = stream.frame_size or 1024
        while astate["fifo"].samples >= frame_size:
            chunk = astate["fifo"].read(frame_size)
            for packet in stream.encode(chunk):
                container.mux(packet)

    @staticmethod
    def _flush_audio(container: Any, astate: dict) -> None:
        fifo = astate.get("fifo")
        stream = astate.get("stream")
        if fifo is None or stream is None:
            return
        remaining = fifo.read()  # whatever is left, below one frame_size
        if remaining is not None:
            for packet in stream.encode(remaining):
                container.mux(packet)

    @staticmethod
    def _flush(container: Any, stream: Any) -> None:
        if stream is not None:
            for packet in stream.encode(None):  # drain the encoder
                container.mux(packet)

    def copy(self, url: str, target: str, *, options: dict, stop: threading.Event) -> None:
        """Remux (stream copy) the source to ``target`` without decoding: packets
        pass through unchanged, so the output is bit exact. ``track`` limits the
        copy to one stream; by default every video and audio stream is copied.

        The target container must accept the source codecs (``.webm`` for vp9/opus,
        ``.mp4`` for h264/aac), else it raises; a transform goes through the
        re-encode path (``write_av``)."""
        # ``format`` in a media hint is the resolver's (yt-dlp) stream selector,
        # not a PyAV container; on a direct open it must never reach
        # ``av.open(format=...)``. The output container is derived from the
        # target. (The extractor strips it on its own branch.)
        opts = {k: v for k, v in options.items() if k != "format"}
        self._remux([(url, None)], target, options=opts, stop=stop)

    def _remux(
        self,
        sources: list[tuple[str, dict | None]],
        target: str,
        *,
        options: dict,
        stop: threading.Event,
    ) -> None:
        """Stream-copy one or more sources into ``target``. A single source is
        read straight through (one continuous connection). For a merged source
        (2+ remote streams, a video-only and an audio-only DASH rendition) the
        muxer would alternate reads, leaving one live connection idle; a CDN like
        googlevideo drops an idle connection, so the next read fails (Errno 5 /
        403). So each REMOTE stream is first staged to a temp file sequentially,
        with its headers (what yt-dlp does), then muxed from the local files,
        where interleaving has no idle-drop and stays bit exact. Local sources
        (already on disk) are muxed in place."""
        remote = [s for s in sources if urlparse(s[0]).scheme in ("http", "https")]
        if len(sources) > 1 and remote:
            staged: list[tuple[str, dict | None]] = []
            temps: list[str] = []
            try:
                for url, headers in sources:
                    if urlparse(url).scheme in ("http", "https"):
                        tmp = self._stage(url, headers, options, stop)
                        temps.append(tmp)
                        staged.append((tmp, None))
                    else:
                        staged.append((url, headers))
                # The signed CDN URLs are self-authenticating; staging used their
                # headers, so the local mux needs neither plan nor headers.
                local_opts = {k: v for k, v in options.items() if k != "auth"}
                self._mux(staged, target, options=local_opts, stop=stop)
            finally:
                for tmp in temps:
                    with contextlib.suppress(OSError):
                        Path(tmp).unlink()
            return
        self._mux(sources, target, options=options, stop=stop)

    @staticmethod
    def _stage(url: str, headers: dict | None, options: dict, stop: threading.Event) -> str:
        """Download one resolved stream fully to a temp file, in one continuous
        read with its request headers. Sequential full reads are what a CDN
        tolerates; the staged file then muxes locally with no idle-connection
        drop."""

        # Resolved stream URLs are http(s) CDN links; refuse any other scheme so
        # urlopen cannot be steered to a local file or custom handler.
        if not url.startswith(("http://", "https://")):
            raise MediaError(f"cannot stage a non-http(s) stream url: {redact_url(url)}")
        req = urllib.request.Request(url, headers=headers or {})  # noqa: S310 (scheme checked above)
        fd, path = tempfile.mkstemp(prefix="thingctx-remux-")
        try:
            with (
                urllib.request.urlopen(  # noqa: S310 (scheme checked above)
                    req, timeout=options.get("timeout")
                ) as resp,
                os.fdopen(fd, "wb") as f,
            ):
                while not stop.is_set():
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(path).unlink()
            raise
        return path

    def _mux(
        self,
        sources: list[tuple[str, dict | None]],
        target: str,
        *,
        options: dict,
        stop: threading.Event,
    ) -> None:
        """Mux several sources into one ``target`` container. Each source is a
        ``(url, http_headers)`` pair. Packets are interleaved across inputs by
        presentation time, every stream copied (no decode/encode)."""
        # optional dep, kept local so the core imports without the extra
        import av  # noqa: PLC0415

        _install_libav_redaction()
        plan = options.get("auth")
        track = options.get("track")  # None: copy all media streams
        fmt = options.get("format") or _output_format(target)
        out = av.open(target, mode="w", format=fmt, timeout=options.get("timeout"))
        inputs: list = []
        try:
            mk = getattr(out, "add_stream_from_template", None)
            # (input index, input stream index) -> output stream, so two inputs
            # that both number a stream 0 (a video-only and an audio-only DASH
            # stream) stay distinct.
            out_for: dict = {}
            demuxers: list = []
            for si, (src, headers) in enumerate(sources):
                in_options = dict(options.get("av_options") or {})
                url = src
                if plan is not None:
                    url, extra = av_auth_options(plan, url)
                    in_options.update(extra)
                # Per-format request headers win over any auth headers: the
                # resolved CDN URL needs the exact User-Agent it was issued for.
                hdr = _headers_to_ffmpeg(headers)
                if hdr is not None:
                    in_options["headers"] = hdr
                if urlparse(url).scheme in ("rtsp", "rtsps"):
                    in_options.setdefault("rtsp_transport", "tcp")
                inp = av.open(url, options=in_options, timeout=options.get("timeout"))
                inputs.append(inp)
                wanted = [
                    s
                    for s in inp.streams
                    if s.type in ("video", "audio") and (track is None or s.type == track)
                ]
                for s in wanted:
                    # Codec parameters copied, no encoder opened. The factory is
                    # ``add_stream_from_template`` on newer PyAV, ``add_stream(
                    # template=...)`` on older.
                    out_for[(si, s.index)] = mk(s) if mk else out.add_stream(template=s)
                if wanted:
                    demuxers.append((si, inp.demux(wanted)))
            if not out_for:
                raise ValueError("source has no copyable video/audio stream")
            self._interleave(demuxers, out, out_for, stop)
        finally:
            with contextlib.suppress(Exception):
                out.close()
            for inp in inputs:
                with contextlib.suppress(Exception):
                    inp.close()

    @staticmethod
    def _interleave(demuxers: list, out: Any, out_for: dict, stop: threading.Event) -> None:
        """Mux packets from several demuxers in presentation-time order. Each
        packet keeps its source time base; reassigning its output stream lets the
        muxer rescale on write. Flush packets (no dts) are skipped."""

        def _next(dem: Any) -> Any:  # the next packet that carries a timestamp
            for pkt in dem:
                if pkt.dts is not None:
                    return pkt
            return None

        # One pending packet per input; repeatedly emit the earliest.
        heads = []
        for si, dem in demuxers:
            pkt = _next(dem)
            if pkt is not None:
                heads.append([si, dem, pkt])

        def _pts_seconds(entry: Any) -> float:
            pkt = entry[2]
            ts = pkt.dts if pkt.dts is not None else pkt.pts
            return float(ts * pkt.time_base) if ts is not None and pkt.time_base else 0.0

        while heads and not stop.is_set():
            entry = min(heads, key=_pts_seconds)
            si, dem, pkt = entry
            pkt.stream = out_for[(si, pkt.stream.index)]
            out.mux(pkt)
            nxt = _next(dem)
            if nxt is None:
                heads.remove(entry)
            else:
                entry[2] = nxt


@implements(MediaBackend)
class ExtractorBackend(PyAVBackend):
    """Resolve a web page URL to a direct media URL with yt-dlp, then decode it
    with PyAV. Works for both recorded and live (HLS) media.

    Selected by a declared media hint (``resolve: "page"``), never by hostname,
    so the runtime carries no per site knowledge. ``source: "youtube"`` is an
    alias for the same intent. Under ``resolve: "auto"`` it claims any http(s)
    URL that is not already a direct stream, so one affordance serves both a
    direct URL and a page."""

    def can_open(self, url: str, hint: dict) -> bool:
        mode = hint.get("resolve")
        if mode == "direct":
            return False
        if mode == "page" or hint.get("source") == "youtube":
            # A page is always http(s). Refuse any other scheme (file:, ftp:,
            # data:, javascript:) here, independent of the file/private flags, so
            # a page hint cannot hand a local-file or SSRF URL to the extractor.
            return urlparse(url).scheme in ("http", "https")
        if mode == "auto":
            # Only an http(s) page resolves via yt-dlp; other schemes (and a
            # direct media URL) are opened directly by PyAVBackend.
            return urlparse(url).scheme in ("http", "https") and not _looks_direct(url)
        return False

    def _ydl_opts(self, options: dict, *, extra: dict | None = None) -> dict:
        """Base yt-dlp options from the form/call: the format selector, account
        login (a declared security plan), and cookie access. ``extra`` overrides
        for a particular run (e.g. a download output template)."""
        opts: dict = {
            "format": options.get("format", "best[protocol^=http]"),
            "quiet": True,
            "no_warnings": True,
        }
        plan = options.get("auth")
        if plan is not None:
            # Account login for sites that gate content behind one.
            opts.update(ytdlp_auth_options(plan))
        # Cookie-based access (the reliable path for private/members content) is
        # an extractor option, not a credential: a cookie file or a browser to
        # read cookies from, declared on the form's media hint.
        if options.get("cookiefile"):
            opts["cookiefile"] = options["cookiefile"]
        if options.get("cookies_from_browser"):
            opts["cookiesfrombrowser"] = (options["cookies_from_browser"],)
        if extra:
            opts.update(extra)
        return opts

    def _resolve_info(self, url: str, options: dict) -> dict:
        """Resolve a page to its yt-dlp info dict (no download). The chosen
        stream(s) are in ``requested_formats`` for a merged selector, else the
        info itself names a single stream."""
        # optional dep, kept local so the core imports without the extra
        import yt_dlp  # noqa: PLC0415

        with yt_dlp.YoutubeDL(self._ydl_opts(options)) as ydl:
            # yt_dlp is untyped, so extract_info is Any; a resolvable page yields
            # the info mapping the contract promises.
            return cast(dict, ydl.extract_info(url, download=False))

    def _extract(self, url: str, options: dict) -> list[tuple[str, dict]]:
        """Resolve a page to its chosen media stream(s), each a ``(url,
        http_headers)`` pair. A merged selector (``bestvideo+bestaudio``) yields
        two (a video-only and an audio-only stream) in ``requested_formats``, to
        be muxed together; a single selector yields one. The per-format
        ``http_headers`` carry the User-Agent the CDN requires, so they travel
        with the URL to the open."""
        info = self._resolve_info(url, options)
        requested = info.get("requested_formats")
        formats = requested if requested else [info]
        return [(f["url"], f.get("http_headers") or {}) for f in formats]

    def _download_format(
        self, url: str, fid: str, tmpdir: str, options: dict, stop: threading.Event
    ) -> str:
        """Download one resolved format fully to a temp file with yt-dlp and
        return its path. yt-dlp's own downloader (range requests, retries, client
        tokens) fetches a googlevideo DASH stream reliably where a plain GET does
        not; the file is fully written and closed before the mux opens it, so
        there is no live pipe between fetch and mux to break."""
        # optional dep, kept local so the core imports without the extra
        import yt_dlp  # noqa: PLC0415

        def _abort(_d: dict) -> None:  # cooperative cancel from the worker's stop
            if stop.is_set():
                raise yt_dlp.utils.DownloadError("cancelled")

        outtmpl = str(Path(tmpdir) / f"{fid}.%(ext)s")
        opts = self._ydl_opts(
            options,
            extra={
                "format": fid,
                "outtmpl": outtmpl,
                "noprogress": True,
                "overwrites": True,
                "progress_hooks": [_abort],
            },
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            raise MediaError(f"failed to stage stream {fid}: {redact_url(str(exc))}") from exc
        produced = [
            str(p)
            for p in Path(tmpdir).iterdir()
            if p.name.startswith(f"{fid}.") and not p.name.endswith(".part")
        ]
        if not produced:
            raise MediaError(f"stream {fid} produced no file")
        return produced[0]

    def read(self, url: str, *, options: dict, stop: threading.Event) -> Iterator[Frame]:
        # The decode path opens one container per call, so it takes the first
        # resolved stream (a single selector is the norm there), carrying that
        # format's request headers into the open.
        resolved, headers = self._extract(url, options)[0]
        hdr = _headers_to_ffmpeg(headers)
        opts = options
        if hdr is not None:
            opts = {**options, "av_options": {**(options.get("av_options") or {}), "headers": hdr}}
        yield from super().read(resolved, options=opts, stop=stop)

    def copy(self, url: str, target: str, *, options: dict, stop: threading.Event) -> None:
        """Stream-copy a page source to ``target``. A single chosen stream is
        copied straight from the resolved URL. A merged selector
        (``bestvideo+bestaudio``) resolves to two DASH streams that cannot be
        read interleaved over the network (a CDN drops the idle one, and a plain
        GET of a googlevideo URL is unreliable), so each is downloaded fully to a
        temp file by yt-dlp, then the closed local files are stream-copy muxed
        into one target. ``format`` is the yt-dlp selector, not a PyAV container;
        the output container comes from the target. Auth and per-format headers
        are consumed at resolve/download time."""
        info = self._resolve_info(url, options)
        requested = info.get("requested_formats")
        opts = {k: v for k, v in options.items() if k not in ("format", "auth")}
        if not requested:
            # One stream: copy it directly, carrying its request headers.
            src = (info["url"], info.get("http_headers") or {})
            super()._mux([src], target, options=opts, stop=stop)
            return

        tmpdir = tempfile.mkdtemp(prefix="thingctx-dl-")
        try:
            local: list[tuple[str, dict | None]] = [
                (self._download_format(url, f["format_id"], tmpdir, options, stop), None)
                for f in requested
            ]
            super()._mux(local, target, options=opts, stop=stop)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
