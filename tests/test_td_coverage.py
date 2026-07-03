"""Parser coverage for the W3C WoT Thing Description 1.1 information model.

A single maximal Thing Description exercises every modeled construct, and is
itself valid against the bundled W3C TD 1.1 schema, so the parser is checked
against the standard it targets, not an ad hoc shape.
"""

from __future__ import annotations

import pytest

from thingctx.thing import parse_thing
from thingctx.validate import validate_td

# A Thing Description that uses, as far as one document can, the full TD 1.1
# vocabulary: metadata, multi-language text, JSON-LD context, Thing-level bulk
# forms, every security scheme variant, the full form vocabulary, per-affordance
# uriVariables, and the action/event lifecycle schemas.
MAX_TD = {
    "@context": [
        "https://www.w3.org/2022/wot/td/v1.1",
        {"tc": "https://thingctx.com/ns#", "saref": "https://saref.etsi.org/core/"},
    ],
    "@type": ["saref:Pump"],
    "id": "urn:dev:ops:pump-42",
    "title": "Pump 42",
    "titles": {"en": "Pump 42", "de": "Pumpe 42"},
    "description": "A coolant pump.",
    "descriptions": {"en": "A coolant pump.", "de": "Eine Kuehlmittelpumpe."},
    "support": "mailto:ops@example.com",
    "created": "2025-01-01T00:00:00Z",
    "modified": "2025-03-12T00:00:00Z",
    "version": {"instance": "1.2.3", "model": "1.0.0"},
    "base": "https://pump.example.com/",
    "profile": ["https://www.w3.org/TR/wot-profile/"],
    "schemaDefinitions": {"rpm": {"type": "integer", "minimum": 0, "maximum": 6000}},
    "uriVariables": {"p": {"type": "integer"}},
    "links": [
        {
            "href": "https://pump.example.com/manual.pdf",
            "rel": "alternate",
            "type": "application/pdf",
        }
    ],
    "securityDefinitions": {
        "nosec_sc": {"scheme": "nosec"},
        "basic_sc": {"scheme": "basic", "in": "header"},
        "digest_sc": {"scheme": "digest", "in": "header", "qop": "auth"},
        "apikey_sc": {"scheme": "apikey", "in": "query", "name": "api_key"},
        "bearer_sc": {
            "scheme": "bearer",
            "in": "header",
            "alg": "ES256",
            "format": "jwt",
            "authorization": "https://auth.example.com/authorize",
        },
        "psk_sc": {"scheme": "psk", "identity": "pump-42"},
        "oauth2_sc": {
            "scheme": "oauth2",
            "flow": "client_credentials",
            "token": "https://auth.example.com/token",
            "scopes": ["read", "write"],
        },
        "auto_sc": {"scheme": "auto"},
        "combo_or": {"scheme": "combo", "oneOf": ["basic_sc", "bearer_sc"]},
        "combo_and": {"scheme": "combo", "allOf": ["basic_sc", "apikey_sc"]},
    },
    "security": ["basic_sc"],
    "forms": [
        {
            "href": "https://pump.example.com/all/props",
            "op": ["readallproperties", "readmultipleproperties"],
        },
        {
            "href": "https://pump.example.com/all/props",
            "op": ["writeallproperties", "writemultipleproperties"],
        },
        {"href": "https://pump.example.com/all/events", "op": ["subscribeallevents"]},
    ],
    "properties": {
        "rpm": {
            "@type": ["saref:Speed"],
            "title": "RPM",
            "titles": {"en": "RPM"},
            "description": "Current speed.",
            "descriptions": {"en": "Current speed."},
            "type": "integer",
            "minimum": 0,
            "maximum": 6000,
            "unit": "rpm",
            "observable": True,
            "readOnly": False,
            "uriVariables": {"window": {"type": "integer"}},
            "forms": [
                {
                    "href": "https://pump.example.com/rpm",
                    "op": ["readproperty", "writeproperty"],
                    "contentType": "application/json",
                    "contentCoding": "gzip",
                    "response": {"contentType": "application/json"},
                    "additionalResponses": [
                        {"success": False, "contentType": "application/json", "schema": "rpm"}
                    ],
                },
                {
                    "href": "https://pump.example.com/rpm/observe",
                    "op": ["observeproperty", "unobserveproperty"],
                    "subprotocol": "sse",
                    "security": ["bearer_sc"],
                    "scopes": ["read"],
                },
            ],
        }
    },
    "actions": {
        "reset": {
            "@type": ["tc:Destructive"],
            "title": "Reset",
            "description": "Reset the pump.",
            "safe": False,
            "idempotent": True,
            "synchronous": True,
            "input": {"type": "object", "properties": {"hard": {"type": "boolean"}}},
            "output": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            "uriVariables": {"p": {"type": "integer"}},
            "forms": [{"href": "https://pump.example.com/reset", "op": ["invokeaction"]}],
        },
        "calibrate": {
            "title": "Calibrate",
            "safe": False,
            "idempotent": False,
            "synchronous": False,
            "forms": [
                {
                    "href": "https://pump.example.com/calibrate",
                    "op": ["invokeaction", "queryaction", "cancelaction"],
                }
            ],
        },
    },
    "events": {
        "overheat": {
            "@type": ["tc:Alarm"],
            "title": "Overheat",
            "description": "Temperature exceeded.",
            "subscription": {"type": "object", "properties": {"threshold": {"type": "number"}}},
            "data": {"type": "object", "properties": {"temp": {"type": "number"}}},
            "dataResponse": {"type": "object", "properties": {"ack": {"type": "boolean"}}},
            "cancellation": {"type": "object", "properties": {"reason": {"type": "string"}}},
            "uriVariables": {"sensor": {"type": "string"}},
            "forms": [
                {
                    "href": "https://pump.example.com/events/overheat",
                    "op": ["subscribeevent", "unsubscribeevent"],
                    "subprotocol": "sse",
                }
            ],
        }
    },
}


def test_max_td_is_valid_wot_td_11():
    """The fixture itself conforms to the bundled W3C TD 1.1 schema."""
    pytest.importorskip("jsonschema")
    assert validate_td(MAX_TD) == []


def test_thing_metadata_is_modeled():
    t = parse_thing(MAX_TD, validate=True)
    assert t.id == "urn:dev:ops:pump-42"
    assert t.title == "Pump 42"
    assert t.titles == {"en": "Pump 42", "de": "Pumpe 42"}
    assert t.description == "A coolant pump."
    assert t.descriptions["de"] == "Eine Kuehlmittelpumpe."
    assert t.support == "mailto:ops@example.com"
    assert t.created == "2025-01-01T00:00:00Z"
    assert t.modified == "2025-03-12T00:00:00Z"
    assert t.version is not None and t.version.instance == "1.2.3" and t.version.model == "1.0.0"
    assert t.base == "https://pump.example.com/"
    assert t.profile == ("https://www.w3.org/TR/wot-profile/",)
    assert t.schema_definitions["rpm"]["maximum"] == 6000
    assert t.uri_variables == {"p": {"type": "integer"}}
    assert t.at_type == ("saref:Pump",)
    # @context prefixes are collected for JSON-LD term resolution
    assert t.context_prefixes["tc"] == "https://thingctx.com/ns#"
    assert t.context_prefixes["saref"] == "https://saref.etsi.org/core/"
    assert len(t.links) == 1 and t.links[0].rel == "alternate"
    assert t.links[0].type == "application/pdf"


def test_thing_level_bulk_forms():
    t = parse_thing(MAX_TD)
    ops = t.bulk_ops()
    for op in (
        "readallproperties",
        "readmultipleproperties",
        "writeallproperties",
        "writemultipleproperties",
        "subscribeallevents",
    ):
        assert op in ops


def test_property_and_its_full_form_vocabulary():
    t = parse_thing(MAX_TD)
    rpm = t.properties["rpm"]
    assert rpm.readable and rpm.writable and rpm.observable
    assert rpm.at_type == ("saref:Speed",)
    assert rpm.schema["type"] == "integer" and rpm.schema["unit"] == "rpm"
    assert rpm.uri_variables == {"window": {"type": "integer"}}

    rw, observe = rpm.forms
    assert rw.content_type == "application/json"
    assert rw.content_coding == "gzip"
    assert rw.response_type == "application/json"
    assert rw.additional_responses[0]["success"] is False
    # the observe form carries a subprotocol and form-level security override
    assert observe.subprotocol == "sse"
    assert observe.security == ("bearer_sc",)
    assert observe.scopes == ("read",)


def test_action_safe_idempotent_synchronous_and_lifecycle():
    t = parse_thing(MAX_TD)
    reset = t.actions["reset"]
    assert reset.idempotent is True and reset.safe is False
    assert reset.synchronous is True
    assert reset.read_only is True  # idempotent => safe to GET / read-only
    assert reset.is_destructive() is True  # @type tc:Destructive
    assert reset.input_schema["properties"]["hard"]["type"] == "boolean"
    assert reset.output_schema["properties"]["ok"]["type"] == "boolean"
    assert reset.uri_variables == {"p": {"type": "integer"}}

    calibrate = t.actions["calibrate"]
    assert calibrate.idempotent is False and calibrate.safe is False
    assert calibrate.synchronous is False
    assert calibrate.read_only is False
    assert calibrate.supports_query() is True
    assert calibrate.supports_cancel() is True


def test_event_lifecycle_schemas():
    t = parse_thing(MAX_TD)
    ev = t.events["overheat"]
    assert ev.at_type == ("tc:Alarm",)
    assert ev.data_schema["properties"]["temp"]["type"] == "number"
    assert ev.subscription_schema["properties"]["threshold"]["type"] == "number"
    assert ev.data_response_schema["properties"]["ack"]["type"] == "boolean"
    assert ev.cancellation_schema["properties"]["reason"]["type"] == "string"
    assert ev.uri_variables == {"sensor": {"type": "string"}}
    assert ev.forms[0].subprotocol == "sse"


def test_every_security_scheme_variant_is_modeled():
    t = parse_thing(MAX_TD)
    s = t.security_schemes
    assert t.security == ("basic_sc",)
    assert s["nosec_sc"].scheme == "nosec"
    assert s["basic_sc"].scheme == "basic" and s["basic_sc"].in_ == "header"
    assert s["digest_sc"].scheme == "digest" and s["digest_sc"].qop == "auth"
    assert s["apikey_sc"].in_ == "query" and s["apikey_sc"].key_name == "api_key"
    bearer = s["bearer_sc"]
    assert bearer.alg == "ES256" and bearer.format_ == "jwt"
    assert bearer.authorization == "https://auth.example.com/authorize"
    assert s["psk_sc"].identity == "pump-42"
    oauth = s["oauth2_sc"]
    assert oauth.flow == "client_credentials" and oauth.scopes == ("read", "write")
    assert oauth.token == "https://auth.example.com/token"
    assert s["auto_sc"].scheme == "auto"
    assert s["combo_or"].combo_kind == "oneOf"
    assert s["combo_or"].combo_of == ("basic_sc", "bearer_sc")
    assert s["combo_and"].combo_kind == "allOf"
    assert s["combo_and"].combo_of == ("basic_sc", "apikey_sc")


def test_multilanguage_text_resolution_prefers_single_then_english():
    from thingctx.thing import _text

    # single key wins
    assert _text({"title": "X", "titles": {"de": "Y"}}, "titles", "title") == "X"
    # else English from the map
    assert _text({"titles": {"de": "Y", "en": "Z"}}, "titles", "title") == "Z"
    # else first entry
    assert _text({"titles": {"de": "Y"}}, "titles", "title") == "Y"
    # else fallback
    assert _text({}, "titles", "title", fallback="fb") == "fb"


def _example_pump_td() -> dict:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "examples" / "pump.td.json"
    raw = path.read_text()
    raw = raw.replace("{BASE_URL}", "https://pump.example.com").replace(
        "{MQTT_BROKER}", "broker:1883"
    )
    return json.loads(raw)


def test_example_pump_td_is_a_valid_full_coverage_exemplar():
    """The pump driven by the examples is itself W3C-valid and exercises the
    full TD 1.1 surface, so the runnable demo and the parser stay in step."""
    td = _example_pump_td()
    assert validate_td(td) == []

    t = parse_thing(td, validate=True)

    # metadata, i18n, JSON-LD context
    assert t.version.instance == "1.2.0"
    assert t.created and t.modified and t.support
    assert len(t.links) == 1 and t.profile and t.schema_definitions
    assert t.titles.get("de") == "Pumpe"
    assert set(t.context_prefixes) == {"tc", "saref", "om"}

    # every security scheme variant is present
    assert {s.scheme for s in t.security_schemes.values()} == {
        "bearer",
        "basic",
        "digest",
        "apikey",
        "oauth2",
        "psk",
        "auto",
        "combo",
    }

    # Thing-level bulk forms, with form-level security/scopes
    assert t.bulk_ops() >= {
        "readallproperties",
        "readmultipleproperties",
        "writeallproperties",
        "writemultipleproperties",
        "subscribeallevents",
    }
    # form-level security/scopes on a Thing-level form (the bulk-events form);
    # the property bulk forms stay on Thing-level bearer so they are invocable.
    events_form = next(f for f in t.forms if "subscribeallevents" in f.op)
    assert events_form.security == ("oauth2_sc",) and events_form.scopes == ("read",)

    # action lifecycle: safe/idempotent/synchronous + query/cancel
    assert t.actions["status"].safe and t.actions["status"].read_only
    cal = t.actions["calibrate"]
    assert cal.synchronous is False
    assert cal.supports_query() and cal.supports_cancel()
    assert t.actions["read_sensor"].uri_variables == {"id": {"type": "string"}}

    # full form vocabulary on a single form
    sf = t.actions["status"].forms[0]
    assert sf.content_coding == "gzip"
    assert sf.response_type == "application/json"
    assert len(sf.additional_responses) == 1

    # event lifecycle schemas
    ev = t.events["overheat"]
    assert ev.subscription_schema and ev.data_response_schema and ev.cancellation_schema
