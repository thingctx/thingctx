"""HttpInvoker: drive a Thing over http(s)."""

from __future__ import annotations

from thingctx.auth import AuthRegistry, AuthStrategy, apply_http
from thingctx.invokers.base import _AuthBinding, _decode


class HttpInvoker(_AuthBinding):
    """POST the action input as JSON to the form's http(s) URL.

    Honors declared security via the transport-neutral auth layer: it resolves
    each owner's schemes into neutral credential material (see
    :class:`_AuthBinding`) and maps it onto the request with ``apply_http`` --
    headers, query params, a client certificate, or request signing. No auth
    logic lives in this transport.

    Transient failures (connection errors, timeouts, 429, 5xx) are retried with
    bounded exponential backoff, and any non-2xx outcome surfaces as a single
    ``TransportError``. Retries are gated to idempotent methods unless
    ``retry_non_idempotent`` is set, so a write is never silently re-sent. A
    pooled client is reused across calls to keep connections warm.
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
    ) -> None:
        from thingctx.reliability import RetryPolicy

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
        # This invoker also claims https.
        self.schemes = ("http", "https")

    async def _prepare(self, owner_id: str | None = None):
        """Resolve the owner's credentials and map them onto HTTP.

        Returns ``(headers, params, signers, cert)``: headers/params to merge
        before the request is built, signers to run on the assembled request,
        and an optional client-level mTLS ``cert``."""
        creds = await self._resolve_credentials(owner_id)
        plan = apply_http(creds, base_headers=self._headers)
        return plan.headers, plan.params, plan.signers, plan.cert

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

    async def __aenter__(self) -> HttpInvoker:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _send(self, method, url, *, signers, cert, empty=None, **kwargs):
        """Build, sign, and send a request with retries, then normalize the
        outcome: a non-2xx becomes a ``TransportError`` (the same shape a
        transport-level failure raises), and the body is decoded by content
        type. A request is rebuilt and re-signed on each attempt.

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

        retryable = method.upper() in IDEMPOTENT_METHODS or self._retry_non_idempotent
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
                return _decode(resp, empty=empty)
            raise AssertionError("unreachable")  # pragma: no cover
        finally:
            if not pooled:
                await client.aclose()

    async def invoke(self, action, form, arguments):  # noqa: ANN001
        headers, params, signers, cert = await self._prepare(getattr(action, "thing_id", None))
        # HTTP binding: honor the form's declared method, else default by
        # safety. Idempotent (safe) actions GET with args as query params;
        # others POST with a JSON body.
        method = form.raw.get("htv:methodName")
        if method is None:
            method = "GET" if getattr(action, "idempotent", False) else "POST"
        if method.upper() == "GET":
            return await self._send(
                "GET",
                form.href,
                signers=signers,
                cert=cert,
                headers=headers,
                params={**params, **arguments},
            )
        return await self._send(
            method,
            form.href,
            signers=signers,
            cert=cert,
            headers=headers,
            params=params,
            json=arguments,
        )

    async def read(self, prop, form):  # noqa: ANN001
        """GET the property's current value from its form URL."""
        headers, params, signers, cert = await self._prepare(getattr(prop, "thing_id", None))
        return await self._send(
            "GET", form.href, signers=signers, cert=cert, headers=headers, params=params
        )

    async def write(self, prop, form, value):  # noqa: ANN001
        """PUT the new value to the property's form URL (the ``writeproperty``
        HTTP binding default)."""
        headers, params, signers, cert = await self._prepare(getattr(prop, "thing_id", None))
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

    async def subscribe(self, name, form):  # noqa: ANN001
        """Subscribe over Server-Sent Events (the HTTP streaming binding for
        events / observable properties). Yields each ``data:`` payload as it
        arrives."""
        import json as _json

        import httpx

        headers, params, signers, cert = await self._prepare()

        async def _stream():
            async with httpx.AsyncClient(timeout=None, cert=cert) as client:
                req = client.build_request("GET", form.href, headers=headers, params=params)
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
