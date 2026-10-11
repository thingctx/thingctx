# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""What an actuation promises, and why the log needs it at all.

The gap this closes: an observation log records what procedures sensed, so a
policy can ask whether a reading is stale and cannot ask how many times a valve
was opened this minute. Nothing recorded what the agent did.

Two rules earn their keep here. Identity collapses a redelivered command, so a
retry after a timeout does not count twice against a rate policy. And an
actuation id can never equal an observation id, because sensing 80 and
commanding 80 are different claims about the same number.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from thingctx.models.sosa import CONTEXT, OUTCOMES, Actuation, Observation, Refusal

_WHEN = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)


def _act(**kw: object) -> Actuation:
    fields: dict = {
        "result": 80,
        "acts_on_property": "position",
        "feature_of_interest": "urn:valve:coolant-7",
        "procedure": "urn:procedure:open:v1",
        "phenomenon_time": _WHEN,
    }
    fields.update(kw)
    return Actuation(**fields)


def test_a_redelivered_command_is_one_row():
    """A retried command must be one row, or a rate policy refuses work the plant
    never did."""
    assert _act().id == _act().id
    assert _act(result_time=_WHEN + timedelta(hours=3)).id == _act().id


def test_two_separate_commands_are_two_rows():
    assert _act(phenomenon_time=_WHEN + timedelta(seconds=1)).id != _act().id
    assert _act(result=40).id != _act().id
    assert _act(feature_of_interest="urn:valve:other").id != _act().id
    assert _act(acts_on_property="setpoint").id != _act().id


def test_sensing_a_value_and_commanding_it_never_collide():
    """Only the separate namespaces keep these apart."""
    shared = {
        "result": 80,
        "feature_of_interest": "urn:valve:coolant-7",
        "procedure": "urn:procedure:open:v1",
        "phenomenon_time": _WHEN,
    }
    assert (
        Actuation(acts_on_property="position", **shared).id
        != Observation(observed_property="position", **shared).id
    )


def test_the_actor_survives_into_the_record():
    """Who asked is the question an audit opens with, so it cannot be derived."""
    act = _act(actor="urn:agent:scheduler-3")
    assert act.as_row()["actor"] == "urn:agent:scheduler-3"
    assert act.as_jsonld()["prov:wasAssociatedWith"] == "urn:agent:scheduler-3"


def test_the_decision_inputs_are_reviewable():
    reading = Observation(result=91.0, observed_property="temperature", phenomenon_time=_WHEN)
    act = _act(derived_from=(reading.id,), ages={reading.id: 0.4})
    assert act.derived_from == [reading.id]
    assert act.as_row()["ages"] == {reading.id: 0.4}
    assert act.as_jsonld()["prov:wasDerivedFrom"] == [reading.id]


def test_a_naive_timestamp_is_refused():
    with pytest.raises(ValueError, match="phenomenon_time"):
        _act(phenomenon_time=datetime(2026, 7, 29, 10, 0))
    with pytest.raises(ValueError, match="result_time"):
        _act(result_time=datetime(2026, 7, 29, 10, 0))


def test_a_result_a_database_could_not_hold_is_refused():
    with pytest.raises(TypeError, match="JSON serializable"):
        _act(result=object())


def test_phenomenon_time_defaults_to_result_time():
    act = Actuation(result=1, acts_on_property="position", result_time=_WHEN)
    assert act.phenomenon_time == _WHEN


def test_it_is_frozen_and_replace_rederives_the_id():
    act = _act()
    with pytest.raises(ValueError, match="frozen"):
        act.result = 10  # type: ignore[misc]
    assert act.replace(result=10).id != act.id


def test_the_jsonld_uses_only_terms_the_context_declares():
    """Every key has to resolve, or a consumer silently drops the statement."""
    doc = _act(actor="urn:agent:a", evidence="urn:seg:1").as_jsonld()
    assert doc["@type"] == ["sosa:Actuation", "schema:Action"]
    for key in doc:
        if key.startswith("@"):
            continue
        prefix, _, local = key.partition(":")
        assert prefix in CONTEXT, f"{key} has no prefix in the context"
        assert local, f"{key} has no local name"


def test_empty_fields_are_absent_rather_than_null():
    """An absent statement and a statement that the value is nothing differ."""
    doc = Actuation(result=1, acts_on_property="position", result_time=_WHEN).as_jsonld()
    assert "sosa:hasFeatureOfInterest" not in doc
    assert "prov:wasAssociatedWith" not in doc
    assert "prov:wasDerivedFrom" not in doc
    assert None not in doc.values()


def test_ages_stays_out_of_the_jsonld():
    """It has no term, and an unmapped key is dropped without a warning."""
    doc = _act(ages={"urn:uuid:x": 1.0}).as_jsonld()
    assert all("ages" not in str(key) for key in doc)


# --------------------------------------------------------------------------- #
# The outcome of a command, and the record a refusal leaves
# --------------------------------------------------------------------------- #


def test_a_refusal_is_not_an_outcome():
    """`GoalStatus` applies only after a goal is accepted, so a refused command
    never reaches a terminal state."""
    assert sorted(OUTCOMES) == ["aborted", "canceled", "succeeded"]
    for bad in ("refused", "rejected", "denied", "pending", "SUCCEEDED"):
        # pydantic enforces the enum from the schema, so the error lists the three
        # allowed values.
        with pytest.raises(ValueError, match="Input should be"):
            _act(outcome=bad)


_STATUS = {
    "succeeded": "schema:CompletedActionStatus",
    "canceled": "schema:FailedActionStatus",
    "aborted": "schema:FailedActionStatus",
}


def test_an_outcome_survives_into_the_record():
    """The row keeps the name ROS uses; the document publishes a schema.org
    disposition, which has four values and no notion of who stopped something."""
    for good in ("succeeded", "canceled", "aborted"):
        act = _act(outcome=good)
        assert act.as_row()["outcome"] == good
        assert act.as_jsonld()["schema:actionStatus"] == _STATUS[good]


def test_canceled_and_aborted_publish_alike_and_stay_distinct_in_the_row():
    """schema.org has no notion of who stopped something, so both publish as failed
    and the row keeps the difference."""
    canceled, aborted = _act(outcome="canceled"), _act(outcome="aborted")
    assert (
        canceled.as_jsonld()["schema:actionStatus"]
        == aborted.as_jsonld()["schema:actionStatus"]
        == "schema:FailedActionStatus"
    )
    assert canceled.as_row()["outcome"] != aborted.as_row()["outcome"]


def test_an_actuation_still_running_has_no_outcome():
    """`None` is the honest value while a goal is in flight, and it stays out of
    the JSON-LD rather than claiming the command ended in nothing."""
    act = _act()
    assert act.outcome is None
    assert "schema:actionStatus" not in act.as_jsonld()


def test_the_outcome_is_outside_identity():
    """A goal that is accepted and later succeeds is one actuation, not two."""
    assert _act(outcome="succeeded").id == _act().id


def test_a_refused_command_leaves_a_record_and_no_actuation():
    """The gap this closes: without `Refusal`, "nothing happened" and "we would
    not let it happen" are the same absence of a row."""
    reading = Observation(result=91.0, observed_property="temperature", phenomenon_time=_WHEN)
    d = Refusal(
        operation="open",
        feature_of_interest="urn:valve:coolant-7",
        actor="urn:agent:scheduler-3",
        reason="no grant for open on urn:valve:coolant-7",
        evidence=(reading.id,),
        refused_time=_WHEN,
    )
    doc = d.as_jsonld()
    assert doc["@type"] == ["schema:Action", "prov:Activity"]
    assert doc["schema:actionStatus"] == "schema:FailedActionStatus"
    assert doc["dct:description"].startswith("no grant")
    assert doc["prov:wasAssociatedWith"] == "urn:agent:scheduler-3"
    assert doc["prov:used"] == [reading.id]
    assert doc["sosa:actsOnProperty"] == "open"


def test_an_allow_is_recorded_on_the_actuation_not_separately():
    """Which rule permitted it is what a review asks about, and a permitted
    command already has an actuation to carry that, so there is no second row."""
    act = _act(reason="role operator grants this", outcome="succeeded")
    assert act.as_jsonld()["dct:description"] == "role operator grants this"


def test_two_callers_refused_at_the_same_instant_are_two_records():
    common = {"operation": "open", "refused_time": _WHEN}
    assert Refusal(actor="urn:agent:a", **common).id != Refusal(actor="urn:agent:b", **common).id


def test_a_refusal_and_an_actuation_never_share_an_id():
    d = Refusal(operation="position", refused_time=_WHEN)
    assert d.id != _act().id


def test_a_refusal_refuses_a_naive_timestamp():
    with pytest.raises(ValueError, match="refused_time"):
        Refusal(operation="open", refused_time=datetime(2026, 7, 29, 10, 0))


def test_every_refusal_term_resolves_through_the_context():
    doc = Refusal(
        operation="open",
        feature_of_interest="urn:v:1",
        actor="urn:a:1",
        reason="why",
        evidence=("urn:uuid:x",),
        refused_time=_WHEN,
    ).as_jsonld()
    for key in doc:
        if key.startswith("@"):
            continue
        prefix, _, local = key.partition(":")
        assert prefix in CONTEXT, f"{key} has no prefix in the context"
        assert local


def test_a_refusal_and_an_actuation_answer_one_query():
    """A reviewer asking what an actor attempted should not need to know how it
    ended."""
    act = _act(actor="urn:agent:a", reason="granted", outcome="succeeded")
    ref = Refusal(
        operation="position",
        feature_of_interest="urn:valve:coolant-7",
        actor="urn:agent:a",
        reason="not granted",
        refused_time=_WHEN,
    )
    a, r = act.as_jsonld(), ref.as_jsonld()
    for predicate in (
        "prov:wasAssociatedWith",
        "sosa:hasFeatureOfInterest",
        "sosa:actsOnProperty",
        "dct:description",
        "schema:actionStatus",
    ):
        assert predicate in a and predicate in r, f"{predicate} is not on both records"
    assert a["schema:actionStatus"] == "schema:CompletedActionStatus"
    assert r["schema:actionStatus"] == "schema:FailedActionStatus"


def test_no_record_carries_a_minted_term():
    """Every predicate resolves through a published vocabulary."""
    published = {"sosa", "prov", "dcat", "schema", "dct"}
    reading = Observation(result=1.0, observed_property="temperature", phenomenon_time=_WHEN)
    docs = [
        reading.as_jsonld(),
        _act(outcome="aborted", reason="why", evidence="urn:seg:1").as_jsonld(),
        Refusal(operation="open", reason="why", refused_time=_WHEN).as_jsonld(),
    ]
    for doc in docs:
        for key in doc:
            if key.startswith("@"):
                continue
            assert key.split(":")[0] in published, f"{key} is not a published vocabulary"


# --------------------------------------------------------------------------- #
# Version binding: which description the decision was judged against
# --------------------------------------------------------------------------- #


def test_a_record_names_the_description_version_it_was_judged_against():
    """The gap this closes: a grant replayed against a changed description is
    otherwise undetectable, so "approved this" and "approved something with the
    same name" read identically."""
    act = _act(described_by="urn:dev:pump:coolant-7", description_version="1.4.0")
    doc = act.as_jsonld()
    assert doc["schema:instrument"] == {
        "@type": "schema:CreativeWork",
        "@id": "urn:dev:pump:coolant-7",
        "schema:version": "1.4.0",
    }


def test_a_refusal_names_it_too():
    """A refusal has to be readable against the document in force at the time, or
    a later reviewer cannot tell whether the rule has since changed."""
    doc = Refusal(
        operation="open",
        described_by="urn:dev:valve:7",
        description_version="2.0.0",
        refused_time=_WHEN,
    ).as_jsonld()
    assert doc["schema:instrument"]["schema:version"] == "2.0.0"


def test_the_version_lives_on_the_description_not_on_the_action():
    """schema:version belongs on a CreativeWork. Hanging it off the action would
    put it outside its own domain and read as the action having a version."""
    doc = _act(described_by="urn:d:1", description_version="1.0.0").as_jsonld()
    assert "schema:version" not in doc
    assert doc["schema:instrument"]["@type"] == "schema:CreativeWork"


def test_naming_neither_leaves_no_instrument_at_all():
    """Absent and unknown are the same here, and an empty instrument node would
    assert that a decision was judged against a description with no version."""
    assert "schema:instrument" not in _act().as_jsonld()
    assert "schema:instrument" not in Refusal(operation="open", refused_time=_WHEN).as_jsonld()


def test_a_version_alone_is_still_recorded():
    """A TD may carry a version and no useful id of its own, and the version is
    the part that matters for replay."""
    doc = _act(description_version="9").as_jsonld()
    assert doc["schema:instrument"] == {"@type": "schema:CreativeWork", "schema:version": "9"}


def test_the_description_version_is_outside_identity():
    """Republishing a description does not make a past actuation a second event."""
    assert _act(description_version="1.0.0").id == _act(description_version="2.0.0").id
