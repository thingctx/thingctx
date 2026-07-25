# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Media auth tests: the applier's credential to engine mapping, and end to end
resolution of an owning Thing's declared security through MediaBinding. All
offline (fake backend, no network, no codecs)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

from thingctx.auth import (
    ApiKeyCredential,
    BasicCredential,
    BearerToken,
    ClientCertificate,
    MediaAuthPlan,
    apply_media,
    av_auth_options,
    redact_url,
    ytdlp_auth_options,
)
from thingctx.bindings.builtin.media import Frame, MediaBinding, MediaError
from thingctx.runtime import ThingClient


class _RecordingBackend:
    def __init__(self) -> None:
        self.seen: dict = {}

    def can_open(self, url: str, hint: dict) -> bool:
        return True

    def read(self, url: str, *, options: dict, stop: threading.Event) -> Iterator[Frame]:
        self.seen["url"] = url
        self.seen["options"] = options
        yield Frame(data=0, kind=options.get("track", "video"), pts=0.0)

    def write(self, frames, target, *, options, stop):
        raise NotImplementedError


def _drain(client: ThingClient, name: str, args=None):
    async def run():
        return [f async for f in await client.frames(name, args)]

    return asyncio.run(run())


# applier: credential to neutral plan


def test_apply_media_maps_each_credential_kind():
    plan = apply_media(
        [
            BasicCredential(username="alice", password="s3cr3t"),
            BearerToken(token="tok"),
            ApiKeyCredential(name="X-Key", value="k", location="query"),
            ClientCertificate(certfile="/c.pem", keyfile="/k.pem", ca_certs="/ca.pem"),
        ]
    )
    assert plan.username == "alice"
    assert plan.password == "s3cr3t"
    assert plan.headers["Authorization"] == "Bearer tok"
    assert plan.query["X-Key"] == "k"
    assert plan.tls is not None and plan.tls.certfile == "/c.pem"
    assert plan.has_credentials


def test_empty_plan_has_no_credentials():
    assert apply_media([]).has_credentials is False


# no secret leaks: masked repr and redacted errors


def test_media_plan_repr_masks_every_secret():
    plan = apply_media(
        [
            BasicCredential(username="alice", password="s3cr3t"),
            BearerToken(token="tok-123"),
            ApiKeyCredential(name="X-Key", value="keyval", location="query"),
        ]
    )
    r = repr(plan)
    for leaked in ("alice", "s3cr3t", "tok-123", "keyval"):
        assert leaked not in r
    assert "***" in r


def test_libav_log_records_are_redacted():
    # FFmpeg output routed through the `libav` logger must have any credentialed
    # URL scrubbed, even if the host app raises the log level.
    import logging

    from thingctx.bindings.builtin.media.backends import _install_libav_redaction

    _install_libav_redaction()
    seen: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    root = logging.getLogger()
    cap = _Cap()
    root.addHandler(cap)
    prev = root.level
    root.setLevel(logging.DEBUG)
    try:
        logging.getLogger("libav.rtsp").error("Opening 'rtsp://u:TOKENXYZ@h:8554/c' for reading")
    finally:
        root.removeHandler(cap)
        root.setLevel(prev)

    assert seen
    assert "TOKENXYZ" not in seen[0]
    assert "rtsp://***@h:8554/c" in seen[0]


def test_redact_url_scrubs_userinfo_and_sensitive_query():
    assert redact_url("boom 'rtsp://user:pa55@h:8554/c'") == "boom 'rtsp://***@h:8554/c'"
    assert redact_url("https://h/s.m3u8?token=abc123&x=1") == "https://h/s.m3u8?token=***&x=1"
    assert redact_url("srt://h?passphrase=zzz&pbkeylen=16") == "srt://h?passphrase=***&pbkeylen=16"
    # An RTMP ingest stream key rides as a path segment, not userinfo/query.
    assert redact_url("rtmp://a.rtmp.youtube.com/live2/xxxx-yyyy-zzzz") == (
        "rtmp://a.rtmp.youtube.com/live2/***"
    )
    assert redact_url("push to rtmps://live.twitch.tv/app/live_123_secret now") == (
        "push to rtmps://live.twitch.tv/app/*** now"
    )
    assert redact_url("nothing secret here") == "nothing secret here"


def test_media_errors_redact_credentials_end_to_end():
    # A backend that builds the credentialed URL (as PyAV does) then fails: the
    # surfaced error must be a MediaError with the secret scrubbed.
    class _BoomBackend:
        def can_open(self, url, hint):
            return True

        def read(self, url, *, options, stop):
            if False:  # makes this a generator without an unreachable statement
                yield
            plan = options.get("auth")
            if plan is not None:
                url, _ = av_auth_options(plan, url)
            raise RuntimeError(f"connect failed for {url!r}")

        def write(self, frames, target, *, options, stop):
            raise NotImplementedError

    client = ThingClient(
        tds=[_secured_td("rtsp://cam/stream")],
        bindings=[
            MediaBinding(backends=[_BoomBackend()], credentials={"cam1": ("alice", "s3cr3t")})
        ],
    )

    async def run():
        return [f async for f in await client.frames("cam1__watch")]

    with pytest.raises(MediaError) as ei:
        asyncio.run(run())
    msg = str(ei.value)
    assert "alice" not in msg and "s3cr3t" not in msg
    assert "rtsp://***@cam/stream" in msg


# applier: plan to engine options


def test_av_auth_options_embeds_userinfo_percent_encoded():
    plan = MediaAuthPlan(username="user", password="p@ss:w0rd")
    url, opts = av_auth_options(plan, "rtsp://127.0.0.1:8554/cam")
    # '@' and ':' in the password must be encoded so they don't break the URL.
    assert url == "rtsp://user:p%40ss%3Aw0rd@127.0.0.1:8554/cam"
    assert opts == {}


def test_av_auth_options_maps_headers_query_and_tls():
    plan = MediaAuthPlan(
        headers={"Authorization": "Bearer t"},
        query={"token": "z"},
        tls=ClientCertificate(certfile="/c.pem", keyfile="/k.pem", ca_certs="/ca.pem"),
    )
    url, opts = av_auth_options(plan, "https://host/live.m3u8?x=1")
    assert url == "https://host/live.m3u8?x=1&token=z"
    assert opts["headers"] == "Authorization: Bearer t\r\n"
    assert opts["cert_file"] == "/c.pem"
    assert opts["key_file"] == "/k.pem"
    assert opts["ca_file"] == "/ca.pem"


def test_ytdlp_auth_options_is_account_login():
    plan = MediaAuthPlan(username="u", password="p")
    assert ytdlp_auth_options(plan) == {"username": "u", "password": "p"}
    assert ytdlp_auth_options(MediaAuthPlan()) == {}


# end to end: declared security to resolved plan in options


def _secured_td(href: str, hint: dict | None = None) -> dict:
    form: dict = {"href": href}
    if hint is not None:
        form["x-thingctx-media"] = hint
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:cam1",
        "title": "cam",
        "securityDefinitions": {"basic_sc": {"scheme": "basic"}},
        "security": "basic_sc",
        "actions": {"watch": {"forms": [form]}},
    }


def test_basic_security_resolves_into_options_auth_plan():
    backend = _RecordingBackend()
    client = ThingClient(
        tds=[_secured_td("rtsp://cam/stream")],
        bindings=[MediaBinding(backends=[backend], credentials={"cam1": ("alice", "s3cr3t")})],
    )
    _drain(client, "cam1__watch")
    plan = backend.seen["options"]["auth"]
    assert isinstance(plan, MediaAuthPlan)
    assert plan.username == "alice"
    assert plan.password == "s3cr3t"


def test_declared_basic_without_secret_attaches_no_plan():
    # A TD that declares `basic` but is given no secret must not inject a bogus
    # "None" login; it resolves to no credential at all.
    backend = _RecordingBackend()
    client = ThingClient(
        tds=[_secured_td("rtsp://cam/stream")],
        bindings=[MediaBinding(backends=[backend])],  # no credentials supplied
    )
    _drain(client, "cam1__watch")
    assert "auth" not in backend.seen["options"]


def test_no_declared_security_attaches_no_plan():
    backend = _RecordingBackend()
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:cam1",
        "title": "cam",
        "actions": {"watch": {"forms": [{"href": "rtsp://cam/stream"}]}},
    }
    client = ThingClient(tds=[td], bindings=[MediaBinding(backends=[backend])])
    _drain(client, "cam1__watch")
    assert "auth" not in backend.seen["options"]


def test_extractor_builds_ytdlp_opts_from_plan_and_cookie_hint(monkeypatch):
    import sys
    import types

    captured: dict = {}

    class _FakeYDL:
        def __init__(self, opts):
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            captured["url"] = url
            return {"url": "https://cdn/resolved.m3u8"}

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = _FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    from thingctx.bindings.builtin.media.backends import ExtractorBackend

    resolved = ExtractorBackend()._extract(
        "https://site/v",
        {"auth": MediaAuthPlan(username="u", password="p"), "cookiefile": "/c.txt"},
    )
    assert resolved == [("https://cdn/resolved.m3u8", {})]
    assert captured["opts"]["username"] == "u"
    assert captured["opts"]["password"] == "p"
    assert captured["opts"]["cookiefile"] == "/c.txt"


def test_extractor_login_for_parameterized_page_source():
    # A parameterized "video pages" Thing with basic security: the account login
    # is resolved per owner and reaches the backend as a plan, unencoded.
    backend = _RecordingBackend()
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:dev:pages",
        "title": "pages",
        "securityDefinitions": {"basic_sc": {"scheme": "basic"}},
        "security": "basic_sc",
        "actions": {
            "watch": {
                "uriVariables": {"url": {"type": "string"}},
                "forms": [{"href": "{+url}", "x-thingctx-media": {"resolve": "page"}}],
            }
        },
    }
    client = ThingClient(
        tds=[td],
        bindings=[MediaBinding(backends=[backend], credentials={"pages": ("me", "pw")})],
    )
    _drain(client, "pages__watch", {"url": "https://www.youtube.com/watch?v=private"})
    plan = backend.seen["options"]["auth"]
    assert ytdlp_auth_options(plan) == {"username": "me", "password": "pw"}
    assert backend.seen["url"] == "https://www.youtube.com/watch?v=private"
