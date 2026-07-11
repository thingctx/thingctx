# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Connect a user-authorized Thing on demand (the MCP bridge sign-in flow).

These drive the real logic in ``thingctx.integrations.connect`` with the browser
consent stubbed, so no network and no browser: the store and the OAuth client dir
are redirected to a temp path, and ``oauth_consent.login`` is replaced with a stub
that writes a token the way a real consent would.
"""

from __future__ import annotations

import json

import pytest

from thingctx import ThingClient

CAL_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:calendar",
    "title": "Demo Calendar",
    "securityDefinitions": {
        "prov": {
            "scheme": "oauth2",
            "flow": "code",
            "authorization": "https://provider.example/auth",
            "token": "https://provider.example/token",
            "scopes": ["calendar.readonly"],
        }
    },
    "security": ["prov"],
    "actions": {
        "list_events": {
            "input": {"type": "object", "properties": {}},
            "forms": [{"href": "https://api.example/events", "htv:methodName": "GET"}],
        }
    },
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A redirected config home (empty token store) plus an OAuth client file for
    the TD's provider, and a stub ``login`` that stores a token like real consent."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    clients = tmp_path / "thingctx" / "oauth-clients"
    clients.mkdir(parents=True)
    (clients / "provider.example.json").write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "sec"}})
    )

    import thingctx.auth.store as store

    store._DEFAULT_STORE = None  # rebind to the redirected path

    def fake_login(**kw):
        from thingctx.auth.store import default_token_store, token_key

        key = token_key(kw["owner_id"], kw["token_url"], kw["scopes"])
        default_token_store().set(key, {"refresh_token": "rt", "client_id": kw["client_id"]})
        return {"refresh_token": "rt"}

    import thingctx.auth.oauth_consent as oc

    monkeypatch.setattr(oc, "login", fake_login)
    yield tmp_path
    store._DEFAULT_STORE = None


class _AcceptSession:
    """A session whose elicitation always accepts (a user clicking approve)."""

    async def elicit(self, message, requestedSchema, **kw):
        class R:
            action = "accept"

        return R()


class _DeclineSession:
    async def elicit(self, message, requestedSchema, **kw):
        class R:
            action = "decline"

        return R()


def _client():
    return ThingClient(tds=[CAL_TD], bindings=[])


async def test_status_starts_unconnected(env):
    from thingctx.integrations.connect import connect_status

    st = connect_status(_client())
    assert st == [{"thing": "urn:demo:calendar", "title": "Demo Calendar", "connected": False}]


async def test_connect_tool_signs_in_then_reports_connected(env):
    from thingctx.integrations.connect import connect_status, connect_tool

    client = _client()
    res = await connect_tool(client, {"thing": "calendar"}, _AcceptSession())
    assert res.get("connected") is True
    # the token is now in the store, keyed by this Thing
    assert connect_status(client)[0]["connected"] is True


async def test_connect_matches_by_title_substring(env):
    from thingctx.integrations.connect import connect_tool

    for name in ("calendar", "demo calendar", "Demo Calendar"):
        # fresh store each time so each name actually runs consent
        import thingctx.auth.store as store

        store.default_token_store()._data = {}
        res = await connect_tool(_client(), {"thing": name}, _AcceptSession())
        assert res.get("connected") is True, name


async def test_declining_does_not_connect(env):
    from thingctx.integrations.connect import connect_status, connect_tool

    client = _client()
    res = await connect_tool(client, {"thing": "calendar"}, _DeclineSession())
    assert "error" in res or res.get("connected") is not True
    assert connect_status(client)[0]["connected"] is False


async def test_no_client_file_is_a_clear_error(tmp_path, monkeypatch):
    # Redirect config home but do NOT create a client file for the provider.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    import thingctx.auth.store as store

    store._DEFAULT_STORE = None
    from thingctx.integrations.connect import connect_tool

    res = await connect_tool(_client(), {"thing": "calendar"}, _AcceptSession())
    assert "error" in res and "no OAuth client is configured" in res["error"]
    store._DEFAULT_STORE = None


async def test_ensure_connected_runs_on_a_call_that_needs_a_token(env):
    from thingctx.integrations.connect import connect_status, ensure_connected

    client = _client()
    # the auto-connect path: a tool call whose Thing is not yet connected
    err = await ensure_connected(client, "calendar.list_events", _AcceptSession())
    assert err is None
    assert connect_status(client)[0]["connected"] is True


async def test_connect_bare_lists_what_needs_signin(env):
    from thingctx.integrations.connect import connect_tool

    res = await connect_tool(_client(), {}, _AcceptSession())
    assert "Demo Calendar" in res["message"]
    assert any(not s["connected"] for s in res["services"])
