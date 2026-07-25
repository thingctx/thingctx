# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""HttpBinding: drive a Thing over http(s)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json as _json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from urllib.request import url2pathname

from thingctx.auth import AuthRegistry, AuthStrategy, apply_http
from thingctx.bindings.base import AuthMixin, ProtocolBinding
from thingctx.contracts import implements
from thingctx.lifecycle import ActionStatus, status_from_body
from thingctx.netpolicy import check_url, confine_path, resolve_and_pin
from thingctx.reliability import IDEMPOTENT_METHODS, RetryPolicy, TransportError, _retry_after

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpcore
    import httpx

    from thingctx.thing import WoTAction, WoTForm, WoTProperty


_UNSET: Any = object()  # sentinel: "no timeout override", distinct from None


def _make_pinning_backend(delegate: Any) -> Any:
    """Wrap an httpcore network backend so it dials a pre-validated IP instead of
    re-resolving the hostname at connect time.

    This is the socket-level half of the DNS-rebinding fix: the binding resolves
    and validates a host once (:func:`~thingctx.netpolicy.resolve_and_pin`),
    records ``{hostname: validated_ip}`` on the backend, and every ``connect_tcp``
    for a pinned host substitutes the validated IP. The URL keeps the hostname, so
    the Host header, TLS SNI, and certificate verification all still run against
    the hostname; only the address the OS dials is pinned. An unpinned host
    (``block_private`` off, or a host with no recorded pin) connects normally."""
    # optional dep, kept local so the core imports without the extra
    import httpcore  # noqa: PLC0415

    class _PinningBackend(httpcore.AsyncNetworkBackend):
        def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
            self._inner = inner
            self._pins: dict[str, str] = {}

        def pin(self, host: str, ip: str) -> None:
            self._pins[host] = ip

        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            return await self._inner.connect_tcp(
                self._pins.get(host, host),
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

        async def connect_unix_socket(
            self, path: str, timeout: float | None = None, socket_options: Any = None
        ) -> httpcore.AsyncNetworkStream:
            return await self._inner.connect_unix_socket(
                path, timeout=timeout, socket_options=socket_options
            )

        async def sleep(self, seconds: float) -> None:
            await self._inner.sleep(seconds)

    return _PinningBackend(delegate)


def _decode(resp: httpx.Response, empty: Any = None) -> Any:
    """Decode an HTTP response by its content type: JSON to a value, text to a
    str, anything else (e.g. an image) to raw bytes. An empty body returns
    ``empty``."""
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


async def _aiter_file(fh: Any, chunk: int = _STREAM_CHUNK) -> AsyncIterator[Any]:
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


async def _aiter_sync(it: Any) -> AsyncIterator[Any]:
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
    httpx ``files=`` accepts. A part assembled from JSON can only carry a str.

    Security: only an explicit ``file://`` URL is read from disk. A plain str is
    sent verbatim as inline content, never treated as a path to read, so a
    caller (or a model driving this action) cannot turn an arbitrary string into
    a local-file read. When ``THINGCTX_FS_ROOT`` is set the ``file://`` target
    is confined under it (and a symlink at the target is refused); otherwise the
    explicit path is read as given. bytes and a file-like object pass through; a
    ``Path`` is read (it can only originate in-process, not from JSON args)."""
    if isinstance(content, bytes | bytearray) or _is_filelike(content):
        return content
    if isinstance(content, Path):
        return content.read_bytes()
    if isinstance(content, str):
        if content.startswith("file://"):
            raw = url2pathname(urlsplit(content).path)
            base = (os.environ.get("THINGCTX_FS_ROOT") or "").strip() or None
            return confine_path(raw, base=base).read_bytes()
        return content.encode("utf-8")
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
        # A ``+json`` structured suffix (RFC 6839) is JSON-encoded like plain
        # json, but its media type is significant to the server (a PATCH with
        # ``application/merge-patch+json`` vs ``application/json`` is a
        # different operation), so the declared type must reach the wire; httpx
        # would otherwise stamp ``application/json`` from the ``json=`` kwarg.
        extra: dict[str, str] = (
            {"Content-Type": content_type} if content_type and ct.endswith("+json") else {}
        )
        return {"json": arguments}, extra, False
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
    # Reached only for a non-empty, non-json ``ct``, so ``content_type`` is a
    # concrete media type here (an empty/None type normalizes to "" and takes
    # the json branch above); send it as the raw body's Content-Type.
    raw_headers: dict[str, str] = {"Content-Type": content_type} if content_type else {}
    return {"content": _as_content(body)}, raw_headers, _is_stream_body(body)


def _merge_href_query(url: str, params: dict | None) -> tuple[str, dict | None]:
    """Preserve a query string declared in the form href. httpx drops a URL's
    existing query whenever ``params=`` is given (even ``params={}``), so a
    TD-declared ``?part=...`` would vanish. Split the href's query out and fold
    it under ``params`` (so call-time params win), returning the query-stripped
    url and the merged mapping. A href with no query is returned unchanged."""

    parts = urlsplit(url)
    if not parts.query:
        return url, params
    href_q = dict(parse_qsl(parts.query, keep_blank_values=True))
    merged = {**href_q, **(params or {})}
    return urlunsplit(parts._replace(query="")), merged


@implements(ProtocolBinding)
class HttpBinding(AuthMixin):
    """The HTTP(S) transport, covering the whole WoT TD 1.1 control plane:
    invoke, read/write, bulk read/write, the async action lifecycle, and subscribe
    over SSE. Auth resolves through :class:`AuthMixin` and applies via
    ``apply_http``. ``digest`` and ``combo`` are parsed but not yet applied.

    Transient failures (connection errors, timeouts, 429, 5xx) retry with bounded
    exponential backoff; any non-2xx surfaces as one ``TransportError``. Retries
    are gated to idempotent methods unless ``retry_non_idempotent`` is set, so a
    write is never silently re-sent.
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
        self._client: httpx.AsyncClient | None = None
        # This binding also claims https.
        self.schemes = ("http", "https")

    async def _prepare(
        self, owner_id: str | None = None, form: WoTForm | None = None
    ) -> tuple[dict[str, Any], dict[str, Any], list[Any], Any]:
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
    async def _sign_request(signers: list[Any], request: httpx.Request) -> None:
        """Run any request-signer callables on the assembled request. A signer
        may be sync or async."""

        for sign in signers:
            result = sign(request)
            if inspect.isawaitable(result):
                await result

    def _new_client(self, *, timeout: Any = _UNSET, **kwargs: Any) -> httpx.AsyncClient:
        """Build an ``AsyncClient``. When ``block_private`` is on, its network
        backend is swapped for one that dials pre-validated IPs, so a later
        :meth:`_pin` call binds the socket to the address that was checked."""
        # optional dep, kept local so the core imports without the extra
        import httpx  # noqa: PLC0415

        client = httpx.AsyncClient(
            timeout=self._timeout if timeout is _UNSET else timeout, **kwargs
        )
        if self._block_private:
            pool = getattr(getattr(client, "_transport", None), "_pool", None)
            # Only a real AsyncHTTPTransport carries a connection pool with a
            # network backend to swap. A test/custom transport (MockTransport) has
            # no socket layer, so there is nothing to pin; _pin falls back to a
            # resolve-time check there.
            if pool is not None and hasattr(pool, "_network_backend"):
                pool._network_backend = _make_pinning_backend(pool._network_backend)
        return client

    def _pin(self, client: httpx.AsyncClient, url: str, *, what: str = "request URL") -> str | None:
        """Resolve+validate the URL host once and pin the connection to that IP.

        Returns the hostname to use as the TLS SNI / cert name (so verification
        runs against the hostname, not the pinned IP). When the client's transport
        has no pinning backend (a mock or custom transport with no real socket),
        there is nothing to pin, so this falls back to a resolve-time private-host
        check: the host is still refused if it resolves private, and no SNI is
        returned. Gated by the caller on ``block_private``."""
        host = urlsplit(url).hostname or ""
        backend: Any = getattr(
            getattr(getattr(client, "_transport", None), "_pool", None), "_network_backend", None
        )
        if not hasattr(backend, "pin"):
            # No socket to rebind (mock/custom transport): keep the private-host
            # refusal via a resolve-check, but do not claim a pin was set.
            check_url(url, block_private=True, resolve=True, what=what)
            return None
        ip = resolve_and_pin(host, what=what)
        backend.pin(host, ip)
        return host

    def _pool(self) -> httpx.AsyncClient:
        """The lazily-created, reused client (created inside the running loop so
        it binds to the right event loop; recreated if closed)."""
        if self._client is None or self._client.is_closed:
            self._client = self._new_client()
        return self._client

    async def aclose(self) -> None:
        """Close the pooled client and its connections. Safe to call twice."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> HttpBinding:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _send(
        self,
        method: str,
        url: str,
        *,
        signers: list[Any],
        cert: Any,
        empty: Any = None,
        retry: bool = True,
        return_response: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Send a request with retries and normalize the outcome: a non-2xx
        becomes a ``TransportError`` (the shape a transport-level failure raises),
        the body is decoded by content type. Rebuilt and re-signed each attempt.

        ``retry=False`` forces a single attempt regardless of method, used when
        the body is a one-shot stream (a file or iterator) that a rebuilt
        request could not re-send.

        The pooled client serves the common case; when a per-owner client
        certificate is present a short-lived client is used instead, since mTLS
        is owner-specific and cannot share the pool."""

        # optional dep, kept local so the core imports without the extra
        import httpx  # noqa: PLC0415

        # Keep any query the form href declares (httpx would drop it once
        # params= is passed). Layer call-time params on top.
        new_url, merged = _merge_href_query(url, kwargs.get("params"))
        if new_url != url:
            url = new_url
            kwargs["params"] = merged

        # Always enforce the scheme allowlist. When block_private is on, resolve
        # and validate the host once, then pin the connection to that exact IP so
        # the address that was checked is the address dialed (the DNS-rebinding
        # fix; check_url alone re-resolves at connect time).
        check_url(url, block_private=False, what="request URL")

        retryable = retry and (method.upper() in IDEMPOTENT_METHODS or self._retry_non_idempotent)
        max_retries = self._policy.retries if retryable else 0
        pooled = cert is None
        client = self._pool() if pooled else self._new_client(cert=cert)
        sni = self._pin(client, url) if self._block_private else None
        try:
            for attempt in range(max_retries + 1):
                req = client.build_request(method, url, **kwargs)
                if sni is not None:
                    req.extensions["sni_hostname"] = sni
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
                    with contextlib.suppress(Exception):
                        detail = resp.text[:200]
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

    async def invoke(self, action: WoTAction, form: WoTForm, arguments: dict[str, Any]) -> Any:
        return await self._invoke_send(action, form, arguments)

    async def _invoke_send(
        self,
        action: WoTAction,
        form: WoTForm,
        arguments: dict[str, Any],
        *,
        return_response: bool = False,
    ) -> Any:
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

    async def read(self, prop: WoTProperty, form: WoTForm) -> Any:
        """GET the property's current value from its form URL."""
        headers, params, signers, cert = await self._prepare(getattr(prop, "thing_id", None), form)
        return await self._send(
            "GET", form.href, signers=signers, cert=cert, headers=headers, params=params
        )

    async def write(self, prop: WoTProperty, form: WoTForm, value: Any) -> Any:
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

    async def read_all(self, thing: Any, form: WoTForm, names: Any = None) -> Any:
        """GET a Thing-level bulk-property form. ``names`` (when given) selects a
        subset via a ``props`` query parameter (the ``readmultipleproperties``
        op); the response is a ``{name: value}`` object."""
        headers, params, signers, cert = await self._prepare(getattr(thing, "id", None), form)
        if names:
            params = {**params, "props": ",".join(names)}
        return await self._send(
            "GET", form.href, signers=signers, cert=cert, headers=headers, params=params
        )

    async def write_all(self, thing: Any, form: WoTForm, values: dict[str, Any]) -> Any:
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

    async def invoke_async(
        self, action: WoTAction, form: WoTForm, arguments: dict[str, Any]
    ) -> ActionStatus:
        """Start a long-running action. POST returns 201/202 with a status body
        carrying the status resource ``href``; map it to an ``ActionStatus``."""

        owner = action.thing_id
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
        return status_from_body(body, form, thing_id=owner, name=action.name)

    async def query_action(self, status: Any) -> ActionStatus:
        """GET the action's status resource (the ``queryaction`` op)."""

        form = status.form
        owner = status.thing_id
        headers, params, signers, cert = await self._prepare(owner, form)
        body = await self._send(
            "GET", status.href, signers=signers, cert=cert, headers=headers, params=params
        )
        return status_from_body(body, form, href=status.href, thing_id=owner, name=status.name)

    async def cancel_action(self, status: Any) -> ActionStatus:
        """DELETE the action's status resource (the ``cancelaction`` op)."""

        form = status.form
        owner = status.thing_id
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
        return status_from_body(body, form, href=status.href, thing_id=owner, name=status.name)

    async def subscribe(
        self, target: Any, form: WoTForm, args: dict[str, Any] | None = None
    ) -> AsyncIterator[Any]:
        """Subscribe over Server-Sent Events (the HTTP streaming binding for
        events / observable properties). Yields each ``data:`` payload as it
        arrives. ``target`` is the affordance, so the stream authenticates as
        its owner; ``args`` are sent as query parameters (an event's
        ``subscription`` schema, e.g. a filter).

        A denied or failed subscription (a 4xx/5xx response, e.g. a 401 for a
        missing or wrong token) raises ``TransportError`` when iteration begins,
        rather than yielding an empty stream that looks valid. A 2xx stream that
        simply carries no events yet does not raise."""

        # optional dep, kept local so the core imports without the extra
        import httpx  # noqa: PLC0415

        owner = getattr(target, "thing_id", None)
        headers, params, signers, cert = await self._prepare(owner, form)
        if args:
            params = {**params, **args}
        url, merged_params = _merge_href_query(form.href, params)
        check_url(url, block_private=False, what="SSE URL")

        # An SSE stream is long-lived, so there is no overall read timeout, but
        # connection setup (and writes) stay bounded so a peer cannot wedge the
        # client just by never completing the handshake.
        sse_timeout = httpx.Timeout(self._timeout, read=None)

        async def _stream() -> AsyncIterator[Any]:
            async with self._new_client(timeout=sse_timeout, cert=cert) as client:
                # Same DNS-rebinding pin as _send: resolve+validate once, dial that
                # IP, verify TLS against the hostname.
                sni = self._pin(client, url, what="SSE URL") if self._block_private else None
                req = client.build_request("GET", url, headers=headers, params=merged_params)
                if sni is not None:
                    req.extensions["sni_hostname"] = sni
                await self._sign_request(signers, req)
                resp = await client.send(req, stream=True)
                # Fail loud on a denied or failed subscription (401/403/5xx):
                # without this the stream would iterate to an empty end and look
                # like a valid stream that never emits. A normally-empty 2xx
                # stream is fine and must not raise. Read the (usually small)
                # error body for the detail before closing the stream.
                if resp.is_error:
                    detail = ""
                    with contextlib.suppress(Exception):
                        detail = (await resp.aread()).decode("utf-8", "replace")[:200]
                    await resp.aclose()
                    raise TransportError("GET", url, status=resp.status_code, detail=detail)
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
