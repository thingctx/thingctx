"""Quickstart hardening: the documented consume-then-invoke path routes
without manual invoker wiring, and TD fetches send a real User-Agent (some
hosts, e.g. Cloudflare, reject the default urllib UA with HTTP 403)."""

from __future__ import annotations

import io
import json
import urllib.request

from thingctx import ThingClient
from thingctx.registry import _get_json, _user_agent

NOSEC_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:svc:v1",
    "title": "Svc",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "actions": {
        "ping": {"forms": [{"href": "https://example.invalid/ping", "htv:methodName": "GET"}]}
    },
}


def test_default_invokers_when_none():
    c = ThingClient(tds=[NOSEC_TD])  # no invokers passed: should default
    kinds = {type(i).__name__ for i in c._invokers}
    assert "HttpInvoker" in kinds and "LocalInvoker" in kinds


def test_explicit_empty_invokers_stays_empty():
    c = ThingClient(tds=[NOSEC_TD], invokers=[])
    assert c._invokers == []


def test_user_agent_string():
    assert _user_agent().startswith("thingctx")


def test_fetch_sends_user_agent(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return io.BytesIO(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _get_json("https://example.invalid/x", 5) == {"ok": True}
    assert seen["ua"] and seen["ua"].startswith("thingctx")
