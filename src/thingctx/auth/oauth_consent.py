# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""One-time, interactive OAuth2 authorization-code consent (RFC 6749 + PKCE).

This is the out-of-band half of user-authorized OAuth: a human grants access in
a browser once, and the resulting refresh token is persisted so every later run
refreshes silently. It is deliberately separate from the provider's ``resolve``
path; nothing here ever runs as a side effect of invoking a Thing.

Desktop consent follows RFC 8252: a public client redirects to a loopback
address (``127.0.0.1`` on an ephemeral port) and proves possession with PKCE
(RFC 7636, S256), guarded by a ``state`` nonce. The token exchange uses the
standard library only, so consent needs no extra dependency.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

from thingctx.auth.store import TokenStore, default_token_store, token_key

__all__ = ["authorize_code_flow", "login"]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for PKCE S256: the challenge is the
    URL-safe, unpadded base64 of ``sha256(verifier)``."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorization_url(
    authorization_url: str,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...] | list[str],
    state: str,
    code_challenge: str,
    offline: bool = True,
    extra_params: dict[str, str] | None = None,
) -> str:
    """Compose the browser consent URL. ``offline`` adds the parameters that
    make a provider return a refresh token (``access_type=offline`` plus
    ``prompt=consent``, which Google requires and others ignore)."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if offline:
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    if extra_params:
        params.update(extra_params)
    sep = "&" if urllib.parse.urlparse(authorization_url).query else "?"
    return authorization_url + sep + urllib.parse.urlencode(params)


def _require_tls(url: str, allow_insecure: bool, *, what: str) -> None:
    """Refuse to hand a credential (an authorization code, or a client secret)
    to a non-https endpoint unless it is loopback or explicitly allowed. The
    authorization and token URLs come from configuration or a TD, so this stops a
    mistyped or hostile ``http://`` endpoint from receiving them in cleartext."""
    u = urllib.parse.urlparse(url)
    if u.scheme == "https" or allow_insecure:
        return
    if (u.hostname or "") in ("localhost", "127.0.0.1", "::1"):
        return
    raise ValueError(
        f"refusing to use non-https {what} {url!r}; use https, or pass "
        f"allow_insecure=True to override"
    )


def exchange_code(
    token_url: str,
    *,
    client_id: str,
    client_secret: str | None,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    timeout: float = 30.0,
    allow_insecure: bool = False,
) -> dict[str, Any]:
    """Exchange the authorization code for tokens (the access + refresh pair)."""
    _require_tls(token_url, allow_insecure, what="token endpoint")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    body = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(  # noqa: S310 - scheme guarded by _require_tls
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


_PAGE = (
    b"<html><body><h3>thingctx: authorization complete.</h3>You can close this tab.</body></html>"
)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        query = urllib.parse.urlparse(self.path).query
        params = dict(urllib.parse.parse_qsl(query))
        if "code" in params or "error" in params:
            self.server.result = params  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, *args: Any) -> None:  # silence the default stderr log
        return


def _await_redirect(
    authorization_url: str, *, open_browser: bool, timeout: float
) -> dict[str, str]:
    """Serve a single loopback redirect and return its query parameters. The
    caller must put ``state``/``code_challenge`` in ``authorization_url`` and
    verify ``state`` against the result."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.result = {}  # type: ignore[attr-defined]
    server.timeout = 1.0
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"
    url = authorization_url.replace("__REDIRECT__", urllib.parse.quote(redirect_uri, safe=""))
    print(f"thingctx: open this URL to authorize:\n  {url}\n")
    if open_browser:
        webbrowser.open(url)
    deadline = time.monotonic() + timeout
    try:
        while not server.result and time.monotonic() < deadline:  # type: ignore[attr-defined]
            server.handle_request()
    finally:
        server.server_close()
    result = dict(server.result)  # type: ignore[attr-defined]
    result["__redirect_uri__"] = redirect_uri
    return result


def authorize_code_flow(
    *,
    authorization_url: str,
    token_url: str,
    client_id: str,
    client_secret: str | None = None,
    scopes: tuple[str, ...] | list[str] = (),
    offline: bool = True,
    open_browser: bool = True,
    timeout: float = 300.0,
    extra_params: dict[str, str] | None = None,
    allow_insecure: bool = False,
) -> dict[str, Any]:
    """Run the interactive consent and return the token response (access token,
    refresh token, ``expires_in``, granted ``scope``). Raises on a denied or
    timed-out consent or a ``state`` mismatch.

    This opens a browser; call it only from an explicit operator action, never
    from a Thing invocation.
    """
    scopes = tuple(scopes)
    # The authorization URL receives client_id/redirect/state/PKCE and the token
    # URL receives the code and any client secret, so both must be https (or
    # loopback / explicitly allowed) before we send a browser or a request there.
    _require_tls(authorization_url, allow_insecure, what="authorization endpoint")
    _require_tls(token_url, allow_insecure, what="token endpoint")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    # The loopback port is only known after the server binds, so the redirect is
    # templated and filled in _await_redirect.
    auth_url = build_authorization_url(
        authorization_url,
        client_id=client_id,
        redirect_uri="__REDIRECT__",
        scopes=scopes,
        state=state,
        code_challenge=challenge,
        offline=offline,
        extra_params=extra_params,
    )
    result = _await_redirect(auth_url, open_browser=open_browser, timeout=timeout)
    if not result.get("code"):
        raise RuntimeError(
            f"authorization did not complete: {result.get('error') or 'timed out or denied'}"
        )
    if result.get("state") != state:
        raise RuntimeError("authorization state mismatch; aborting (possible CSRF)")
    tok = exchange_code(
        token_url,
        client_id=client_id,
        client_secret=client_secret,
        code=result["code"],
        redirect_uri=result["__redirect_uri__"],
        code_verifier=verifier,
        timeout=30.0,
        allow_insecure=allow_insecure,
    )
    if not tok.get("access_token"):
        raise RuntimeError("token endpoint returned no access token")
    return tok


def login(
    *,
    authorization_url: str,
    token_url: str,
    client_id: str,
    client_secret: str | None = None,
    scopes: tuple[str, ...] | list[str] = (),
    owner_id: str | None = None,
    store: TokenStore | None = None,
    offline: bool = True,
    open_browser: bool = True,
    timeout: float = 300.0,
    extra_params: dict[str, str] | None = None,
    allow_insecure: bool = False,
) -> dict[str, Any]:
    """Run consent and persist the refresh token so the provider can refresh
    silently later. ``owner_id`` is the Thing id the token authorizes (it keys
    the store the same way the provider does). Returns the stored record (no
    secret in its ``repr``-safe form is enforced here; treat it as sensitive)."""
    scopes = tuple(scopes)
    store = store if store is not None else default_token_store()
    tok = authorize_code_flow(
        authorization_url=authorization_url,
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        offline=offline,
        open_browser=open_browser,
        timeout=timeout,
        extra_params=extra_params,
        allow_insecure=allow_insecure,
    )
    refresh = tok.get("refresh_token")
    if not refresh:
        raise RuntimeError(
            "consent succeeded but the provider returned no refresh token; request "
            "offline access (offline=True) and a provider that issues one"
        )
    key = token_key(owner_id, token_url, scopes)
    record = {"refresh_token": refresh, "client_id": client_id, "scopes": list(scopes)}
    # A confidential client (e.g. an "installed"/desktop app) needs its secret to
    # refresh. Persist it next to the refresh token (an equally long-lived secret,
    # in the same 0600 store) so refresh is self-contained: any later run -- CLI,
    # library, or the MCP server -- resolves silently with no runtime credential
    # config. A public/PKCE client has no secret and this is simply absent.
    if client_secret:
        record["client_secret"] = client_secret
    store.set(key, record)
    return record
