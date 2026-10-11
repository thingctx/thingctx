# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""What an observation promises: a stable identity and standard terms.

The identity rule is the one that earns its keep. The same fact computed twice,
by a live run today and a replay of the same recording tomorrow, has to be one
row. So identity is what the observation says, never when the computation ran.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from thingctx.models.sosa import CONTEXT, Observation

_WHEN = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)


def _obs(**kw: object) -> Observation:
    fields: dict = {
        "result": 41.5,
        "observed_property": "temperature",
        "feature_of_interest": "urn:cell:3",
        "procedure": "urn:procedure:thermal:v2",
        "phenomenon_time": _WHEN,
    }
    fields.update(kw)
    return Observation(**fields)


def test_the_same_fact_twice_is_the_same_id() -> None:
    assert _obs().id == _obs().id


def test_when_the_computation_ran_is_not_part_of_identity() -> None:
    """Replay depends on this.

    Running yesterday's recording through the procedure again produces the same
    facts at a later result_time. If that changed the id, every replay would
    double the table.
    """
    live = _obs(result_time=_WHEN + timedelta(seconds=1))
    replayed = _obs(result_time=_WHEN + timedelta(days=30))
    assert live.id == replayed.id


def test_what_the_observation_says_changes_the_id() -> None:
    base = _obs()
    assert base.id != _obs(result=41.6).id
    assert base.id != _obs(observed_property="pressure").id
    assert base.id != _obs(feature_of_interest="urn:cell:4").id
    assert base.id != _obs(procedure="urn:procedure:thermal:v3").id
    assert base.id != _obs(phenomenon_time=_WHEN + timedelta(seconds=1)).id


def test_provenance_does_not_change_what_the_observation_is() -> None:
    # Evidence and staleness are facts about how this was reached, not about
    # what was observed, so stamping them must not mint a second row.
    base = _obs()
    stamped = base.replace(evidence="mcap://ring/segment-7#123", ages={"plc": 0.02})
    assert stamped.id == base.id
    assert stamped.evidence == "mcap://ring/segment-7#123"


def test_an_id_is_reproducible_across_processes() -> None:
    # Pinned rather than recomputed: a change to how the id is derived splits
    # every historical row from its replay, so it has to be loud.
    assert _obs().id == "urn:uuid:ef203854-a38f-523d-bf91-0f412b52e6e4"


def test_a_naive_timestamp_is_refused() -> None:
    # Guessing a zone is how a two device correlation goes quietly wrong.
    with pytest.raises(ValueError, match="timezone"):
        _obs(phenomenon_time=datetime(2026, 7, 29, 10, 0))


def test_phenomenon_time_defaults_to_when_the_result_was_made() -> None:
    # Right for a live reading, wrong for a replay, which is why a replay says.
    obs = Observation(result=1, observed_property="count", result_time=_WHEN)
    assert obs.phenomenon_time == _WHEN


def test_a_result_that_could_not_reach_a_database_is_refused() -> None:
    # A frame or a buffer belongs in evidence. Accepting it here would put a
    # memory address in an id and break every guarantee above.
    with pytest.raises(TypeError, match="JSON serializable"):
        _obs(result=object())


def test_a_row_is_flat_and_a_document_is_typed() -> None:
    obs = _obs(evidence="mcap://ring/segment-7#123", derived_from=("urn:uuid:a",))
    row = obs.as_row()
    assert row["observed_property"] == "temperature"
    assert row["phenomenon_time"] == "2026-07-29T10:00:00+00:00"
    assert row["derived_from"] == ["urn:uuid:a"]

    doc = obs.as_jsonld()
    assert doc["@type"] == "sosa:Observation"
    assert doc["@context"] == CONTEXT
    assert doc["sosa:hasSimpleResult"] == 41.5
    assert doc["sosa:usedProcedure"] == "urn:procedure:thermal:v2"
    assert doc["prov:wasDerivedFrom"] == ["urn:uuid:a"]
    assert doc["dcat:accessURL"] == "mcap://ring/segment-7#123"


def test_an_unknown_field_is_absent_rather_than_null() -> None:
    # In a procedure, "no statement" and "the value is nothing" are different
    # claims, and only the first is true of a field nobody filled.
    doc = Observation(result=1, observed_property="count", result_time=_WHEN).as_jsonld()
    assert "sosa:hasFeatureOfInterest" not in doc
    assert "dcat:accessURL" not in doc
