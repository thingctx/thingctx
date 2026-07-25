# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""HttpBinding: drive a Thing over http(s)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from thingctx.auth import AuthRegistry, AuthStrategy, apply_http
from thingctx.bindings.base import AuthMixin, ProtocolBinding
from thingctx.contracts import implements


def _decode(resp, empty=None):
    """Decode an HTTP response by its content type: JSON to a value, text to a
    str, anything else (e.g. an image) to raw bytes. An empty body returns
    ``empty``. HTTP-specific (reads content-type/content); it lives here, not in
    the transport-neutral binding base."""
    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
    # An empty body (a 204 No Content, or any empty 2xx) has nothing to parse,
    # even when the response still declares a JSON content type. Check this first
    # so a successful empty response never raises a decode error.
    if not resp.content:
        return empty
    if ctype == "application/json" or ctype.endswith("+json"):
        return resp.json()
    if ctype.startswith("text/") or ctype == "":
        return resp.text
    return resp.content


@dataclass
class HttpResult:
    """A response surfaced to a caller that needs more than the decoded body:
    the status and headers (e.g. to read a ``Location`` for response chaining)
    and the final request URL. ``invoke`` returns the bare body; the chaining
    engine asks for this shape with ``return_response=True``."""

    status: int
    headers: dict[str, str]
    body: Any
    url: str


def _norm_ct(content_type: str | None) -> str:
    """The media type with any parameters (``; charset=...``) stripped, lowered."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _is_filelike(v: Any) -> bool:
    return hasattr(v, "read")


def _is_stream_body(v: Any) -> bool:
    """Whether a body value is consumed once (a file object, a ``Path`` opened
    into one, or an iterator), so a retry could not re-send it. In-memory
    ``bytes``/``str`` are reusable."""
    if isinstance(v, bytes | bytearray | str):
        return False
    if isinstance(v, Path) or _is_filelike(v):
        return True
    return hasattr(v, "__iter__") or hasattr(v, "__aiter__")


def _raw_body_value(arguments: Any) -> Any:
    """The single value to send as a raw request body. A mapping must name it
    under ``body`` or carry exactly one entry; anything else is sent verbatim."""
    if isinstance(arguments, dict):
        if "body" in arguments:
            return arguments["body"]
        vals = list(arguments.values())
        if len(vals) == 1:
            return vals[0]
        raise ValueError(
            "a raw request body needs a single value: pass {'body': <bytes|path|file>} "
            "or a single-key mapping (content type is not json/form/multipart)"
        )
    return arguments


_STREAM_CHUNK = 1024 * 1024  # 1 MiB read size for streamed bodies


async def _aiter_file(fh: Any, chunk: int = _STREAM_CHUNK):
    """Stream a (sync) file object in blocks, closing it when done. httpx's
    ``AsyncClient`` rejects a plain sync file as ``content=``, so a file body is
    bridged through this async generator instead of being read into memory."""
    try:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            yield block
    finally:
        close = getattr(fh, "close", None)
        if callable(close):
            close()


async def _aiter_sync(it: Any):
    for block in it:
        yield block


def _as_content(v: Any) -> Any:
    """Coerce a raw-body value to something httpx's ``AsyncClient`` accepts as
    ``content=``. bytes/str pass through; a ``Path`` or sync file object is
    streamed via an async generator (not buffered); a sync iterator is bridged
    to async; an async iterator passes through."""
    if isinstance(v, bytes | bytearray | str):
        return v
    if isinstance(v, Path):
        return _aiter_file(v.open("rb"))
    if _is_filelike(v):
        return _aiter_file(v)
    if hasattr(v, "__aiter__"):
        return v
    if hasattr(v, "__iter__"):
        return _aiter_sync(v)
    return v


def _is_file_part(v: Any) -> bool:
    """Whether a multipart field is a file (vs a scalar form field)."""
    if isinstance(v, bytes | bytearray | Path) or _is_filelike(v):
        return True
    return isinstance(v, tuple | list) and 2 <= len(v) <= 3


def _part_content(content: Any) -> Any:
    """Coerce the content element of a multipart part to bytes/file-like that
    httpx ``files=`` accepts. A part assembled from JSON can only carry a str,
    so a str is read from disk when it names an existing file (or a ``file://``
    URL, matching the media ``{media}`` path), else taken as inline text. bytes
    and a file-like object pass through; a ``Path`` is read."""
    if isinstance(content, bytes | bytearray) or _is_filelike(content):
        return content
    if isinstance(content, Path):
        return content.read_bytes()
    if isinstance(content, str):
        path: Path | None = None
        if content.startswith("file://"):
            from urllib.parse import urlsplit
            from urllib.request import url2pathname

            path = Path(url2pathname(urlsplit(content).path))
        else:
            try:
                cand = Path(content)
                if cand.is_file():
                    path = cand
            except (OSError, ValueError):  # not a usable path (e.g. embedded NUL)
                path = None
        return path.read_bytes() if path is not None else content.encode("utf-8")
    return content


def _file_part(v: Any) -> Any:
    """Normalize a file field to what httpx ``files=`` accepts."""
    if isinstance(v, Path):
        return (v.name, v.open("rb"))
    # A ``[filename, content, content-type?]`` part (e.g. from JSON, where it
    # arrives as a list): coerce the content element so a str/path is sent as
    # bytes rather than handed to httpx, which would call ``.read()`` on it.
    if isinstance(v, tuple | list) and 2 <= len(v) <= 3:
        filename, content, *rest = v
        return (filename, _part_content(content), *rest)
    return v


def _http_body(
    content_type: str | None, arguments: Any
) -> tuple[dict[str, Any], dict[str, str], bool]:
    """Map an action's arguments onto httpx body kwargs per the form's declared
    content type. Returns ``(send_kwargs, extra_headers, is_stream)``.

    * json (default, ``application/json`` or ``*+json``): ``json=arguments``;
    * ``application/x-www-form-urlencoded``: ``data=arguments``;
    * ``multipart/form-data``: split into ``files=`` (bytes/Path/file-like/tuple)
      and ``data=`` (scalars);
    * anything else (``application/octet-stream``, ``image/*``, ``video/*`` ...):
      a single raw ``content=`` body, with the ``Content-Type`` header set.

    ``is_stream`` is true when the body is consumed once (a file or iterator),
    so the caller can disable retries for that send."""
    ct = _norm_ct(content_type)
    if ct in ("", "application/json") or ct.endswith("+json"):
        return {"json": arguments}, {}, False
    if ct == "application/x-www-form-urlencoded":
        return {"data": arguments}, {}, False
    if ct == "multipart/form-data":
        files: dict[str, Any] = {}
        data: dict[str, Any] = {}
        for key, value in (arguments or {}).items():
            if _is_file_part(value):
                files[key] = _file_part(value)
            else:
                data[key] = value
        kwargs: dict[str, Any] = {}
        if files:
            kwargs["files"] = files
        if data:
            kwargs["data"] = data
        return kwargs, {}, bool(files)
    body = _raw_body_value(arguments)
    return {"content": _as_content(body)}, {"Content-Type": content_type}, _is_stream_body(body)


def _merge_href_query(url: str, params: dict | None) -> tuple[str, dict | None]:
    """Preserve a query string declared in the form href. httpx drops a URL's
    existing query whenever ``params=`` is given (even ``params={}``), so a
    TD-declared ``?part=...`` would vanish. Split the href's query out and fold
    it under ``params`` (so call-time params win), returning the query-stripped
    url and the merged mapping. A href with no query is returned unchanged."""
    from urllib.parse import parse_qsl, urlsplit, urlunsplit

    parts = urlsplit(url)
    if not parts.query:
        return url, params
    href_q = dict(parse_qsl(parts.query, keep_blank_values=True))
    merged = {**href_q, **(params or {})}
    return urlunsplit(parts._replace(query="")), merged


@implements(ProtocolBinding)
class HttpBinding(AuthMixin):
    """POST the action input as JSON to the form's http(s) URL.

    Honors declared security via the transport-neutral auth layer: it resolves
    each owner's schemes into neutral credential material (see
    :class:`AuthMixin`) and maps it onto the request with ``apply_http`` --
    headers, query params, a client certificate, or request signing. No auth
    logic lives in this transport.

    Transient failures (connection errors, timeouts, 429, 5xx) are retried with
    bounded exponential backoff, and any non-2xx outcome surfaces as a single
    ``TransportError``. Retries are gated to idempotent methods unless
    ``retry_non_idempotent`` is set, so a write is never silently re-sent. A
    pooled client is reused across calls to keep connections warm.

    Capability coverage: this is the reference transport and implements the whole
    control-plane contract, so every optional WoT TD 1.1 capability is exercised
    here. ``invoke`` (incl. ``contentCoding`` negotiation and the declared
    ``additionalResponses`` error shape on failure); ``read`` / ``write``; bulk
    ``read_all`` / ``write_all`` over a Thing-level form; the async action
    lifecycle ``invoke_async`` / ``query_action`` / ``cancel_action`` (POST a
    201/202 + status resource, then GET / DELETE it); and ``subscribe`` over SSE.
    Auth covers the schemes ``apply_http`` maps: bearer, basic, apikey, oauth2
    (resolved to a bearer token), mutual TLS, and request signing (``auto``,
    e.g. SigV4). ``digest`` and ``combo`` are modeled by the parser but not yet
    applied here.
    """

    scheme = "http"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        headers: dict | None = None,
        credentials: dict | None = None,
        allow_insecure_oauth: bool = False,
        auth: AuthRegistry | None = None,
        extra_auth: list[AuthStrategy] | None = None,
        retries: int = 2,
        backoff: float = 0.2,
        retry_non_idempotent: bool = False,
        block_private: bool = False,
    ) -> None:
        from thingctx.reliability import RetryPolicy

        # Refuse requests whose host is, or resolves to, a private, loopback,
        # or link-local address (see thingctx.netpolicy). Off by default: a WoT
        # client legitimately drives LAN devices. A gateway processing
        # untrusted TDs turns it on to close SSRF to metadata/internal hosts.
        self._block_private = block_private
        self._headers = headers or {}
        self._init_auth(
            credentials=credentials,
            auth=auth,
            extra_auth=extra_auth,
            timeout=timeout,
            allow_insecure_oauth=allow_insecure_oauth,
        )
        self._retry_non_idempotent = retry_non_idempotent
        self._policy = RetryPolicy(retries=retries, backoff=backoff)
        # One pooled AsyncClient, created lazily inside the running loop and
        # reused across calls so connections (and TLS handshakes) stay warm.
        self._client = None
        # This binding also claims https.
        self.schemes = ("http", "https")

    async def _prepare(self, owner_id: str | None = None, form=None):
        """Resolve the owner's credentials and map them onto HTTP.

        Returns ``(headers, params, signers, cert)``: headers/params to merge
        before the request is built, signers to run on the assembled request,
        and an optional client-level mTLS ``cert``. A form may carry its own
        security, which overrides the owner's for that affordance."""
        creds = await self._resolve_credentials(owner_id, form)
        plan = apply_http(creds, base_headers=self._headers)
        headers = plan.headers
        # Honor the form's declared contentCoding by requesting it; httpx
        # transparently decompresses a gzip/deflate response body.
        coding = getattr(form, "content_coding", None) if form is not None else None
        if coding:
            headers = {**headers, "Accept-Encoding": coding}
        return headers, plan.params, plan.signers, plan.cert

    @staticmethod
    async def _sign_request(signers, request) -> None:
        """Run any request-signer callables on the assembled request. A signer
        may be sync or async."""
        import inspect

        for sign in signers:
            result = sign(request)
            if inspect.isawaitable(result):
                await result

    def _pool(self):
        """The lazily-created, reused client (created inside the running loop so
        it binds to the right event loop; recreated if closed)."""
        import httpx

        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the pooled client and its connections. Safe to call twice."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> HttpBinding:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _send(
        self, method, url, *, signers, cert, empty=None, retry=True, return_response=False, **kwargs
    ):
        """Build, sign, and send a request with retries, then normalize the
        outcome: a non-2xx becomes a ``TransportError`` (the same shape a
        transport-level failure raises), and the body is decoded by content
        type. A request is rebuilt and re-signed on each attempt.

        ``retry=False`` forces a single attempt regardless of method, used when
        the body is a one-shot stream (a file or iterator) that a rebuilt
        request could not re-send.

        The pooled client serves the common case; when a per-owner client
        certificate is present a short-lived client is used instead, since mTLS
        is owner-specific and cannot share the pool."""
        import asyncio

        import httpx

        from thingctx.reliability import (
            IDEMPOTENT_METHODS,
            TransportError,
            _retry_after,
        )

        # Keep any query the form href declares (httpx would drop it once
        # params= is passed). Layer call-time params on top.
        new_url, merged = _merge_href_query(url, kwargs.get("params"))
        if new_url != url:
            url = new_url
            kwargs["params"] = merged

        if self._block_private:
            from thingctx.netpolicy import check_url

            check_url(url, block_private=True, what="request URL")

        retryable = retry and (method.upper() in IDEMPOTENT_METHODS or self._retry_non_idempotent)
        max_retries = self._policy.retries if retryable else 0
        pooled = cert is None
        client = self._pool() if pooled else httpx.AsyncClient(timeout=self._timeout, cert=cert)
        try:
            for attempt in range(max_retries + 1):
                req = client.build_request(method, url, **kwargs)
                await self._sign_request(signers, req)
                try:
                    resp = await client.send(req)
                except httpx.TransportError as exc:  # connection + timeout errors
                    if attempt < max_retries:
                        await asyncio.sleep(self._policy.delay(attempt))
                        continue
                    raise TransportError(method, url, attempts=attempt + 1, cause=exc) from exc
                if resp.status_code in self._policy.retry_statuses and attempt < max_retries:
                    await asyncio.sleep(_retry_after(resp, self._policy, attempt))
                    continue
                if resp.is_error:
                    detail = ""
                    try:
                        detail = resp.text[:200]
                    except Exception:  # noqa: BLE001 - detail is best-effort
                        pass
                    raise TransportError(
                        method, url, status=resp.status_code, attempts=attempt + 1, detail=detail
                    )
                body = _decode(resp, empty=empty)
                if return_response:
                    return HttpResult(
                        status=resp.status_code,
                        headers=dict(resp.headers),
                        body=body,
                        url=str(req.url),
                    )
                return body
            raise AssertionError("unreachable")  # pragma: no cover
        finally:
            if not pooled:
                await client.aclose()

    async def invoke(self, action, form, arguments):  # noqa: ANN001
        return await self._invoke_send(action, form, arguments)

    async def _invoke_send(self, action, form, arguments, *, return_response=False):  # noqa: ANN001
        """Drive an action's form and return its decoded body, or (with
        ``return_response``) an :class:`HttpResult` carrying status + headers so
        a caller can chain off the response (read a ``Location`` etc.)."""
        owner = getattr(action, "thing_id", None)
        headers, params, signers, cert = await self._prepare(owner, form)
        # HTTP binding: honor the form's declared method, else default by
        # safety. Idempotent (safe) actions GET with args as query params;
        # others send a body shaped by the form's contentType (json by default,
        # else form-encoded, multipart, or a raw/streamed binary body).
        method = form.raw.get("htv:methodName")
        if method is None:
            method = "GET" if getattr(action, "read_only", False) else "POST"
        if method.upper() == "GET":
            return await self._send(
                "GET",
                form.href,
                signers=signers,
                cert=cert,
                headers=headers,
                params={**params, **(arguments or {})},
                return_response=return_response,
            )
        body_kwargs, body_headers, is_stream = _http_body(
            getattr(form, "content_type", None), arguments
        )
        if body_headers:
            headers = {**headers, **body_headers}
        return await self._send(
            method,
            form.href,
            signers=signers,
            cert=cert,
            headers=headers,
            params=params,
            retry=not is_stream,
            return_response=return_response,
            **body_kwargs,
        )

    async def read(self, prop, form):  # noqa: ANN001
        """GET the property's current value from its form URL."""
        headers, params, signers, cert = await self._prepare(getattr(prop, "thing_id", None), form)
        return await self._send(
            "GET", form.href, signers=signers, cert=cert, headers=headers, params=params
        )

    async def write(self, prop, form, value):  # noqa: ANN001
        """PUT the new value to the property's form URL (the ``writeproperty``
        HTTP binding default)."""
        headers, params, signers, cert = await self._prepare(getattr(prop, "thing_id", None), form)
        return await self._send(
            "PUT",
            form.href,
            signers=signers,
            cert=cert,
            headers=headers,
            params=params,
            json=value,
            empty={"ok": True},
        )

    async def read_all(self, thing, form, names=None):  # noqa: ANN001
        """GET a Thing-level bulk-property form. ``names`` (when given) selects a
        subset via a ``props`` query parameter (the ``readmultipleproperties``
        op); the response is a ``{name: value}`` object."""
        headers, params, signers, cert = await self._prepare(getattr(thing, "id", None), form)
        if names:
            params = {**params, "props": ",".join(names)}
        return await self._send(
            "GET", form.href, signers=signers, cert=cert, headers=headers, params=params
        )

    async def write_all(self, thing, form, values):  # noqa: ANN001
        """PUT a ``{name: value}`` object to a Thing-level bulk-property form
        (the ``writeallproperties`` / ``writemultipleproperties`` op)."""
        headers, params, signers, cert = await self._prepare(getattr(thing, "id", None), form)
        return await self._send(
            "PUT",
            form.href,
            signers=signers,
            cert=cert,
            headers=headers,
            params=params,
            json=values,
            empty={"ok": True},
        )

    async def invoke_async(self, action, form, arguments):  # noqa: ANN001
        """Start a long-running action. POST returns 201/202 with a status body
        carrying the status resource ``href``; map it to an ``ActionStatus``."""
        from thingctx.lifecycle import status_from_body

        owner = getattr(action, "thing_id", None)
        headers, params, signers, cert = await self._prepare(owner, form)
        body = await self._send(
            "POST",
            form.href,
            signers=signers,
            cert=cert,
            headers=headers,
            params=params,
            json=arguments,
        )
        return status_from_body(body, form)

    async def query_action(self, status):  # noqa: ANN001
        """GET the action's status resource (the ``queryaction`` op)."""
        from thingctx.lifecycle import status_from_body

        form = status.form
        owner = getattr(form, "thing_id", None)
        headers, params, signers, cert = await self._prepare(owner, form)
        body = await self._send(
            "GET", status.href, signers=signers, cert=cert, headers=headers, params=params
        )
        return status_from_body(body, form, href=status.href)

    async def cancel_action(self, status):  # noqa: ANN001
        """DELETE the action's status resource (the ``cancelaction`` op)."""
        from thingctx.lifecycle import status_from_body

        form = status.form
        owner = getattr(form, "thing_id", None)
        headers, params, signers, cert = await self._prepare(owner, form)
        body = await self._send(
            "DELETE",
            status.href,
            signers=signers,
            cert=cert,
            headers=headers,
            params=params,
            empty={"status": "cancelled"},
        )
        return status_from_body(body, form, href=status.href)

    async def subscribe(self, target, form, args=None):  # noqa: ANN001
        """Subscribe over Server-Sent Events (the HTTP streaming binding for
        events / observable properties). Yields each ``data:`` payload as it
        arrives. ``target`` is the affordance, so the stream authenticates as
        its owner; ``args`` are sent as query parameters (an event's
        ``subscription`` schema, e.g. a filter)."""
        import json as _json

        import httpx

        owner = getattr(target, "thing_id", None)
        headers, params, signers, cert = await self._prepare(owner, form)
        if args:
            params = {**params, **args}
        url, params = _merge_href_query(form.href, params)
        if self._block_private:
            from thingctx.netpolicy import check_url

            check_url(url, block_private=True, what="SSE URL")

        # An SSE stream is long-lived, so there is no overall read timeout, but
        # connection setup (and writes) stay bounded so a peer cannot wedge the
        # client just by never completing the handshake.
        sse_timeout = httpx.Timeout(self._timeout, read=None)

        async def _stream():
            async with httpx.AsyncClient(timeout=sse_timeout, cert=cert) as client:
                req = client.build_request("GET", url, headers=headers, params=params)
                await self._sign_request(signers, req)
                resp = await client.send(req, stream=True)
                try:
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            raw = line[5:].strip()
                            try:
                                yield _json.loads(raw)
                            except ValueError:
                                yield raw
                finally:
                    await resp.aclose()

        return _stream()
