# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Abuse-case coverage for the safety guardrails: URL/scheme policy, download
and Thing-Description size caps, path confinement, OAuth transport security,
token-store symlink handling, and credential redaction in transport errors."""

from __future__ import annotations

import os

import httpx
import pytest

from thingctx.bindings import HttpBinding
from thingctx.chain import ChainError
from thingctx.netpolicy import (
    PolicyError,
    check_url,
    confine_path,
    is_private_host,
    require_scheme,
    resolve_and_pin,
)
from thingctx.reliability import TransportError
from thingctx.runtime import ThingClient

# --- netpolicy --------------------------------------------------------------


def test_require_scheme_rejects_non_web():
    with pytest.raises(PolicyError):
        require_scheme("file:///etc/passwd", {"http", "https"})
    assert require_scheme("https://ok/x", {"http", "https"}).startswith("https://")


def test_is_private_host_ip_literals():
    for host in ("127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.1.1", "::1", "0.0.0.0"):
        assert is_private_host(host), host
    for host in ("93.184.216.34", "example.com", ""):
        assert not is_private_host(host), host


@pytest.mark.parametrize(
    "encoded",
    [
        "2130706433",  # decimal 127.0.0.1
        "0x7f000001",  # hex 127.0.0.1
        "0x7f.0.0.1",  # mixed hex-dotted
        "017700000001",  # octal 127.0.0.1
        "127.1",  # short form 127.0.0.1
        "0177.0.0.1",  # octal first octet
        "::ffff:127.0.0.1",  # IPv4-mapped IPv6 loopback
        "::ffff:169.254.169.254",  # IPv4-mapped cloud metadata
    ],
)
def test_non_canonical_ip_encodings_still_blocked(encoded):
    # invariant NET-4: a loopback or metadata address written in a non-dotted form
    # (decimal, hex, octal, short, IPv4-mapped) is canonicalized before the
    # private-host check, so it does not slip past block_private as a "hostname".
    assert is_private_host(encoded), encoded
    # An IPv6 literal in a URL authority must be bracketed for urlsplit to read it.
    host = f"[{encoded}]" if ":" in encoded else encoded
    with pytest.raises(PolicyError, match="private or loopback"):
        check_url(f"http://{host}/latest/meta-data/", block_private=True, resolve=False)


def test_check_url_block_private_opt_in():
    # Off by default: a LAN device is legitimate for a WoT client.
    check_url("http://192.168.1.5/td", block_private=False)
    with pytest.raises(PolicyError):
        check_url("http://192.168.1.5/td", block_private=True)


def test_resolve_and_pin_returns_a_public_literal_as_is():
    # invariant NET-3: a public IP literal is validated and returned unchanged, so
    # a caller can pin the socket straight to it.
    assert resolve_and_pin("93.184.216.34") == "93.184.216.34"


def test_resolve_and_pin_refuses_a_private_literal():
    # invariant NET-3: a private/loopback literal is refused before any connect.
    with pytest.raises(PolicyError, match="private or loopback"):
        resolve_and_pin("127.0.0.1")


def test_resolve_and_pin_refuses_when_resolution_fails(monkeypatch):
    # invariant NET-3: a name that cannot be resolved is refused (fail closed),
    # never falling through to an unpinned connect that would re-resolve unchecked.
    import socket as _socket

    def _boom(host, *a, **k):
        raise OSError("no such host")

    monkeypatch.setattr(_socket, "getaddrinfo", _boom)
    with pytest.raises(PolicyError, match="could not be resolved"):
        resolve_and_pin("nonexistent.invalid")


def test_resolve_and_pin_refuses_a_name_that_resolves_private(monkeypatch):
    # invariant NET-3: a name whose resolved address is private is refused, even
    # when the name itself is not a literal (the rebind / split-horizon case).
    import socket as _socket

    def _fake(host, *a, **k):
        return [(2, 1, 6, "", ("10.0.0.5", 0))]  # a private A-record

    monkeypatch.setattr(_socket, "getaddrinfo", _fake)
    with pytest.raises(PolicyError, match="private or loopback"):
        resolve_and_pin("internal.example")


def test_resolve_and_pin_returns_the_resolved_public_ip(monkeypatch):
    # invariant NET-3: a name that resolves to a public address returns that exact
    # address, so the socket pins to the validated IP.
    import socket as _socket

    def _fake(host, *a, **k):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(_socket, "getaddrinfo", _fake)
    assert resolve_and_pin("public.example") == "93.184.216.34"


def test_resolve_and_pin_refuses_when_no_address_resolves(monkeypatch):
    # invariant NET-3: getaddrinfo returning an empty set is refused, not treated
    # as "not private" and allowed through.
    import socket as _socket

    monkeypatch.setattr(_socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(PolicyError, match="no address"):
        resolve_and_pin("empty.example")


def test_confine_path_refuses_symlink(tmp_path):
    real = tmp_path / "real.bin"
    real.write_bytes(b"x")
    link = tmp_path / "link.bin"
    link.symlink_to(real)
    with pytest.raises(PolicyError):
        confine_path(link)


def test_confine_path_base_escape(tmp_path):
    base = tmp_path / "downloads"
    base.mkdir()
    with pytest.raises(PolicyError):
        confine_path("../../etc/cron.d/evil", base=base)
    inside = confine_path("sub/ok.bin", base=base)
    assert str(inside).startswith(str(base.resolve()))


def test_confine_path_no_base_refuses_relative_traversal(monkeypatch, tmp_path):
    # GAP 3 fix (invariant NET-12): with no configured root, the fail-safe default
    # refuses a relative path that climbs out of the working directory with "..",
    # rather than silently resolving to ../../secret.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PolicyError, match="working directory"):
        confine_path("../../etc/passwd")
    # a bare relative filename with no traversal still works (download-here case)
    ok = confine_path("out.bin")
    assert str(ok).startswith(str(tmp_path.resolve()))


def test_confine_path_no_base_allows_absolute_trusted_path(tmp_path):
    # GAP 3 scope (invariant NET-12): an absolute path with no root is the
    # operator's own local target (a trusted-machine file:// ingest), so it is
    # permitted; the traversal block is for attacker-derived relative input.
    target = tmp_path / "clip.mp4"
    assert confine_path(target) == target


# --- chain: next-URL scheme + download cap ----------------------------------


def _mock_client(handler, *, credentials=None):
    http = HttpBinding(credentials=credentials or {})
    http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return http


def _chain_td(follow, from_="json:url"):
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thingctx:dl",
        "title": "dl",
        "securityDefinitions": {"sc": {"scheme": "bearer"}},
        "security": ["sc"],
        "actions": {
            "fetch": {
                "forms": [
                    {
                        "href": "https://api.test/dl/initiate",
                        "htv:methodName": "POST",
                        "contentType": "application/json",
                        "x-thingctx-next": {"from": from_, "follow": follow},
                    }
                ]
            }
        },
    }


async def test_chain_refuses_file_scheme_next_url():
    def handler(req):
        return httpx.Response(200, json={"url": "file:///etc/passwd"})

    http = _mock_client(handler, credentials={"dl": "TOK"})
    client = ThingClient(tds=[_chain_td({"op": "GET"})], bindings=[http])
    with pytest.raises(PolicyError, match="scheme"):
        await client.invoke("dl__fetch", {})


async def test_chain_download_size_cap(monkeypatch):
    monkeypatch.setenv("THINGCTX_MAX_DOWNLOAD_BYTES", "4")
    blob = bytes(range(20))

    def handler(req):
        if req.url.path.endswith("/initiate"):
            return httpx.Response(200, json={"url": "https://api.test/dl/blob"})
        rng = req.headers.get("range", "")
        lo, _, hi = rng.replace("bytes=", "").partition("-")
        lo = int(lo or 0)
        hi = int(hi) if hi else len(blob) - 1
        chunk = blob[lo : hi + 1]
        return httpx.Response(
            206,
            content=chunk,
            headers={
                "Content-Range": f"bytes {lo}-{lo + len(chunk) - 1}/{len(blob)}",
                "Content-Type": "application/octet-stream",
            },
        )

    http = _mock_client(handler, credentials={"dl": "TOK"})
    td = _chain_td({"transport": "ranged-get", "chunkSize": 4})
    client = ThingClient(tds=[td], bindings=[http])
    with pytest.raises(ChainError, match="exceeds"):
        await client.invoke("dl__fetch", {})


async def test_chain_block_private_refuses_metadata_next_url_every_mode():
    """With block_private set, a chain next URL that resolves to a private/metadata
    host is refused for EVERY follow mode. The resumable and ranged-get sends
    bypass HttpBinding._send (where the direct path applies the block), so the
    refusal must happen at the chain's own next-URL check or those modes are an
    SSRF hole: initiate a chain, hand back a link-local Location, and confirm the
    ranged-get download never connects."""
    metadata = "http://169.254.169.254/latest/meta-data/iam/"

    def handler(req):
        # Only the initiate should ever be reached; a connect to the metadata host
        # would mean the block failed.
        assert "169.254.169.254" not in str(req.url), "connected to the metadata host"
        return httpx.Response(200, json={"url": metadata})

    http = _mock_client(handler, credentials={"dl": "TOK"})
    http._block_private = True
    for follow in (
        {"transport": "ranged-get", "chunkSize": 4, "dest": "{out}"},
        {"transport": "resumable", "media": "x", "chunkSize": 4},
        {"op": "GET"},
    ):
        td = _chain_td(follow, from_="json:url")
        # allowlist the metadata host so only the private-host block can stop it
        td["actions"]["fetch"]["forms"][0]["x-thingctx-next"]["allowOrigins"] = ["169.254.169.254"]
        client = ThingClient(tds=[td], bindings=[http])
        with pytest.raises(PolicyError, match="private or loopback"):
            await client.invoke("dl__fetch", {"out": "/tmp/x"})


# --- multi-Thing slug collision ---------------------------------------------


def _min_td(thing_id):
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": thing_id,
        "title": thing_id,
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "properties": {"temp": {"type": "number", "forms": [{"href": "https://h/temp"}]}},
    }


def test_colliding_thing_slugs_are_refused():
    with pytest.raises(ValueError, match="tool namespace"):
        ThingClient(tds=[_min_td("urn:acme:pump:v1"), _min_td("urn:beta:pump:v2")])


# --- TransportError redaction -----------------------------------------------


def test_transport_error_redacts_url_and_detail():
    exc = TransportError("GET", "https://h/x?token=SECRETVALUE", detail="see https://h/y?sig=ABC")
    assert "SECRETVALUE" not in str(exc)
    assert "ABC" not in str(exc)
    d = exc.as_dict()
    assert "SECRETVALUE" not in d["error"]["url"]
    assert "***" in d["error"]["url"]


# --- THINGCTX_BLOCK_PRIVATE: env-driven private-host guard -------------------


def _probe_td(href):
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:probe",
        "title": "Probe",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {"probe": {"forms": [{"href": href, "htv:methodName": "GET"}]}},
    }


class _OneTdRegistry:
    def __init__(self, td):
        self._td = td

    def fetch(self):
        return [self._td]


async def test_missing_transport_dep_skips_its_binding(monkeypatch):
    """A transport whose optional dependency is not installed must not register,
    so a call over it returns a clean no-binding envelope, not a raw ImportError
    from the dep failing to import mid-call. Simulate httpx absent via the probe."""
    import importlib.util

    from thingctx.integrations.mcp import client_from_registry

    real_find_spec = importlib.util.find_spec

    def _find_spec(name, *a, **k):
        if name == "httpx":
            return None  # report the http extra as not installed
        return real_find_spec(name, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)
    monkeypatch.delenv("THINGCTX_POLICY", raising=False)
    monkeypatch.delenv("THINGCTX_IDENTITY", raising=False)
    client = client_from_registry(_OneTdRegistry(_probe_td("http://device.local/probe")))
    schemes = {s for b in client._bindings for s in getattr(b, "schemes", None) or (b.scheme,)}
    assert "http" not in schemes  # the http binding was skipped
    result = await client.invoke("probe__probe", {})
    assert result["transport"] == "http"
    assert "no binding for transport" in result["error"]  # clean, not an ImportError


async def test_block_private_env_refuses_metadata_host(monkeypatch):
    """With THINGCTX_BLOCK_PRIVATE=1, the registry-built client's HTTP transport
    refuses a link-local (cloud metadata) target before any connection."""
    from thingctx.integrations.mcp import client_from_registry

    monkeypatch.setenv("THINGCTX_BLOCK_PRIVATE", "1")
    monkeypatch.delenv("THINGCTX_POLICY", raising=False)
    monkeypatch.delenv("THINGCTX_IDENTITY", raising=False)
    client = client_from_registry(_OneTdRegistry(_probe_td("http://169.254.169.254/meta")))
    with pytest.raises(PolicyError, match="private or loopback"):
        await client.invoke("probe__probe", {})


async def test_block_private_unset_keeps_private_hosts_reachable(monkeypatch):
    """Unset, behavior is unchanged: the same client reaches a loopback server
    (a WoT client legitimately drives LAN and local devices)."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from thingctx.integrations.mcp import client_from_registry

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.delenv("THINGCTX_BLOCK_PRIVATE", raising=False)
        monkeypatch.delenv("THINGCTX_POLICY", raising=False)
        monkeypatch.delenv("THINGCTX_IDENTITY", raising=False)
        href = f"http://127.0.0.1:{srv.server_address[1]}/meta"
        client = client_from_registry(_OneTdRegistry(_probe_td(href)))
        assert await client.invoke("probe__probe", {}) == {"ok": True}
    finally:
        srv.shutdown()


# --- DNS-rebinding: pin the validated IP, never re-resolve to the rebind --------


async def test_dns_rebinding_connects_to_pinned_ip_not_the_rebound_private(monkeypatch):
    """GAP 1 fix (invariant NET-3, DNS-rebinding TOCTOU): with block_private set,
    the host is resolved and validated ONCE, and the socket is pinned to that exact
    IP. A resolver that answers a public (allowed) IP at check time and a private
    (metadata) IP at connect time must not defeat the block: the connection uses
    the pinned validated address and never re-resolves to the rebound private one.
    """
    import json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from thingctx import netpolicy
    from thingctx.bindings import HttpBinding

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ok": True, "host": self.headers.get("Host", "")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    real_gai = socket.getaddrinfo
    lookups: list[str] = []

    def rebinding_gai(host, *a, **k):
        if host == "victim.test":
            lookups.append(host)
            # First lookup (validation): the loopback we control, standing in for a
            # validated public address. Any SECOND lookup is the rebind attempt:
            # answer a cloud-metadata private IP to prove the connect never uses it.
            return (
                real_gai("127.0.0.1", None)
                if len(lookups) == 1
                else real_gai("169.254.169.254", None)
            )
        return real_gai(host, *a, **k)

    # Treat the loopback validation address as public for this test, so the pin
    # path runs (the point under test is the pin, not the private-IP literal check).
    real_is_private = netpolicy.is_private_host

    def is_private_except_loopback_probe(h):
        return False if h == "127.0.0.1" else real_is_private(h)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_gai)
    monkeypatch.setattr(netpolicy, "is_private_host", is_private_except_loopback_probe)
    # http.py calls resolve_and_pin, which reads is_private_host from netpolicy's
    # module globals; patching the attribute above covers it.

    try:
        binding = HttpBinding(block_private=True)
        result = await binding._send("GET", f"http://victim.test:{port}/x", signers=[], cert=None)
        assert result["ok"] is True
        assert result["host"].startswith("victim.test")  # Host header kept as the name
        assert len(lookups) == 1  # resolved once; never re-resolved to the private rebind
        await binding.aclose()
    finally:
        srv.shutdown()


# --- block_private default follows the bind posture --------------------------


def test_exposed_bind_defaults_block_private_on(monkeypatch):
    # A public bind is where SSRF bites, so an operator who set nothing gets the
    # safe posture without opting in.
    from thingctx.integrations.mcp import _default_block_private_when_exposed

    monkeypatch.delenv("THINGCTX_BLOCK_PRIVATE", raising=False)
    _default_block_private_when_exposed("0.0.0.0")
    assert os.environ["THINGCTX_BLOCK_PRIVATE"] == "1"


def test_exposed_bind_honors_explicit_override(monkeypatch):
    # A trusted-LAN gateway must be able to reach private hosts on a public bind,
    # so an explicit 0 is a choice, not a default to overwrite.
    from thingctx.integrations.mcp import _default_block_private_when_exposed

    monkeypatch.setenv("THINGCTX_BLOCK_PRIVATE", "0")
    _default_block_private_when_exposed("0.0.0.0")
    assert os.environ["THINGCTX_BLOCK_PRIVATE"] == "0"


def test_loopback_bind_leaves_block_private_unset(monkeypatch):
    # The laptop default: loopback is not exposed, so LAN devices stay reachable.
    from thingctx.integrations.mcp import _default_block_private_when_exposed

    monkeypatch.delenv("THINGCTX_BLOCK_PRIVATE", raising=False)
    _default_block_private_when_exposed("127.0.0.1")
    assert "THINGCTX_BLOCK_PRIVATE" not in os.environ
