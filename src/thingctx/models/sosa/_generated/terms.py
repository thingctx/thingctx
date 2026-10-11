# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Generated from records.yaml by scripts/generate_model.py. Do not edit.

Run ``scripts/generate_model.py`` after changing the schema. CI fails if this
file and the schema disagree.
"""

from __future__ import annotations

__all__ = [
    "CONTEXT",
    "IDENTITY",
    "ID_NAMESPACE",
    "NESTED",
    "OUTCOMES",
    "STATUS_IRI",
    "TERMS",
    "TYPES",
]

# Prefix to namespace, for every term the records publish.
CONTEXT = {
    "dcat": "http://www.w3.org/ns/dcat#",
    "dct": "http://purl.org/dc/terms/",
    "prov": "http://www.w3.org/ns/prov#",
    "schema": "http://schema.org/",
    "sosa": "http://www.w3.org/ns/sosa/",
}

# Record type, then field to the term it publishes as.
TERMS = {
    "Observation": {
        "result": "sosa:hasSimpleResult",
        "observed_property": "sosa:observedProperty",
        "feature_of_interest": "sosa:hasFeatureOfInterest",
        "procedure": "sosa:usedProcedure",
        "phenomenon_time": "sosa:phenomenonTime",
        "result_time": "sosa:resultTime",
        "derived_from": "prov:wasDerivedFrom",
        "evidence": "dcat:accessURL",
    },
    "Actuation": {
        "result": "sosa:hasSimpleResult",
        "acts_on_property": "sosa:actsOnProperty",
        "feature_of_interest": "sosa:hasFeatureOfInterest",
        "procedure": "sosa:usedProcedure",
        "actor": "prov:wasAssociatedWith",
        "outcome": "schema:actionStatus",
        "reason": "dct:description",
        "phenomenon_time": "sosa:phenomenonTime",
        "result_time": "sosa:resultTime",
        "derived_from": "prov:wasDerivedFrom",
        "evidence": "dcat:accessURL",
    },
    "Refusal": {
        "operation": "sosa:actsOnProperty",
        "feature_of_interest": "sosa:hasFeatureOfInterest",
        "actor": "prov:wasAssociatedWith",
        "status": "schema:actionStatus",
        "reason": "dct:description",
        "evidence": "prov:used",
        "refused_time": "prov:endedAtTime",
    },
}

# The @type IRIs each record carries.
TYPES = {
    "Observation": ["sosa:Observation"],
    "Actuation": ["sosa:Actuation", "schema:Action"],
    "Refusal": ["schema:Action", "prov:Activity"],
}

# A pair that publishes as a node, not as two predicates.
NESTED = {
    "Actuation": {
        "terms": {"description_version": "schema:version"},
        "predicate": "schema:instrument",
        "type": "schema:CreativeWork",
        "id_from": "described_by",
    },
    "Refusal": {
        "terms": {"description_version": "schema:version"},
        "predicate": "schema:instrument",
        "type": "schema:CreativeWork",
        "id_from": "described_by",
    },
}

# The fields that make two records the same record.
IDENTITY = {
    "Observation": [
        "procedure",
        "observed_property",
        "feature_of_interest",
        "phenomenon_time",
        "result",
    ],
    "Actuation": [
        "procedure",
        "acts_on_property",
        "feature_of_interest",
        "phenomenon_time",
        "result",
    ],
    "Refusal": ["actor", "operation", "feature_of_interest", "refused_time"],
}

# Record type to its own id namespace, where it has one.
ID_NAMESPACE = {"Actuation": "sosa:Actuation", "Refusal": "schema:Action/refused"}

# Enum to its values.
OUTCOMES = {"ActuationOutcome": ["succeeded", "canceled", "aborted"], "RefusalStatus": ["refused"]}

# Enum value to the IRI it means.
STATUS_IRI = {
    "succeeded": "schema:CompletedActionStatus",
    "canceled": "schema:FailedActionStatus",
    "aborted": "schema:FailedActionStatus",
    "refused": "schema:FailedActionStatus",
}
