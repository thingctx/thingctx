# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Response chaining: drive a follow-up call from an action's response.

Handle-then-act: one call returns the address for the next (a ``Location`` header
or a JSON field), and a second call goes there. Resumable upload, presigned-URL
upload, async-job polling, and S3-style multipart share this shape. A TD form
declares the handoff with an ``x-thingctx-next`` annotation and the engine here
runs it, so these stay plain ``client.invoke`` actions.

Annotation (on a form)::

    "x-thingctx-next": {
      "from": "header:Location",          # or "json:upload.url" (dotted, [i] ok)
      "allowOrigins": ["storage.example"],  # extra hosts the next URL may target
      "carry": ["Authorization"],            # auth headers to forward to it
      "follow": { ...one mode... }
    }

Follow modes:

* op      - one request to the next URL: ``{"op": "PUT", "body": "{media}",
            "contentType": "application/octet-stream", "next": {...}}``
* resumable - chunked/resumable PUT (Layer 2): ``{"transport": "resumable",
            "media": "{media}", "chunkSize": 8388608}``
* poll    - GET until terminal: ``{"op": "GET", "until": {"path": "status",
            "in": ["DONE", "FAILED"]}, "error": {"path": "status",
            "equals": "FAILED"}, "interval": 2.0, "timeout": 300}``

Security: by default a next URL is only followed, and ``carry`` auth only
forwarded, when it is same-origin with the initiate URL. A cross-origin next URL
(e.g. a presigned storage host) must be named in ``allowOrigins`` (or the
``allow_origins`` call argument), and auth is forwarded cross-origin only when
``carry`` explicitly lists it. This keeps a server-provided URL from turning into
a credential leak.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO
from urllib.parse import urljoin, urlsplit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    import httpx

from thingctx.bindings.base import _decode
from thingctx.bindings.builtin.http import _http_body
from thingctx.netpolicy import WEB_SCHEMES, check_url, confine_path
from thingctx.reliability import TransportError

DEFAULT_CHUNK = 8 * 1024 * 1024  # 8 MiB, a multiple of the 256 KiB resumable-upload unit

# A chain is HTTP(S) end to end: a next address extracted from a response may
# only be an http(s) URL, never a jump to file:/data:/another scheme.
_CHAIN_SCHEMES = WEB_SCHEMES

# Bound a chain so a TD (or a server that keeps handing back another "next")
# cannot drive an unbounded number of hops. Override with the env var.
DEFAULT_MAX_HOPS = 20

# A download without an explicit cap still stops before it can exhaust memory or
# disk. Default 2 GiB; set the env var to a byte count, or to 0 to disable.
DEFAULT_MAX_DOWNLOAD = 2 * 1024 * 1024 * 1024

# A poll never spins: even interval 0 waits this long between requests so a large
# timeout cannot turn into a tight request loop.
MIN_POLL_INTERVAL = 0.05


def _max_hops() -> int:
    v = os.environ.get("THINGCTX_MAX_CHAIN_HOPS")
    return int(v) if v and v.strip().isdigit() else DEFAULT_MAX_HOPS


def _max_download_bytes() -> int | None:
    v = os.environ.get("THINGCTX_MAX_DOWNLOAD_BYTES")
    if v is None:
        return DEFAULT_MAX_DOWNLOAD
    v = v.strip()
    if v in ("", "0"):
        return None  # explicitly disabled
    return int(v)


def _download_base() -> str | None:
    """Directory a chained download must stay inside, from
    ``THINGCTX_DOWNLOAD_DIR`` (unset means only the symlink guard applies)."""
    return os.environ.get("THINGCTX_DOWNLOAD_DIR") or None


# A str is accepted (a filesystem path or a file:// URL) so an agent driving a
# chained upload over MCP can pass a path handle as plain JSON; it is coerced to a
# Path before any I/O.
Media = bytes | bytearray | str | Path | BinaryIO


class ChainError(RuntimeError):
    """A response-chained action could not complete its handoff."""


_DEFAULT_PORT = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urlsplit(url)
    scheme = (p.scheme or "").lower()
    return scheme, (p.hostname or "").lower(), p.port or _DEFAULT_PORT.get(scheme)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def same_origin(a: str, b: str) -> bool:
    """Whether two URLs share scheme, host, and port."""
    return _origin(a) == _origin(b)


def _split_path(path: str) -> list[str | int]:
    """Parse a dotted JSON path with optional ``[i]`` indices: ``a.b[0].c`` ->
    ``['a', 'b', 0, 'c']``."""
    path = path.strip()
    if path.startswith("$."):
        path = path[2:]
    elif path.startswith("$"):
        path = path[1:]
    parts: list[str | int] = []
    for seg in path.split("."):
        if not seg:
            continue
        name = seg.split("[", 1)[0]  # the segment up to the first index bracket
        if name:
            parts.append(name)
        parts.extend(int(idx) for idx in re.findall(r"\[(\d+)\]", seg))
    return parts


def json_path(body: Any, path: str) -> Any:
    """Read a value out of a decoded JSON body by dotted path, or None."""
    cur = body
    for part in _split_path(path):
        if isinstance(part, int):
            if not isinstance(cur, list | tuple) or part >= len(cur):
                return None
            cur = cur[part]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
    return cur


def extract_next(spec_from: str, result: Any) -> Any:
    """Pull the next address out of a response per ``from``: ``header:<Name>``
    (case-insensitive) or ``json:<dotted.path>``."""
    kind, _, sel = spec_from.partition(":")
    if kind == "header":
        want = sel.strip().lower()
        for key, value in (result.headers or {}).items():
            if key.lower() == want:
                return value
        return None
    if kind == "json":
        return json_path(result.body, sel)
    raise ChainError(f"unknown next source {spec_from!r} (use 'header:Name' or 'json:path')")


def _matches(body: Any, cond: dict) -> bool:
    """Evaluate a structured poll condition against a body: ``{path, equals}`` /
    ``{path, in: [...]}`` / ``{path, truthy: true}``."""
    value = json_path(body, cond.get("path", ""))
    if "equals" in cond:
        return bool(value == cond["equals"])
    if "in" in cond:
        return value in (cond["in"] or [])
    if "truthy" in cond:
        return bool(value) is bool(cond["truthy"])
    return False


def _carry_headers(auth_headers: dict, carry: list | None) -> dict:
    """The auth headers to forward: all of them when ``carry`` is unset, else the
    named subset (case-insensitive)."""
    if carry is None:
        return dict(auth_headers)
    sel = {c.lower() for c in carry}
    return {k: v for k, v in auth_headers.items() if k.lower() in sel}


def _template_name(tmpl: Any) -> str | None:
    if isinstance(tmpl, str) and len(tmpl) > 2 and tmpl[0] == "{" and tmpl[-1] == "}":
        return tmpl[1:-1]
    return None


def _resolve_template(tmpl: Any, arguments: dict) -> Any:
    """A ``"{name}"`` template resolves to ``arguments[name]`` (so a raw body or
    media object passes through unencoded); anything else is returned verbatim."""
    name = _template_name(tmpl)
    if name is not None:
        return (arguments or {}).get(name)
    return tmpl


def _follow_arg_names(spec: dict) -> set[str]:
    """Argument names a follow step pulls from (its ``body``/``media`` templates,
    across nested hops). These are held back from the initiate request body so a
    media payload is not also POSTed into the handoff call."""
    names: set[str] = set()
    stack = [spec or {}]
    while stack:
        follow = (stack.pop() or {}).get("follow") or {}
        for key in ("body", "media", "dest"):
            name = _template_name(follow.get(key))
            if name:
                names.add(name)
        if follow.get("next"):
            stack.append(follow["next"])
    return names


def _signer(binding: Any, signers: list) -> Callable[[Any], Awaitable[None]] | None:
    if not signers:
        return None

    async def sign(req: Any) -> None:
        await binding._sign_request(signers, req)

    return sign


async def run_chain(
    client: Any,
    action: Any,
    form: Any,
    arguments: dict | None,
    *,
    allow_origins: tuple[str, ...] = (),
) -> Any:
    """Drive a form that declares ``x-thingctx-next``: invoke it, read the next
    address from the response, and run the declared follow-up. Auth is resolved
    the same way :meth:`ThingClient.invoke` does."""
    binding = client.http_binding()
    if binding is None:
        raise ChainError("response chaining needs an http binding registered")
    owner = getattr(action, "thing_id", None)
    spec = form.raw.get("x-thingctx-next") or {}
    import dataclasses

    href, rest = form.fill(arguments or {})
    initiate = dataclasses.replace(form, href=href) if href != form.href else form
    # Hold media/body args back from the initiate body; they belong to the
    # follow-up call, not the handoff request.
    held = _follow_arg_names(spec)
    init_args = {k: v for k, v in rest.items() if k not in held}
    result = await binding._invoke_send(action, initiate, init_args, return_response=True)
    return await _follow(client, binding, owner, initiate.href, result, spec, rest, allow_origins)


async def _follow(
    client: Any,
    binding: Any,
    owner: str | None,
    initiate_url: str,
    result: Any,
    spec: dict,
    arguments: dict,
    allow_origins: tuple[str, ...],
    depth: int = 0,
) -> Any:
    if depth > _max_hops():
        raise ChainError(f"response chain exceeded {_max_hops()} hops")
    src = spec.get("from")
    if not src:
        raise ChainError("x-thingctx-next needs a 'from' (header:Name or json:path)")
    nxt = extract_next(src, result)
    if not nxt:
        raise ChainError(f"no next address from {src!r} (status {result.status})")
    nxt = urljoin(initiate_url, str(nxt))  # a Location may be relative
    # A chain is HTTP(S) end to end: refuse a next address that jumps to another
    # scheme (file:, data:, ...) before it reaches the transport.
    check_url(nxt, allowed_schemes=_CHAIN_SCHEMES, what="chain next URL")

    allowed = tuple(spec.get("allowOrigins") or ()) + tuple(allow_origins)
    allowed_same = same_origin(initiate_url, nxt)
    allowed_cross = _host(nxt) in {h.lower() for h in allowed}
    if not (allowed_same or allowed_cross):
        raise ChainError(
            f"next URL host {_host(nxt)!r} is cross-origin and not allowlisted; "
            "add it to x-thingctx-next.allowOrigins or allow_origins"
        )

    follow = spec.get("follow") or {}
    carry = spec.get("carry")
    headers_a, params_a, signers, cert = await binding._prepare(owner, None)
    # Forward auth to the next URL only when it is same-origin, or cross-origin
    # but the TD explicitly named the headers to carry.
    forward_auth = allowed_same or (allowed_cross and carry is not None)
    fwd_headers = _carry_headers(headers_a, carry) if forward_auth else {}
    fwd_signers = signers if forward_auth else []
    fwd_params = params_a if allowed_same else None

    if follow.get("transport") == "resumable":
        return await _follow_resumable(
            binding, nxt, follow, arguments, fwd_headers, fwd_signers, cert
        )
    if follow.get("transport") == "ranged-get":
        return await _follow_ranged(binding, nxt, follow, arguments, fwd_headers, fwd_signers, cert)
    if "until" in follow:
        return await _follow_poll(binding, nxt, follow, fwd_headers, fwd_signers, fwd_params, cert)

    # op mode: a single request to the next URL.
    op = (follow.get("op") or "PUT").upper()
    body_val = _resolve_template(follow.get("body"), arguments)
    if body_val is not None:
        body_kwargs, body_headers, is_stream = _http_body(follow.get("contentType"), body_val)
    else:
        body_kwargs, body_headers, is_stream = {}, {}, False
    res = await binding._send(
        op,
        nxt,
        signers=fwd_signers,
        cert=cert,
        headers={**fwd_headers, **body_headers},
        params=fwd_params,
        retry=not is_stream,
        return_response=True,
        **body_kwargs,
    )
    nested = follow.get("next")
    if nested:
        return await _follow(
            client, binding, owner, nxt, res, nested, arguments, allow_origins, depth + 1
        )
    return res.body


async def _follow_resumable(
    binding: Any,
    session_url: str,
    follow: dict,
    arguments: dict,
    headers: dict,
    signers: list,
    cert: Any,
) -> Any:
    """Layer 2: hand the session URL to the resumable chunk/resume transport."""
    import httpx

    media = _resolve_template(follow.get("media"), arguments)
    if media is None:
        raise ChainError("resumable follow needs a 'media' argument")
    pooled = cert is None
    http = binding._pool() if pooled else httpx.AsyncClient(timeout=binding._timeout, cert=cert)
    try:
        return await _resumable_put(
            http,
            session_url,
            media,
            media_type=follow.get("mediaType", "application/octet-stream"),
            base_headers=headers,
            chunk_size=int(follow.get("chunkSize", DEFAULT_CHUNK)),
            sign=_signer(binding, signers),
        )
    finally:
        if not pooled:
            await http.aclose()


async def _follow_ranged(
    binding: Any,
    url: str,
    follow: dict,
    arguments: dict,
    headers: dict,
    signers: list,
    cert: Any,
) -> Any:
    """Resumable download: GET the next URL in Range slices, resuming from the
    last received byte on a dropped connection. With ``dest`` the bytes stream to
    a file; otherwise the assembled bytes are returned."""
    import httpx

    dest = _resolve_template(follow.get("dest"), arguments)
    if dest is not None:
        # A download destination comes from a caller/LLM argument; keep it from
        # escaping through a symlink or (when a download dir is configured)
        # outside it.
        dest = str(confine_path(dest, base=_download_base()))
    pooled = cert is None
    http = binding._pool() if pooled else httpx.AsyncClient(timeout=binding._timeout, cert=cert)
    try:
        return await _ranged_get(
            http,
            url,
            headers=headers,
            chunk_size=int(follow.get("chunkSize", DEFAULT_CHUNK)),
            sign=_signer(binding, signers),
            dest=dest,
            retries=int(follow.get("retries", 3)),
            backoff=float(follow.get("backoff", 0.2)),
            max_bytes=_max_download_bytes(),
        )
    finally:
        if not pooled:
            await http.aclose()


async def _follow_poll(
    binding: Any,
    url: str,
    follow: dict,
    headers: dict,
    signers: list,
    params: dict | None,
    cert: Any,
) -> Any:
    """Poll the next URL until a terminal condition (the async-job pattern)."""
    op = (follow.get("op") or "GET").upper()
    interval = max(MIN_POLL_INTERVAL, float(follow.get("interval", 2.0)))
    timeout = float(follow.get("timeout", 300.0))
    until = follow.get("until") or {}
    error = follow.get("error")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        res = await binding._send(
            op,
            url,
            signers=signers,
            cert=cert,
            headers=headers,
            params=params,
            return_response=True,
        )
        if error and _matches(res.body, error):
            raise ChainError(f"polled action reported failure: {error}")
        if _matches(res.body, until):
            return res.body
        if loop.time() >= deadline:
            raise ChainError(f"polling {_host(url)!r} timed out after {timeout}s")
        await asyncio.sleep(interval)


# --- chunked transfer transports (resumable upload, ranged download) --------
#
# The byte phase behind the resumable and ranged-get follow modes. Both stream
# in chunks so a large transfer survives a dropped connection: upload resumes on
# a 308 between chunks, download resumes from the last received byte. Reached only
# through the follow modes above, never directly.

_RETRY_STATUS = (408, 429, 500, 502, 503, 504)


def _coerce_media(media: Media) -> bytes | bytearray | Path | BinaryIO:
    """Turn a str media into a Path: a ``file://`` URL maps to its filesystem
    path, anything else is taken as a path. Non-str media passes through. Never
    returns a ``str``, so callers narrow to the seekable/readable members."""
    if isinstance(media, str):
        if media.startswith("file://"):
            from urllib.request import url2pathname

            return Path(url2pathname(urlsplit(media).path))
        return Path(media)
    return media


def _media_size(media: Media) -> int:
    """Total byte length of the media, from memory, the filesystem, or a
    seekable stream."""
    media = _coerce_media(media)
    if isinstance(media, bytes | bytearray):
        return len(media)
    if isinstance(media, Path):
        return media.stat().st_size
    cur = media.tell()
    media.seek(0, os.SEEK_END)
    end = media.tell()
    media.seek(cur, os.SEEK_SET)
    return end - cur


def _chunks(media: Media, chunk_size: int) -> Iterator[bytes]:
    """Yield the media body in ``chunk_size`` slices without buffering the whole
    thing (except when it is already ``bytes`` in memory)."""
    media = _coerce_media(media)
    if isinstance(media, bytes | bytearray):
        view = memoryview(media)
        for start in range(0, len(view), chunk_size):
            yield bytes(view[start : start + chunk_size])
        return
    if isinstance(media, Path):
        with media.open("rb") as fh:
            while True:
                block = fh.read(chunk_size)
                if not block:
                    return
                yield block
    while True:
        block = media.read(chunk_size)
        if not block:
            return
        yield block


async def _send_signed(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    sign: Callable[[Any], Awaitable[None]] | None = None,
) -> httpx.Response:
    """Build, optionally sign, and send one request. A request is built (rather
    than ``client.request``) so an auth signer can run on it, and ``params`` is
    never passed so a URL's existing query survives."""
    kwargs: dict[str, Any] = {}
    if headers is not None:
        kwargs["headers"] = headers
    if content is not None:
        kwargs["content"] = content
    req = client.build_request(method, url, **kwargs)
    if sign is not None:
        await sign(req)
    return await client.send(req)


async def _resumable_put(
    client: httpx.AsyncClient,
    session_uri: str,
    media: Media,
    *,
    media_type: str,
    base_headers: dict[str, str],
    chunk_size: int,
    sign: Callable[[Any], Awaitable[None]] | None = None,
) -> Any:
    """Upload: PUT ``media`` to an already-open session URI in ``chunk_size``
    blocks with ``Content-Range`` headers, finalizing on a 2xx. A ``308`` means
    resume incomplete (keep going). Returns the finalized response body."""

    total = _media_size(media)
    offset = 0
    last = None
    for block in _chunks(media, chunk_size):
        end = offset + len(block) - 1
        put_headers = {
            **base_headers,
            "Content-Type": media_type,
            "Content-Range": f"bytes {offset}-{end}/{total}",
        }
        last = await _send_signed(
            client, "PUT", session_uri, headers=put_headers, content=block, sign=sign
        )
        offset = end + 1
        if last.status_code in (200, 201):
            return _decode(last)
        if last.status_code == 308:  # resume incomplete: continue
            continue
        if last.is_error:
            raise TransportError(
                "PUT", session_uri, status=last.status_code, attempts=1, detail=last.text[:200]
            )
    if total == 0 and last is None:
        # Zero-byte media: finalize with an empty terminal PUT.
        last = await _send_signed(
            client,
            "PUT",
            session_uri,
            headers={**base_headers, "Content-Type": media_type, "Content-Range": "bytes */0"},
            content=b"",
            sign=sign,
        )
    if last is None or last.is_error:
        status = getattr(last, "status_code", None)
        raise TransportError(
            "PUT", session_uri, status=status, attempts=1, detail="upload did not finalize"
        )
    return _decode(last)


def _range_total(content_range: str) -> int | None:
    """Total size from a ``Content-Range: bytes 0-0/12345`` header, or None when
    the server reports an unknown total (``/*``)."""
    tail = (content_range or "").split("/")[-1].strip()
    return int(tail) if tail.isdigit() else None


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    sign: Callable[[Any], Awaitable[None]] | None,
    retries: int,
    backoff: float,
) -> httpx.Response:
    """GET ``url`` once, retrying transient transport errors and retryable
    statuses with bounded backoff. A connection dropped mid-body raises, so the
    caller re-requests the same range, resuming from the last completed byte."""
    import httpx

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await _send_signed(client, "GET", url, headers=headers, sign=sign)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(backoff * (2**attempt))
                continue
            raise TransportError("GET", url, attempts=attempt + 1, cause=exc) from exc
        if resp.status_code in _RETRY_STATUS and attempt < retries:
            await asyncio.sleep(backoff * (2**attempt))
            continue
        if resp.is_error:
            raise TransportError(
                "GET", url, status=resp.status_code, attempts=attempt + 1, detail=resp.text[:200]
            )
        return resp
    raise TransportError("GET", url, attempts=retries + 1, cause=last_exc)


async def _ranged_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    chunk_size: int,
    sign: Callable[[Any], Awaitable[None]] | None = None,
    dest: str | None = None,
    retries: int = 3,
    backoff: float = 0.2,
    max_bytes: int | None = None,
) -> Any:
    """Download: GET ``url`` in ``Range`` slices, resuming from the last received
    byte when a chunk fails. Probes with ``bytes=0-0``: a ``206`` reports the
    total in ``Content-Range`` and confirms range support; a ``200`` means the
    server ignored the range, so the body just fetched is returned whole (no
    resume). With ``dest`` the bytes stream to that file and a ``{path, bytes,
    contentType}`` descriptor is returned; otherwise the assembled bytes."""
    probe = await _get_with_retry(
        client,
        url,
        headers={**headers, "Range": "bytes=0-0"},
        sign=sign,
        retries=retries,
        backoff=backoff,
    )
    ctype = probe.headers.get("content-type")
    total = (
        _range_total(probe.headers.get("content-range", "")) if probe.status_code == 206 else None
    )
    if probe.status_code != 206 or total is None:
        # No usable range support: the probe already holds the full body.
        return _deliver(dest, probe.content, ctype, max_bytes=max_bytes)

    if max_bytes is not None and total > max_bytes:
        raise ChainError(
            f"download of {total} bytes exceeds the {max_bytes}-byte limit "
            "(raise THINGCTX_MAX_DOWNLOAD_BYTES or set it to 0 to disable)"
        )
    # The sink stays open across the download loop and is closed in the finally
    # below; a `with` cannot span the conditional open, and the one local-file
    # open is not the blocking network I/O ASYNC230 targets.
    sink = Path(dest).open("wb") if dest is not None else None  # noqa: SIM115, ASYNC230
    buf = bytearray() if dest is None else None
    received = 0
    try:
        offset = 0
        while offset < total:
            end = min(offset + chunk_size, total) - 1
            resp = await _get_with_retry(
                client,
                url,
                headers={**headers, "Range": f"bytes={offset}-{end}"},
                sign=sign,
                retries=retries,
                backoff=backoff,
            )
            block = resp.content
            # buf and sink are mutually exclusive: buf accumulates in memory when
            # there is no dest, sink streams to the file when there is.
            if buf is not None:
                buf.extend(block)
            else:
                assert sink is not None  # noqa: S101 (buf/sink invariant, not a runtime check)
                sink.write(block)
            received += len(block)
            if max_bytes is not None and received > max_bytes:
                raise ChainError(
                    f"download exceeded the {max_bytes}-byte limit "
                    "(raise THINGCTX_MAX_DOWNLOAD_BYTES or set it to 0 to disable)"
                )
            # Advance only by what the server actually returned, so a short read
            # resumes from the true last byte rather than skipping a gap.
            offset += len(block) if block else (end - offset + 1)
    finally:
        if sink is not None:
            sink.close()
    if dest is not None:
        return {"path": str(dest), "bytes": received, "contentType": ctype}
    assert buf is not None  # noqa: S101 (dest is None here, so buf accumulated the body)
    return bytes(buf)


def _deliver(
    dest: str | None, data: bytes, ctype: str | None, *, max_bytes: int | None = None
) -> Any:
    if max_bytes is not None and len(data) > max_bytes:
        raise ChainError(
            f"download of {len(data)} bytes exceeds the {max_bytes}-byte limit "
            "(raise THINGCTX_MAX_DOWNLOAD_BYTES or set it to 0 to disable)"
        )
    if dest is not None:
        Path(dest).write_bytes(data)
        return {"path": str(dest), "bytes": len(data), "contentType": ctype}
    return data
