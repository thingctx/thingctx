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
    ) -> None:
        self._headers = headers or {}
        self._init_auth(
            credentials=credentials,
            auth=auth,
            extra_auth=extra_auth,
            timeout=timeout,
            allow_insecure_oauth=allow_insecure_oauth,
        )
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

    async def invoke(self, action, form, arguments):  # noqa: ANN001
        import httpx

        headers, params, signers, cert = await self._prepare(getattr(action, "thing_id", None))
        # HTTP binding: honor the form's declared method, else default by
        # safety. Idempotent (safe) actions GET with args as query params;
        # others POST with a JSON body.
        method = form.raw.get("htv:methodName")
        if method is None:
            method = "GET" if getattr(action, "idempotent", False) else "POST"
        async with httpx.AsyncClient(timeout=self._timeout, cert=cert) as client:
            if method.upper() == "GET":
                req = client.build_request(
                    "GET", form.href, headers=headers, params={**params, **arguments}
                )
            else:
                req = client.build_request(
                    method, form.href, json=arguments, headers=headers, params=params
                )
            await self._sign_request(signers, req)
            resp = await client.send(req)
            resp.raise_for_status()
            return _decode(resp)

    async def read(self, prop, form):  # noqa: ANN001
        """GET the property's current value from its form URL."""
        import httpx

        headers, params, signers, cert = await self._prepare(getattr(prop, "thing_id", None))
        async with httpx.AsyncClient(timeout=self._timeout, cert=cert) as client:
            req = client.build_request("GET", form.href, headers=headers, params=params)
            await self._sign_request(signers, req)
            resp = await client.send(req)
            resp.raise_for_status()
            return _decode(resp)

    async def write(self, prop, form, value):  # noqa: ANN001
        """PUT the new value to the property's form URL (the ``writeproperty``
        HTTP binding default)."""
        import httpx

        headers, params, signers, cert = await self._prepare(getattr(prop, "thing_id", None))
        async with httpx.AsyncClient(timeout=self._timeout, cert=cert) as client:
            req = client.build_request(
                "PUT", form.href, json=value, headers=headers, params=params
            )
            await self._sign_request(signers, req)
            resp = await client.send(req)
            resp.raise_for_status()
            return _decode(resp, empty={"ok": True})

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
