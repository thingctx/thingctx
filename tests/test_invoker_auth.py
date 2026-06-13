"""Per-Thing auth resolution for HttpInvoker.

A multi-Thing client shares one HttpInvoker, so each request must
authenticate as the Thing that owns the action -- with its own secret and
its own declared scheme. These cases lock that in (offline; no requests are
made -- we assert the headers/params the invoker would attach).
"""

from __future__ import annotations

import base64

from thingctx.invokers import HttpInvoker
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


def _headers_for(http: HttpInvoker, client: ThingClient, tool: str):
    """(headers, params) the invoker would send for `tool`."""
    action = client.action_for(tool)
    return http._hp(action.thing_id)


def test_bearer_resolves_distinct_secret_per_thing():
    http = HttpInvoker(credentials={"alpha": "AAA", "beta": "BBB"})
    client = ThingClient(
        tds=[_td("alpha", {"scheme": "bearer"}), _td("beta", {"scheme": "bearer"})],
        invokers=[http],
    )
    ha, _ = _headers_for(http, client, "alpha.ping")
    hb, _ = _headers_for(http, client, "beta.ping")
    assert ha["Authorization"] == "Bearer AAA"
    assert hb["Authorization"] == "Bearer BBB"


def test_apikey_query_and_header():
    http = HttpInvoker(credentials={"mapsvc": "GKEY", "searchsvc": "BKEY"})
    client = ThingClient(
        tds=[
            _td("mapsvc", {"scheme": "apikey", "in": "query", "name": "key"}),
            _td("searchsvc", {"scheme": "apikey", "in": "header", "name": "X-Token"}),
        ],
        invokers=[http],
    )
    hq, pq = _headers_for(http, client, "mapsvc.ping")
    hh, ph = _headers_for(http, client, "searchsvc.ping")
    assert pq["key"] == "GKEY" and "Authorization" not in hq
    assert hh["X-Token"] == "BKEY" and ph == {}


def test_basic_is_base64_encoded():
    http = HttpInvoker(credentials={"acct": "user:pass"})
    client = ThingClient(tds=[_td("acct", {"scheme": "basic"})], invokers=[http])
    h, _ = _headers_for(http, client, "acct.ping")
    assert h["Authorization"] == "Basic " + base64.b64encode(b"user:pass").decode()


def test_nosec_thing_gets_no_auth():
    http = HttpInvoker(credentials={"openthing": "ignored"})
    client = ThingClient(tds=[_td("openthing", {"scheme": "nosec"})], invokers=[http])
    h, p = _headers_for(http, client, "openthing.ping")
    assert "Authorization" not in h and p == {}


def test_missing_credential_sends_no_header():
    http = HttpInvoker(credentials={})
    client = ThingClient(tds=[_td("alpha", {"scheme": "bearer"})], invokers=[http])
    h, _ = _headers_for(http, client, "alpha.ping")
    assert "Authorization" not in h


def test_legacy_scheme_name_keying_still_works():
    # Single-Thing adopters key credentials by scheme name, not Thing slug.
    http = HttpInvoker(credentials={"sc": "LEGACY"})
    client = ThingClient(tds=[_td("alpha", {"scheme": "bearer"})], invokers=[http])
    h, _ = _headers_for(http, client, "alpha.ping")
    assert h["Authorization"] == "Bearer LEGACY"


def test_one_thing_secret_does_not_leak_to_another():
    # alpha has a secret; beta does not -> beta must send no auth.
    http = HttpInvoker(credentials={"alpha": "AAA"})
    client = ThingClient(
        tds=[_td("alpha", {"scheme": "bearer"}), _td("beta", {"scheme": "bearer"})],
        invokers=[http],
    )
    ha, _ = _headers_for(http, client, "alpha.ping")
    hb, _ = _headers_for(http, client, "beta.ping")
    assert ha["Authorization"] == "Bearer AAA"
    assert "Authorization" not in hb
