# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""What must stay true about vocabulary: ours, a binding's, and a stranger's.

One property per test, each named, so a regression surfaces as the invariant it
broke rather than as a mystery.

    VOC-1  the record publishes no term we minted
    VOC-2  the gate's input does not depend on any extension term
    VOC-3  a document carrying terms we do not know still loads and still drives
    VOC-4  every binding term the code reads is declared with a spec reference
    VOC-5  an extension member survives parsing at every level it may appear
    VOC-6  every term a record publishes is defined by the ontology that owns it
    VOC-7  the model layer's behaviour names no vocabulary at all
"""

from __future__ import annotations

import pathlib
import re

import pytest

from thingctx import LocalBinding, ThingClient
from thingctx.thing import parse_thing

# Each prefix and the namespace IRI its publisher declares. Checking the prefix alone
# is not enough: two documents can both write ``schema:`` and expand to different IRIs.
#
# Source: https://schema.org/docs/jsonldcontext.jsonld ("@vocab": "http://schema.org/")
# Source: https://www.w3.org/TR/vocab-ssn/ (sosa), https://www.w3.org/TR/prov-o/ (prov)
# Source: https://www.w3.org/TR/vocab-dcat-3/ (dcat)
# Source: https://www.dublincore.org/specifications/dublin-core/dcmi-terms/ (dct)
PUBLISHED_NAMESPACES = {
    "sosa": "http://www.w3.org/ns/sosa/",
    "prov": "http://www.w3.org/ns/prov#",
    "dcat": "http://www.w3.org/ns/dcat#",
    "schema": "http://schema.org/",
    "dct": "http://purl.org/dc/terms/",
}

PUBLISHED_PREFIXES = set(PUBLISHED_NAMESPACES)

BASE = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:dev:pump:coolant",
    "title": "Pump",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "properties": {
        "rpm": {
            "type": "number",
            "forms": [{"href": "local://rpm", "op": ["readproperty"]}],
        }
    },
    "actions": {
        "stop": {
            "safe": False,
            "idempotent": True,
            "forms": [{"href": "local://stop", "op": ["invokeaction"]}],
        }
    },
    "events": {
        "tripped": {
            "data": {"type": "string"},
            "forms": [{"href": "local://tripped", "op": ["subscribeevent"]}],
        }
    },
}


def _with_extensions() -> dict:
    """The same Thing, annotated by a stranger at every level a TD allows."""
    import copy

    td = copy.deepcopy(BASE)
    td["@context"] = [
        "https://www.w3.org/2022/wot/td/v1.1",
        {"acme": "https://acme.example/vocab#"},
    ]
    td["acme:fleet"] = "line-4"
    td["properties"]["rpm"]["acme:tag"] = "P-4711"
    td["actions"]["stop"]["acme:clearance"] = "high"
    td["events"]["tripped"]["acme:severity"] = 2
    td["actions"]["stop"]["forms"][0]["acme:retries"] = 3
    return td


# --------------------------------------------------------------------------- #
# VOC-1: the record publishes no term we minted.
# --------------------------------------------------------------------------- #


def test_voc1_the_record_publishes_only_published_vocabularies():
    # invariant VOC-1: a record is not a Thing Description, so there is nothing of
    # WoT's to extend, and no excuse for a minted term when a published one exists.
    from datetime import datetime, timezone

    from thingctx.models.sosa import Actuation, Observation, Refusal

    when = datetime(2026, 10, 2, tzinfo=timezone.utc)
    docs = [
        Observation(result=1.0, observed_property="rpm", phenomenon_time=when).as_jsonld(),
        Actuation(
            result=0,
            acts_on_property="rpm",
            actor="urn:a:1",
            outcome="succeeded",
            reason="granted",
            evidence="urn:seg:1",
            phenomenon_time=when,
        ).as_jsonld(),
        Refusal(
            operation="stop", actor="urn:a:1", reason="not granted", refused_time=when
        ).as_jsonld(),
    ]
    for doc in docs:
        for key in doc:
            if key.startswith("@"):
                continue
            assert key.split(":")[0] in PUBLISHED_PREFIXES, f"{key} is minted"
        # A prefix bound to the wrong IRI is a minted term wearing a published name.
        for prefix, namespace in doc["@context"].items():
            assert namespace == PUBLISHED_NAMESPACES[prefix], (
                f"{prefix}: points at {namespace}, not the namespace its publisher declares"
            )


# --------------------------------------------------------------------------- #
# VOC-2: the gate's input does not depend on any extension term.
# --------------------------------------------------------------------------- #


def test_voc2_the_grantable_surface_ignores_extension_terms():
    # invariant VOC-2: extensions may inform, core must decide. A policy that
    # reads a vendor term only works on that vendor's documents, so the set of
    # things a grant can name must be identical with and without them.
    from thingctx.authz import build_vocabulary

    plain = build_vocabulary(ThingClient(tds=[BASE], bindings=[LocalBinding()]).things)
    annotated = build_vocabulary(
        ThingClient(tds=[_with_extensions()], bindings=[LocalBinding()]).things
    )
    assert plain == annotated, (
        "an extension term changed what a grant can name, so a policy could come "
        "to depend on a vendor vocabulary"
    )


# --------------------------------------------------------------------------- #
# VOC-3: a document carrying terms we do not know still loads and still drives.
# --------------------------------------------------------------------------- #


def test_voc3_an_unknown_vocabulary_does_not_break_loading_or_projection():
    # invariant VOC-3: TD 1.1 permits extension, so refusing one is a conformance
    # bug. The tool surface must be unchanged by annotations we cannot read.
    plain = ThingClient(tds=[BASE], bindings=[LocalBinding()])
    annotated = ThingClient(tds=[_with_extensions()], bindings=[LocalBinding()])
    names = lambda c: sorted(s["function"]["name"] for s in c.tool_specs)  # noqa: E731
    assert names(plain) == names(annotated)
    thing = parse_thing(_with_extensions())
    assert thing.context_prefixes == {"acme": "https://acme.example/vocab#"}, (
        "a declared namespace was dropped, so an extension term cannot be resolved"
    )


# --------------------------------------------------------------------------- #
# VOC-4: every binding term the code reads is declared with a spec reference.
# --------------------------------------------------------------------------- #

# Each term a binding reads off a form, and the published field it comes from.
# A term with no field behind it is declared as ours, in the open, rather than
# left for a reader to discover.
BINDING_TERMS: dict[str, str] = {
    # Reused, not minted: the W3C HTTP Vocabulary in RDF, which the official TD
    # context already declares as htv.
    "htv:methodName": "W3C HTTP Vocabulary in RDF 1.0, http:methodName",
    # The WoT MQTT binding's own term.
    "mqv:retain": "WoT MQTT Binding Template, mqv:retain",
    # The rosbridge binding reads no prefixed term. It uses TD 1.1 fields and the href.
}


def test_voc4_every_binding_term_read_from_a_form_is_declared():
    # invariant VOC-4: a reviewer must be able to check each minted term against a
    # section number. A term that appears in the code and not in the table above is
    # either undocumented or invented, and both are caught here.
    src = pathlib.Path("src/thingctx/bindings/builtin").glob("*.py")
    found: set[str] = set()
    for path in src:
        found |= set(re.findall(r'raw\.get\(\s*"([a-z]+:[A-Za-z]+)"', path.read_text()))
    undeclared = sorted(found - set(BINDING_TERMS))
    assert not undeclared, (
        f"these form terms are read by a binding and declared nowhere: {undeclared}. "
        "Add each to BINDING_TERMS with the published field it comes from, or say "
        "plainly that it is ours."
    )


# --------------------------------------------------------------------------- #
# VOC-5: an extension member survives parsing at every level it may appear.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("level", "member"),
    [
        pytest.param("action", "acme:clearance", id="action"),
        pytest.param("form", "acme:retries", id="form"),
        pytest.param(
            "thing",
            "acme:fleet",
            marks=pytest.mark.xfail(
                reason="WoTThing has no raw, so a Thing level annotation is dropped. "
                "That is where a plant's own naming would sit, e.g. an IEC 81346 "
                "reference designation.",
                strict=True,
            ),
            id="thing",
        ),
        pytest.param(
            "property",
            "acme:tag",
            marks=pytest.mark.xfail(
                reason="WoTProperty has no raw, so a property level annotation is dropped.",
                strict=True,
            ),
            id="property",
        ),
        pytest.param(
            "event",
            "acme:severity",
            marks=pytest.mark.xfail(
                reason="WoTEvent has no raw, so an event level annotation is dropped.",
                strict=True,
            ),
            id="event",
        ),
    ],
)
def test_voc5_an_extension_member_survives_at_every_level(level: str, member: str):
    # invariant VOC-5: saying the architecture supports vocabulary extension is
    # only true where the member survives parsing. Three of the five levels drop
    # it today; those are xfail rather than absent so the gap is visible and the
    # test flips to an assertion when it closes.
    thing = parse_thing(_with_extensions())
    target = {
        "thing": thing,
        "property": thing.properties["rpm"],
        "action": thing.actions["stop"],
        "event": thing.events["tripped"],
        "form": thing.actions["stop"].forms[0],
    }[level]
    assert member in getattr(target, "raw", {}), f"{member} did not survive on the {level}"


# --------------------------------------------------------------------------- #
# VOC-6: every term a record publishes is defined by the ontology that owns it.
# --------------------------------------------------------------------------- #

# The URL where each vocabulary's publisher serves it as Turtle.
ONTOLOGIES = {
    "sosa": "http://www.w3.org/ns/sosa/",
    "prov": "http://www.w3.org/ns/prov-o#",
    "dcat": "http://www.w3.org/ns/dcat#",
    "dct": "http://purl.org/dc/terms/",
    "schema": "https://schema.org/version/latest/schemaorg-current-http.ttl",
}


def _published_terms() -> set[str]:
    """Every prefixed name a record can put in a document."""
    from thingctx.models.sosa._generated.terms import NESTED, STATUS_IRI, TERMS, TYPES

    terms: set[str] = set(STATUS_IRI.values())
    for mapping in TERMS.values():
        terms.update(mapping.values())
    for iris in TYPES.values():
        terms.update(iris)
    for shape in NESTED.values():
        terms.add(shape["predicate"])
        terms.add(shape["type"])
        terms.update(shape["terms"].values())
    return terms


@pytest.mark.network
def test_voc6_every_published_term_exists_in_its_ontology():
    # invariant VOC-6: every term a record publishes exists in the vocabulary file its
    # publisher serves. This catches a typo in a slot_uri as well as an invented term.
    httpx = pytest.importorskip("httpx")
    rdflib = pytest.importorskip("rdflib", reason="needs the models extra")

    from thingctx.models.sosa._generated.terms import CONTEXT

    defined: dict[str, set[str]] = {}
    for prefix, url in ONTOLOGIES.items():
        try:
            response = httpx.get(
                url, headers={"Accept": "text/turtle"}, follow_redirects=True, timeout=60
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            pytest.skip(f"cannot reach {prefix} at {url}: {exc}")
        graph = rdflib.Graph().parse(data=response.text, format="turtle")
        defined[prefix] = {str(subject) for subject in set(graph.subjects())}

    missing = []
    for term in sorted(_published_terms()):
        prefix, local = term.split(":", 1)
        iri = CONTEXT[prefix] + local
        if iri not in defined[prefix]:
            missing.append(f"{term} ({iri})")
    assert not missing, "terms no publisher defines, so minted in effect: " + ", ".join(missing)


# --------------------------------------------------------------------------- #
# VOC-7: the model layer's behaviour names no vocabulary at all.
# --------------------------------------------------------------------------- #

_A_TERM = re.compile(r"""["'](sosa|prov|dcat|dct|schema|tm|td):[A-Za-z]""")


def test_voc7_the_model_base_names_no_vocabulary():
    # invariant VOC-7: models/_base.py is shared by every model, so it must not name
    # any vocabulary term or import any one model.
    base = pathlib.Path(__file__).resolve().parent.parent / "src" / "thingctx" / "models"
    source = (base / "_base.py").read_text()
    found = sorted(set(_A_TERM.findall(source)))
    assert not found, f"the model base names vocabulary: {found}"
    assert "models.sosa" not in source, "the shared base imports one model's tables"
