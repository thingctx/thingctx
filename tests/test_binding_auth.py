# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Per-Thing auth resolution for HttpBinding.

A multi-Thing client shares one HttpBinding, so each request must
authenticate as the Thing that owns the action, with its own secret and
its own declared scheme. These cases lock that in (offline; no requests are
made; we assert the headers/params the binding would attach).
"""

from __future__ import annotations

import base64

from thingctx.bindings import HttpBinding
from thingctx.runtime import ThingClient


def _td(slug: str, scheme: dict, security: str = "sc") -> dict:
    """Minimal TD with a single GET action `ping` and one security scheme."""
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": f"urn:thingctx:{slug}",
        "title": slug,
        "securityDefinitions": {security: scheme},
        "security": [security],
        "actions": {
            "ping": {"forms": [{"href": f"https://api.example/{slug}", "htv:methodName": "GET"}]}
        },
    }


async def _headers_for(http: HttpBinding, client: ThingClient, tool: str):
    """(headers, params) the binding would send for `tool`."""
    action = client.action_for(tool)
    headers, params, _signers, _cert = await http._prepare(action.thing_id)
    return headers, params


async def test_bearer_resolves_distinct_secret_per_thing():
    http = HttpBinding(credentials={"alpha": "AAA", "beta": "BBB"})
    client = ThingClient(
        tds=[_td("alpha", {"scheme": "bearer"}), _td("beta", {"scheme": "bearer"})],
        bindings=[http],
    )
    ha, _ = await _headers_for(http, client, "alpha__ping")
    hb, _ = await _headers_for(http, client, "beta__ping")
    assert ha["Authorization"] == "Bearer AAA"
    assert hb["Authorization"] == "Bearer BBB"


async def test_apikey_query_and_header():
    http = HttpBinding(credentials={"mapsvc": "GKEY", "searchsvc": "BKEY"})
    client = ThingClient(
        tds=[
            _td("mapsvc", {"scheme": "apikey", "in": "query", "name": "key"}),
            _td("searchsvc", {"scheme": "apikey", "in": "header", "name": "X-Token"}),
        ],
        bindings=[http],
    )
    hq, pq = await _headers_for(http, client, "mapsvc__ping")
    hh, ph = await _headers_for(http, client, "searchsvc__ping")
    assert pq["key"] == "GKEY" and "Authorization" not in hq
    assert hh["X-Token"] == "BKEY" and ph == {}


async def test_basic_is_base64_encoded():
    http = HttpBinding(credentials={"acct": "user:pass"})
    client = ThingClient(tds=[_td("acct", {"scheme": "basic"})], bindings=[http])
    h, _ = await _headers_for(http, client, "acct__ping")
    assert h["Authorization"] == "Basic " + base64.b64encode(b"user:pass").decode()


async def test_nosec_thing_gets_no_auth():
    http = HttpBinding(credentials={"openthing": "ignored"})
    client = ThingClient(tds=[_td("openthing", {"scheme": "nosec"})], bindings=[http])
    h, p = await _headers_for(http, client, "openthing__ping")
    assert "Authorization" not in h and p == {}


async def test_missing_credential_sends_no_header():
    http = HttpBinding(credentials={})
    client = ThingClient(tds=[_td("alpha", {"scheme": "bearer"})], bindings=[http])
    h, _ = await _headers_for(http, client, "alpha__ping")
    assert "Authorization" not in h


async def test_legacy_scheme_name_keying_still_works():
    # Single-Thing adopters key credentials by scheme name, not Thing slug.
    http = HttpBinding(credentials={"sc": "LEGACY"})
    client = ThingClient(tds=[_td("alpha", {"scheme": "bearer"})], bindings=[http])
    h, _ = await _headers_for(http, client, "alpha__ping")
    assert h["Authorization"] == "Bearer LEGACY"


async def test_one_thing_secret_does_not_leak_to_another():
    # alpha has a secret; beta does not -> beta must send no auth.
    http = HttpBinding(credentials={"alpha": "AAA"})
    client = ThingClient(
        tds=[_td("alpha", {"scheme": "bearer"}), _td("beta", {"scheme": "bearer"})],
        bindings=[http],
    )
    ha, _ = await _headers_for(http, client, "alpha__ping")
    hb, _ = await _headers_for(http, client, "beta__ping")
    assert ha["Authorization"] == "Bearer AAA"
    assert "Authorization" not in hb


async def test_ownerless_path_authenticates_as_the_sole_thing():
    # subscribe() carries no owner id (it has only the event name), so the
    # binding resolves credentials with owner_id=None. With a single bound
    # Thing its declared security is unambiguous and must still attach.
    http = HttpBinding(credentials={"sc": "TKN"})
    client = ThingClient(tds=[_td("alpha", {"scheme": "bearer"})], bindings=[http])
    assert client is not None  # binding is now bound to the Thing
    headers, _params, _signers, _cert = await http._prepare(None)
    assert headers["Authorization"] == "Bearer TKN"


async def test_ownerless_path_stays_unauthed_when_ambiguous():
    # With more than one Thing bound, an owner-less call cannot pick a Thing,
    # so it attaches no auth (no silent cross-Thing credential reuse).
    http = HttpBinding(credentials={"alpha": "AAA", "beta": "BBB"})
    ThingClient(
        tds=[_td("alpha", {"scheme": "bearer"}), _td("beta", {"scheme": "bearer"})],
        bindings=[http],
    )
    headers, _params, _signers, _cert = await http._prepare(None)
    assert "Authorization" not in headers


async def test_form_level_security_overrides_thing_security():
    # One Thing, two affordances, two schemes: the Thing defaults to bearer, but
    # the admin form overrides with basic (WoT form-level security). The binding
    # resolves per affordance, so one Thing uses a different scheme per form.
    td = {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:thingctx:dev",
        "title": "dev",
        "securityDefinitions": {"bearer_sc": {"scheme": "bearer"}, "basic_sc": {"scheme": "basic"}},
        "security": ["bearer_sc"],
        "actions": {
            "ping": {"forms": [{"href": "https://api.example/ping", "htv:methodName": "GET"}]},
            "admin": {
                "forms": [
                    {
                        "href": "https://api.example/admin",
                        "htv:methodName": "GET",
                        "security": ["basic_sc"],
                    }
                ]
            },
        },
    }
    http = HttpBinding(credentials={"bearer_sc": "TKN", "basic_sc": "user:pass"})
    client = ThingClient(tds=[td], bindings=[http])
    ping = client.action_for("dev__ping")
    admin = client.action_for("dev__admin")
    ph, _, _, _ = await http._prepare(ping.thing_id, ping.forms[0])
    ah, _, _, _ = await http._prepare(admin.thing_id, admin.forms[0])
    assert ph["Authorization"] == "Bearer TKN"  # inherited the Thing's scheme
    assert ah["Authorization"] == "Basic " + base64.b64encode(b"user:pass").decode()
