# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Media binding: pull frames from a stream, or push frames to one.

The continuous-binary plane (audio/video), distinct from the request/response
bindings and from the event/subscription plane (MQTT, SSE, Pub/Sub). Event
subscriptions are also streams, but they carry discrete structured messages and
are bindable WoT Events. Media is continuous, encoded, session oriented, and
reached by reference; the runtime never binds it as a property value. This
binding opens the session off the event loop and yields decoded frames as an
async iterator (consume), or pushes frames to an ingest target (produce). The
control around a stream (generate stream, get ingest uri) stays on the
request/response plane.

Backends are blocking (FFmpeg/PyAV). The binding runs them in a worker thread and
bridges frames back through a bounded queue. Backpressure is a policy: ``latest``
sheds all but the newest frame (live video, low latency), ``all`` paces the
source to the consumer (lossless). The surface is the same for one source or
many: drive a fleet with ``asyncio.gather`` over several ``frames()`` iterators.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

from thingctx.auth import AuthRegistry, AuthStrategy, apply_media, redact_url
from thingctx.bindings.base import AuthMixin, ProtocolBinding
from thingctx.bindings.builtin.media.frame import Frame, MediaBackend
from thingctx.contracts import implements
from thingctx.reliability import RetryPolicy, TransportError
from thingctx.thing import WoTAction, WoTForm


class MediaError(Exception):
    """A media read/connection failure. Any credentials embedded in the source
    URL (userinfo, token query params) are redacted from the message, so media
    errors never leak secrets into logs or tracebacks."""


# PyAV maps an HTTP status onto a named error; these are the ones worth a retry
# on a long media read. A page-resolved CDN URL throttling to 403 mid-stream is
# the common one: the URL is dead but a fresh resolve works. 401/404/400 are
# fatal (auth or missing), so they are not retried.
_TRANSIENT_HTTP = {
    "HTTPForbiddenError": 403,
    "HTTPInternalServerError": 500,
    "HTTPBadGatewayError": 502,
    "HTTPServiceUnavailableError": 503,
    "HTTPGatewayTimeoutError": 504,
}
_FATAL_HTTP = {
    "HTTPUnauthorizedError": 401,
    "HTTPBadRequestError": 400,
    "HTTPNotFoundError": 404,
}


def _media_status(exc: BaseException) -> int | None:
    """The HTTP status PyAV attached to a media error, if any."""
    name = type(exc).__name__
    return _TRANSIENT_HTTP.get(name) or _FATAL_HTTP.get(name)


def _is_transient_media(exc: BaseException) -> bool:
    """Whether a media read error is worth re-opening (re-resolve + resume).
    Covers a throttled/expired CDN URL (403), the retryable 5xx family, and
    network blips (timeout, connection reset); decode and auth errors are not."""
    name = type(exc).__name__
    if name in _TRANSIENT_HTTP:
        return True
    if name in _FATAL_HTTP:
        return False
    if isinstance(exc, TimeoutError | ConnectionError | BrokenPipeError):
        return True
    msg = str(exc).lower()
    return "timed out" in msg or "connection reset" in msg or "403 forbidden" in msg


# Schemes whose hrefs route here directly. Sources whose href is an http(s) page
# are routed by the media hint instead (see ``handles``).
MEDIA_SCHEMES = ("rtsp", "rtsps", "srt", "rtmp", "rtmps", "webrtc")
_MEDIA_HINT = "x-thingctx-media"
# Sources that produce in real time, named by the media hint rather than the URL
# scheme (their href is often an http(s) page or a device handle).
_LIVE_SOURCES = ("webrtc", "genicam")


def _is_live_source(url: str, options: dict) -> bool:
    """Whether the source produces in real time. A live transport cannot be
    paced, so the ``latest`` policy drops frames there. A finite or seekable
    source (a file, or VOD over http(s)) can always be paced, so it is read
    losslessly even under ``latest``: dropping a finite source's frames is pure
    loss (it decimates an ingest's fps) with no real-time benefit. An explicit
    ``live`` hint forces the live treatment for the rare live-over-http(s) case
    (HLS/DASH live)."""
    if options.get("live"):
        return True
    if urlparse(url).scheme in MEDIA_SCHEMES:
        return True
    return options.get("source") in _LIVE_SOURCES


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Schemes whose URL is meaningless without a host, so an empty netloc is a clear
# sign of a malformed source (e.g. a bare ``https://``).
_HOSTED_SCHEMES = ("http", "https", "rtsp", "rtsps", "rtmp", "rtmps", "srt")


def _clean_source(url: str) -> str:
    """Strip and validate a media source, raising a clear :class:`MediaError`
    before it reaches yt-dlp or av. Accepts any scheme'd URL or a local path
    (spaces allowed); rejects an empty value, an embedded control character (a
    multi-line paste), or a hosted-scheme URL with no host."""
    if not isinstance(url, str) or not url.strip():
        raise MediaError("media source is empty; expected a URL or a file path")
    cleaned = url.strip()
    if _CONTROL_CHARS.search(cleaned):
        raise MediaError(f"malformed media source: {url!r}")
    parsed = urlparse(cleaned)
    if parsed.scheme in _HOSTED_SCHEMES and not parsed.netloc:
        raise MediaError(f"malformed media source (no host): {url!r}")
    return cleaned


def _media_hint(form: WoTForm) -> dict:
    """The form's media hint (``x-thingctx-media``) as a dict, or empty."""
    raw = getattr(form, "raw", {}) or {}
    hint = raw.get(_MEDIA_HINT)
    if isinstance(hint, dict):
        return hint
    if isinstance(hint, str):
        return {"source": hint}
    return {}


def is_media_form(form: WoTForm) -> bool:
    """Whether a form belongs to the media plane: a media scheme
    (``rtsp``/``webrtc``/...) or any form carrying the ``x-thingctx-media`` hint
    (e.g. an http(s) href that should decode frames rather than be fetched)."""
    return form.scheme in MEDIA_SCHEMES or bool(_media_hint(form))


@implements(ProtocolBinding)
class MediaBinding(AuthMixin):
    """Drives media forms. Selected for the media schemes, or for any form
    carrying a media hint. Exposes ``frames()`` (consume) and ``publish()``
    (produce), for both video and audio tracks.

    Honors declared security through the transport neutral auth layer: it
    resolves each owner's schemes into neutral credential material (see
    :class:`AuthMixin`) and maps it onto the source with ``apply_media`` (URL
    userinfo, request headers, query tokens, or TLS). No auth logic lives in this
    transport.

    A media-plane binding, not a control-plane one. It deliberately does not
    implement the control-plane methods (``read`` / ``write`` / ``subscribe`` /
    bulk / async lifecycle); a continuous stream has no request/response surface,
    so ``invoke`` raises and directs callers to ``frames()`` / ``publish()``."""

    scheme = "rtsp"
    schemes = MEDIA_SCHEMES

    def __init__(
        self,
        backends: list[MediaBackend] | None = None,
        *,
        max_queue: int = 4,
        backpressure: str = "latest",
        credentials: dict | None = None,
        timeout: float = 30.0,
        allow_insecure_oauth: bool = False,
        auth: AuthRegistry | None = None,
        extra_auth: list[AuthStrategy] | None = None,
        retry: RetryPolicy | None = None,
        resume: str = "gap",
        allow_file: bool = True,
        block_private: bool = False,
        allow_backend_options: bool = True,
    ) -> None:
        # Source/target policy for hosts that process semi-trusted TDs:
        #  allow_file          - permit file:// and bare local paths as a source
        #                        (a local-ingest feature; turn off to forbid
        #                        reading arbitrary local files),
        #  block_private       - refuse http(s) sources on private/loopback hosts
        #                        (off by default: a WoT camera is often on a LAN),
        #  allow_backend_options - let call-time args set backend options such as
        #                        cookiefile / cookies_from_browser / av_options
        #                        (turn off so only the TD hint can).
        self._allow_file = allow_file
        self._block_private = block_private
        self._allow_backend_options = allow_backend_options
        # Lazy default backends so importing this module never requires the
        # optional media dependencies.
        if backends is None:
            from thingctx.bindings.builtin.media.backends import ExtractorBackend, PyAVBackend

            backends = [ExtractorBackend(), PyAVBackend()]
        if backpressure not in ("latest", "all"):
            raise ValueError("backpressure must be 'latest' or 'all'")
        if resume not in ("gap", "seek"):
            raise ValueError("resume must be 'gap' or 'seek'")
        self._backends = list(backends)
        self._max_queue = max(1, max_queue)
        # "latest": shed all but the newest frame when the consumer falls behind
        # (live media keeps latency low). "all": pace the source to the consumer
        # so no frame is lost (finite or lossless sources).
        self._backpressure = backpressure
        # Reliability for a long read: on a transient error re-open the source
        # (page sources re-resolve, since the old CDN URL is dead) and resume.
        # "gap" continues from the fresh position (the only option for a live
        # source, which cannot seek its edge); "seek" asks a seekable source to
        # continue near the last pts. Pass ``RetryPolicy(retries=0)`` to disable
        # re-opening (a transient error then surfaces immediately).
        self._retry = RetryPolicy() if retry is None else retry
        self._resume = resume
        self._init_auth(
            credentials=credentials,
            auth=auth,
            extra_auth=extra_auth,
            timeout=timeout,
            allow_insecure_oauth=allow_insecure_oauth,
        )

    def handles(self, form: WoTForm) -> bool:
        """Whether this binding should drive ``form``: a media scheme, or a
        media hint on an otherwise http(s) form (e.g. a page resolved by an
        extractor)."""
        return form.scheme in self.schemes or bool(_media_hint(form))

    # Sensitive backend options a call-time argument may not set unless the
    # binding is configured to allow it (only the TD hint may otherwise).
    _GUARDED_OPTIONS = ("cookiefile", "cookies_from_browser", "av_options")

    def _guard_source(self, url: str) -> None:
        """Apply the source policy: optionally forbid local files, optionally
        refuse private/loopback network hosts. Both are off by default because a
        WoT source is often a local file or a LAN device."""
        scheme = urlparse(url).scheme
        if not self._allow_file and scheme in ("", "file"):
            raise MediaError(f"local-file media source is not allowed by policy: {url!r}")
        if self._block_private and scheme in _HOSTED_SCHEMES:
            from thingctx.netpolicy import resolve_is_private

            host = urlparse(url).hostname or ""
            # Resolve the host: a hostname (localhost, a cloud metadata name, or
            # an attacker A-record) that points at a private address must be
            # blocked too, not only a literal private IP.
            if resolve_is_private(host):
                raise MediaError(
                    f"media source host {host!r} is a private or loopback address "
                    "(blocked by policy)"
                )

    def _guarded_rest(self, rest: dict) -> dict:
        """Drop call-time backend options the binding does not allow (only the
        TD hint may set them then)."""
        if self._allow_backend_options:
            return rest
        return {k: v for k, v in rest.items() if k not in self._GUARDED_OPTIONS}

    def _confine_target(self, target: str) -> str:
        """Confine a local output target (a bare path or ``file://``): refuse a
        symlink, and keep it inside ``THINGCTX_DOWNLOAD_DIR`` when set. A network
        ingest target (rtmp/http/...) passes through unchanged."""
        import os
        from urllib.parse import urlparse as _up

        scheme = _up(str(target)).scheme
        if scheme not in ("", "file"):
            return target
        from thingctx.netpolicy import confine_path

        path = target[len("file://") :] if str(target).startswith("file://") else target
        base = os.environ.get("THINGCTX_DOWNLOAD_DIR") or None
        return str(confine_path(path, base=base))

    async def invoke(self, action: WoTAction, form: WoTForm, arguments: dict[str, Any]) -> Any:
        raise TypeError(
            "MediaBinding has no request/response surface; use frames() to "
            "consume or publish() to produce media."
        )

    def _pick(self, url: str, hint: dict) -> MediaBackend:
        for backend in self._backends:
            if backend.can_open(url, hint):
                return backend
        raise LookupError(f"no media backend for {url!r} (hint={hint!r})")

    async def frames(
        self,
        action: WoTAction,
        form: WoTForm,
        arguments: dict[str, Any] | None = None,
        *,
        track: str = "video",
    ) -> AsyncIterator[Frame]:
        """Open the form's media source and yield decoded frames for ``track``
        (``video`` or ``audio``). Blocking decode runs in a worker thread;
        frames cross back through a bounded queue under the backpressure
        policy."""
        if track not in ("video", "audio"):
            raise ValueError("track must be 'video' or 'audio'")
        url, rest = form.fill(arguments or {})
        url = _clean_source(url)
        hint = _media_hint(form)
        self._guard_source(url)
        backend = self._pick(url, hint)
        # Call-time arguments not consumed by the href template flow to the
        # backend (e.g. cookies_from_browser, cookiefile, format), so a static
        # TD can take per-call options without being mutated. The explicit
        # ``track`` and the resolved ``auth`` are reserved: they are set last so
        # an argument cannot override them.
        options = {**hint, **self._guarded_rest(rest), "track": track}
        # Resolve the owning Thing's declared security and hand the backend a
        # neutral auth plan; the backend maps it to its engine (URL userinfo for
        # a decoder, account login for the extractor). Absent declared security,
        # no plan is attached.
        creds = await self._resolve_credentials(getattr(action, "thing_id", None), form)
        if creds:
            plan = apply_media(creds)
            if plan.has_credentials:
                options["auth"] = plan
        async for frame in self._pump(backend.read, url, options):
            yield frame

    async def publish(
        self,
        action: WoTAction,
        form: WoTForm,
        frames: AsyncIterator[Frame],
        arguments: dict[str, Any] | None = None,
        *,
        track: str = "video",
        audio: AsyncIterator[Frame] | None = None,
    ) -> None:
        """Push frames to the form's ingest target (a URL or a file), the mirror
        of ``frames()``. The consumer produces frames on the event loop; a worker
        thread encodes and muxes them off it, paced through a bounded queue.

        With ``audio`` supplied, ``frames`` is the video track and ``audio`` is
        muxed alongside it into one A/V output (passthrough the source's audio,
        or a dubbed track), synced by pts at the writer. A single track publishes
        on its own with ``track`` (``video`` or ``audio``)."""
        if track not in ("video", "audio"):
            raise ValueError("track must be 'video' or 'audio'")
        url, rest = form.fill(arguments or {})
        url = _clean_source(url)
        url = self._confine_target(url)
        hint = _media_hint(form)
        backend = self._pick(url, hint)
        # Call-time arguments (minus consumed uriVariables) reach the encode
        # backend the same way they do for frames(): a static TD can take a
        # per-call format/codec/etc without mutation. ``track`` and ``auth`` are
        # reserved and set last.
        options = {**hint, **self._guarded_rest(rest), "track": track}
        creds = await self._resolve_credentials(getattr(action, "thing_id", None), form)
        if creds:
            plan = apply_media(creds)
            if plan.has_credentials:
                options["auth"] = plan
        if audio is None and track == "video":
            await self._drain(backend.write, url, options, frames)
            return
        # The muxed (or audio-only) path needs a backend that owns both streams
        # in one container; it cannot be composed from two single-track writes.
        if not hasattr(backend, "write_av"):
            raise TypeError(f"{type(backend).__name__} has no write_av; cannot mux audio")
        video_src = None if (track == "audio" and audio is None) else frames
        audio_src = frames if (track == "audio" and audio is None) else audio
        await self._drain_av(backend.write_av, url, options, video_src, audio_src)

    async def save(
        self,
        action: WoTAction,
        form: WoTForm,
        target: str,
        arguments: dict[str, Any] | None = None,
        *,
        track: str | None = None,
    ) -> None:
        """Remux the form's media source to ``target`` (a file) by stream copy:
        the source's compressed packets are written through unchanged, so the
        file is bit exact (same codecs, frame rate, A/V sync) with no re-encode.
        ``publish`` is the re-encode path for a transform. ``track``
        (``video``/``audio``) limits the copy to one stream; by default every
        media stream is copied.

        The target container must accept the source codecs (``.webm`` for
        vp9/opus, ``.mp4`` for h264/aac); an incompatible target raises."""
        if track not in (None, "video", "audio"):
            raise ValueError("track must be 'video', 'audio', or None")
        url, _ = form.fill(arguments or {})
        url = _clean_source(url)
        self._guard_source(url)
        target = self._confine_target(target)
        hint = _media_hint(form)
        backend = self._pick(url, hint)
        if not hasattr(backend, "copy"):
            raise TypeError(f"{type(backend).__name__} has no copy; cannot remux")
        options = {**hint}
        if track is not None:
            options["track"] = track
        creds = await self._resolve_credentials(getattr(action, "thing_id", None), form)
        if creds:
            plan = apply_media(creds)
            if plan.has_credentials:
                options["auth"] = plan
        await self._run_copy(backend.copy, url, target, options)

    async def _run_copy(self, copy, url: str, target: str, options: dict) -> None:  # noqa: ANN001
        """Run a blocking stream-copy in a worker thread. No frame queue bridge
        is needed (the copy is a single blocking call, not a per-frame handoff);
        a worker error is re-raised on the event loop with credentials scrubbed,
        and a cancellation asks the copy to stop."""
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        error: list[BaseException] = []

        def _worker() -> None:
            try:
                copy(url, target, options=options, stop=stop)
            except MediaError as exc:  # already a clear, scrubbed media error
                error.append(exc)
            except BaseException as exc:
                error.append(MediaError(f"{type(exc).__name__}: {redact_url(str(exc))}"))
            finally:
                stop.set()

        thread = threading.Thread(target=_worker, name="thingctx-media-copy", daemon=True)
        thread.start()
        try:
            await loop.run_in_executor(None, thread.join)
        except BaseException:
            stop.set()  # cancellation: ask the copy to wind down, then reap
            await loop.run_in_executor(None, thread.join)
            raise
        if error:
            raise error[0]

    async def _drain(self, write, target: str, options: dict, source: AsyncIterator[Frame]) -> None:
        """Bridge an async frame source to a blocking writer thread.

        Frames cross through a bounded queue; when it fills, the producer awaits
        a free slot, so the encoder paces the source and no frame is dropped. A
        worker error is re-raised on the event loop with credentials scrubbed
        from the message.
        """
        import queue as _queue

        loop = asyncio.get_running_loop()
        q: _queue.Queue = _queue.Queue(maxsize=self._max_queue)
        stop = threading.Event()
        done = object()
        error: list[BaseException] = []

        def _blocking_frames() -> Any:
            while True:
                item = q.get()
                if item is done:
                    return
                yield item

        def _worker() -> None:
            try:
                write(_blocking_frames(), target, options=options, stop=stop)
            except BaseException as exc:
                # Scrub credentials the engine may echo from the target URL; do
                # not chain the original (its message can hold the raw URL).
                error.append(MediaError(f"{type(exc).__name__}: {redact_url(str(exc))}"))
            finally:
                stop.set()

        def _put(item: Any) -> None:
            # Block until a slot frees or the worker stops, so a dead writer
            # never wedges the producer on a full queue.
            while not stop.is_set():
                try:
                    q.put(item, timeout=0.1)
                    return
                except _queue.Full:
                    continue

        thread = threading.Thread(target=_worker, name="thingctx-media-pub", daemon=True)
        thread.start()
        sent_done = False
        try:
            async for frame in source:
                if stop.is_set():  # the writer stopped early
                    break
                await loop.run_in_executor(None, _put, frame)
            # Graceful end of stream; signal a drain. Do not set ``stop``, the
            # writer must flush every queued frame and the encoder's tail.
            if not stop.is_set():
                await loop.run_in_executor(None, q.put, done)
                sent_done = True
        except BaseException:
            # Consumer error or cancellation; ask the writer to stop promptly.
            stop.set()
            raise
        finally:
            if not sent_done:
                # Unblock the worker's get() on the abnormal path.
                with contextlib.suppress(Exception):
                    q.put_nowait(done)
            await loop.run_in_executor(None, thread.join)
            if error:
                raise error[0]

    async def _drain_av(self, write_av, target: str, options: dict, video, audio) -> None:
        """Bridge two async frame sources (video, audio; either may be None) to a
        single blocking ``write_av`` that muxes both into one container. Each
        track crosses on its own bounded queue; the worker pulls from both and
        interleaves by pts. A worker error is re-raised on the event loop with
        credentials scrubbed from the message."""
        import queue as _queue

        loop = asyncio.get_running_loop()
        stop = threading.Event()
        done = object()
        error: list[BaseException] = []
        depth = self._max_queue
        qv: _queue.Queue | None = _queue.Queue(maxsize=depth) if video is not None else None
        qa: _queue.Queue | None = _queue.Queue(maxsize=depth) if audio is not None else None

        def _blocking(q: _queue.Queue | None):
            if q is None:
                return None

            def _gen() -> Any:
                while True:
                    item = q.get()
                    if item is done:
                        return
                    yield item

            return _gen()

        def _worker() -> None:
            try:
                write_av(_blocking(qv), _blocking(qa), target, options=options, stop=stop)
            except BaseException as exc:
                error.append(MediaError(f"{type(exc).__name__}: {redact_url(str(exc))}"))
            finally:
                stop.set()

        def _put(q: _queue.Queue, item: Any) -> None:
            while not stop.is_set():
                try:
                    q.put(item, timeout=0.1)
                    return
                except _queue.Full:
                    continue

        thread = threading.Thread(target=_worker, name="thingctx-media-av", daemon=True)
        thread.start()

        async def _feed(source: AsyncIterator[Frame], q: _queue.Queue) -> None:
            sent_done = False
            try:
                async for frame in source:
                    if stop.is_set():
                        break
                    await loop.run_in_executor(None, _put, q, frame)
                if not stop.is_set():
                    await loop.run_in_executor(None, q.put, done)
                    sent_done = True
            finally:
                if not sent_done:
                    with contextlib.suppress(Exception):
                        q.put_nowait(done)

        feeds = []
        if video is not None:
            feeds.append(_feed(video, qv))
        if audio is not None:
            feeds.append(_feed(audio, qa))
        try:
            await asyncio.gather(*feeds)
        except BaseException:
            stop.set()
            raise
        finally:
            await loop.run_in_executor(None, thread.join)
            if error:
                raise error[0]

    async def _pump(self, read, url: str, options: dict) -> AsyncIterator[Frame]:
        """Run a blocking frame generator in a thread and yield its frames on
        the event loop.

        With ``backpressure="latest"`` the oldest queued frame is dropped when
        the consumer falls behind. With ``"all"`` a free-slot semaphore paces the
        worker to the consumer so no frame is lost. Errors and end-of-stream are
        control items that always reach the consumer.
        """
        loop = asyncio.get_running_loop()
        # Shed only for a live source that cannot be paced; a finite/seekable
        # source is always read losslessly so an ingest keeps every frame (and
        # thus the source frame rate), regardless of the policy.
        drop = self._backpressure == "latest" and _is_live_source(url, options)
        # One slot of headroom (beyond the frame budget) reserved for a control
        # item, so end of stream or error never has to evict a pending frame, in
        # either mode.
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue + 1)
        free = None if drop else threading.Semaphore(self._max_queue)
        stop = threading.Event()
        done = object()

        def _offer_frame_drop(frame: Frame) -> None:
            # "latest": keep at most max_queue frames (shed the oldest when the
            # consumer lags); the reserved slot is left free for a control item.
            if queue.qsize() >= self._max_queue:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(frame)

        def _emit_frame(frame: Frame) -> None:
            if drop:
                loop.call_soon_threadsafe(_offer_frame_drop, frame)
                return
            # "all": block the worker until the consumer frees a slot.
            while not free.acquire(timeout=0.1):
                if stop.is_set():
                    return
            loop.call_soon_threadsafe(queue.put_nowait, frame)

        def _emit_control(item: Any) -> None:
            # The reserved slot guarantees this lands without evicting a frame.
            loop.call_soon_threadsafe(queue.put_nowait, item)

        policy = self._retry
        resume = self._resume

        def _worker() -> None:
            attempt = 0
            last_pts: float | None = None
            while not stop.is_set():
                progressed = False
                opts = options
                if attempt and resume == "seek" and last_pts is not None:
                    opts = {**options, "_resume_pts": last_pts}
                try:
                    for frame in read(url, options=opts, stop=stop):
                        if stop.is_set():
                            break
                        progressed = True
                        if frame.pts is not None:
                            last_pts = frame.pts
                        _emit_frame(frame)
                    break  # clean end of stream
                except BaseException as exc:
                    if stop.is_set():
                        break
                    # A read that produced frames before failing earned a fresh
                    # retry budget, so a minutes-long stream is not capped by a
                    # hiccup early on.
                    if progressed:
                        attempt = 0
                    if _is_transient_media(exc) and attempt < policy.retries:
                        time.sleep(policy.delay(attempt))
                        attempt += 1
                        continue  # re-open: page sources re-resolve a fresh URL
                    if _is_transient_media(exc):  # retries spent: normalized error
                        _emit_control(
                            TransportError(
                                "STREAM",
                                redact_url(url),
                                status=_media_status(exc),
                                attempts=attempt + 1,
                                detail=redact_url(str(exc)),
                            )
                        )
                        break
                    # Fatal: surface a MediaError with credentials scrubbed from
                    # the message (the engine may echo the source URL). Don't
                    # chain the original; its message can hold the unredacted URL.
                    _emit_control(MediaError(f"{type(exc).__name__}: {redact_url(str(exc))}"))
                    break
            _emit_control(done)

        thread = threading.Thread(target=_worker, name="thingctx-media", daemon=True)
        thread.start()
        try:
            while True:
                item = await queue.get()
                if item is done:
                    return
                if isinstance(item, BaseException):
                    raise item
                if free is not None:
                    free.release()
                yield item
        finally:
            stop.set()
