# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Media applier: map neutral credential material onto a media source.

The media plane has two engines (a decoder and a page extractor), so this
produces one neutral :class:`MediaAuthPlan` and offers a per engine mapping for
each. As with the other appliers, all credential to engine knowledge lives here;
the backends just call the mapping for the engine they use and hold no auth
logic.

Mapping choices:
* ``BasicCredential`` becomes username and password (URL userinfo for a decoder;
  account login for the extractor).
* ``BearerToken`` or header ``ApiKeyCredential`` becomes request headers (for
  token bearing HTTP and HLS streams).
* query ``ApiKeyCredential`` becomes a URL query parameter (tokened stream URLs).
* ``ClientCertificate`` becomes TLS cert, key, and ca (for ``rtsps`` and TLS
  transports).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from thingctx.auth.credentials import (
    ApiKeyCredential,
    BasicCredential,
    BearerToken,
    ClientCertificate,
    Credential,
)

__all__ = [
    "MediaAuthPlan",
    "apply_media",
    "av_auth_options",
    "ytdlp_auth_options",
    "redact_url",
]

_REDACTED = "***"


@dataclass(repr=False)
class MediaAuthPlan:
    """Neutral media auth material, mapped onto an engine on demand.

    Holds plaintext only at the point of use (an engine needs it); its ``repr``
    masks every credential-bearing field so it is safe to log."""

    username: str | None = None
    password: str | None = None
    headers: dict = field(default_factory=dict)  # token-bearing HTTP/HLS streams
    query: dict = field(default_factory=dict)  # tokened stream URL params
    tls: ClientCertificate | None = None  # rtsps / TLS

    @property
    def has_credentials(self) -> bool:
        return any((self.username, self.password, self.headers, self.query, self.tls))

    def __repr__(self) -> str:
        user = _REDACTED if self.username else None
        pw = _REDACTED if self.password else None
        hdr = sorted(self.headers) if self.headers else []  # names only, no values
        qs = sorted(self.query) if self.query else []
        return (
            f"MediaAuthPlan(username={user!r}, password={pw!r}, "
            f"headers={hdr!r}, query={qs!r}, tls={self.tls!r})"
        )


# Credentials can ride inside a media URL (userinfo or a token query param), or a
# request header, and FFmpeg / extractors echo those into their error messages
# and logs. Redact before any media error or log line is surfaced.
# The userinfo match extends to the LAST '@' before a path or whitespace, so a
# password that itself contains '@' (e.g. ``p@ss:w0rd``) is fully redacted; the
# lookahead keeps a bare or path-boundary '@' from being consumed.
_USERINFO_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s]+@(?=[^/@\s])")
_SENSITIVE_QS = (
    "password",
    "passwd",
    "pass",
    "passphrase",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apikey",
    "key",
    "secret",
    "client_secret",
    "sig",
    "signature",
    "auth",
    "authorization",
    "code",
    "session",
    "sessionid",
    "jwt",
    "bearer",
    "credential",
    "credentials",
)
_QS_RE = re.compile(r"(?i)([?&](?:" + "|".join(_SENSITIVE_QS) + r")=)[^&\s'\"]+")
# An RTMP ingest stream key is a path segment (``rtmp://host/app/<key>``), not
# userinfo or a query value; keep ``scheme://host/app/`` and redact the rest.
_RTMP_KEY_RE = re.compile(r"(?i)(rtmps?://[^/\s]+/[^/\s]+/)[^\s'\"?#]+")
# FFmpeg takes request headers as CRLF-joined lines; a credential header (Bearer,
# api key, cookie) can echo into a libav log or an error. Redact the value after
# a sensitive header name, whether the lines are real CRLFs or a literal ``\r\n``.
_SENSITIVE_HDR = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "apikey",
    "x-auth-token",
    "x-access-token",
    "x-amz-security-token",
)
_HEADER_RE = re.compile(r"(?im)^([ \t]*(?:" + "|".join(_SENSITIVE_HDR) + r")[ \t]*:[ \t]*)[^\r\n]+")
_HEADER_LITERAL_RE = re.compile(
    r"(?i)((?:" + "|".join(_SENSITIVE_HDR) + r")[ \t]*:[ \t]*)(?:(?!\\r\\n).)+"
)


def redact_url(text: str) -> str:
    """Redact credentials in ``text``: URL userinfo (``scheme://user:pass@``
    becomes ``scheme://***@``), sensitive query values (``?token=...`` becomes
    ``?token=***``), an RTMP stream key path segment
    (``rtmp://host/app/key`` becomes ``rtmp://host/app/***``), and the value of a
    credential request header (``Authorization: Bearer ...`` becomes
    ``Authorization: ***``)."""
    if not text:
        return text
    text = _USERINFO_RE.sub(lambda m: m.group("scheme") + _REDACTED + "@", text)
    text = _QS_RE.sub(r"\1" + _REDACTED, text)
    text = _RTMP_KEY_RE.sub(r"\1" + _REDACTED, text)
    text = _HEADER_RE.sub(r"\1" + _REDACTED, text)
    return _HEADER_LITERAL_RE.sub(r"\1" + _REDACTED, text)


def apply_media(creds: list[Credential]) -> MediaAuthPlan:
    """Build a :class:`MediaAuthPlan` from neutral credential material."""
    plan = MediaAuthPlan()
    for c in creds:
        if isinstance(c, BasicCredential):
            plan.username = c.username.get_secret_value()
            plan.password = c.password.get_secret_value()
        elif isinstance(c, BearerToken):
            plan.headers["Authorization"] = f"{c.scheme} {c.token.get_secret_value()}"
        elif isinstance(c, ApiKeyCredential):
            if c.location == "query":
                plan.query[c.name] = c.value.get_secret_value()
            else:
                plan.headers[c.name] = c.value.get_secret_value()
        elif isinstance(c, ClientCertificate):
            plan.tls = c
        # SignatureCredential / RequestSigner / EnhancedAuth: no media mapping.
    return plan


def _with_userinfo(url: str, username: str, password: str) -> str:
    """Set (replacing any existing) percent encoded ``user:pass@`` userinfo on a
    URL; how a decoder carries credentials for these transports."""
    parts = urlsplit(url)
    user = quote(username, safe="")
    pw = quote(password, safe="")
    userinfo = f"{user}:{pw}" if pw else user
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    netloc = f"{userinfo}@{host}" if userinfo else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _with_query(url: str, extra: dict) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q.update({k: str(v) for k, v in extra.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def av_auth_options(plan: MediaAuthPlan, url: str) -> tuple[str, dict]:
    """Map a plan onto FFmpeg/PyAV: credentials in the URL userinfo, tokens in
    the query, bearer/key in request ``headers``, and TLS cert/key/ca. Returns
    the (possibly rewritten) URL and the av options to merge."""
    opts: dict = {}
    if plan.username or plan.password:
        url = _with_userinfo(url, plan.username or "", plan.password or "")
    if plan.query:
        url = _with_query(url, plan.query)
    if plan.headers:
        for k, v in plan.headers.items():
            if any(c in "\r\n" for c in str(k)) or any(c in "\r\n" for c in str(v)):
                raise ValueError(f"illegal newline in credential header {k!r}")
        opts["headers"] = "".join(f"{k}: {v}\r\n" for k, v in plan.headers.items())
    if plan.tls is not None:
        c = plan.tls
        if c.certfile:
            opts["cert_file"] = c.certfile
        if c.keyfile:
            opts["key_file"] = c.keyfile
        if c.ca_certs:
            opts["ca_file"] = c.ca_certs
    return url, opts


def ytdlp_auth_options(plan: MediaAuthPlan) -> dict:
    """Map a plan onto yt-dlp options: an account login for sites that gate
    content behind one. (Cookie-based access is a separate, non-credential
    extractor option.)"""
    opts: dict = {}
    if plan.username:
        opts["username"] = plan.username
    if plan.password:
        opts["password"] = plan.password
    return opts
