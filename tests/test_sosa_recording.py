# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""What the SOSA model makes of the operations the runtime reports: a read is an
Observation, a command an Actuation, a refusal a Refusal, and a long running action
is one Actuation that its final status completes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("pydantic")

from thingctx.lifecycle import ActionStatus
from thingctx.models.sosa import (
    Actuation,
    Observation,
    Refusal,
    recorder,
    records_for,
)
from thingctx.operations import Operation

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=3)


def op(**over):
    facts = {
        "thing_id": "urn:dev:ur5e:1",
        "affordance": "move",
        "op": "invokeaction",
        "permitted": True,
        "reason": "role operator grants it",
        "identity": {"sub": "urn:agent:qwen3", "roles": ["operator"]},
        "arguments": {"to": 1},
        "result": {"ok": True},
        "error": None,
        "started": T0,
        "ended": T1,
        "href": "rosbridge://robot:9090/move",
        "thing_version": "2",
    }
    facts.update(over)
    return Operation(**facts)


def _status(status, href="urn:uuid:7"):
    return ActionStatus(status=status, href=href, thing_id="urn:dev:ur5e:1", name="move")


def test_a_refusal_becomes_a_refusal_record():
    (rec,) = records_for(op(permitted=False, reason="no grant", result=None))
    assert isinstance(rec, Refusal)
    assert rec.operation == "move"
    assert rec.feature_of_interest == "urn:dev:ur5e:1"
    assert rec.actor == "urn:agent:qwen3"
    assert rec.reason == "no grant"
    assert (rec.described_by, rec.description_version) == ("urn:dev:ur5e:1", "2")
    assert rec.refused_time == T1
    assert "schema:Action" in rec.as_jsonld()["@type"]


def test_a_read_becomes_an_observation():
    (rec,) = records_for(
        op(op="readproperty", affordance="joint_states", arguments=None, result=[0.1])
    )
    assert isinstance(rec, Observation)
    assert rec.result == [0.1]
    assert rec.observed_property == "joint_states"
    assert rec.procedure == "rosbridge://robot:9090/move"
    assert rec.phenomenon_time == rec.result_time == T1


def test_a_failed_read_leaves_no_observation():
    assert records_for(op(op="readproperty", arguments=None, result=None, error="timeout")) == []
    assert (
        records_for(op(op="readproperty", arguments=None, result={"error": "no transport"})) == []
    )


def test_a_command_becomes_an_actuation_that_succeeded():
    (rec,) = records_for(op())
    assert isinstance(rec, Actuation)
    assert rec.result == {"to": 1}
    assert rec.acts_on_property == "move"
    assert rec.actor == "urn:agent:qwen3"
    assert rec.outcome == "succeeded"
    assert rec.reason == "role operator grants it"
    assert rec.phenomenon_time == T0
    assert rec.result_time == T1
    assert rec.evidence is None
    assert "sosa:Actuation" in rec.as_jsonld()["@type"]


def test_a_write_is_an_actuation_of_the_property():
    (rec,) = records_for(op(op="writeproperty", affordance="setpoint", arguments=21))
    assert (rec.acts_on_property, rec.result, rec.outcome) == ("setpoint", 21, "succeeded")


def test_a_failure_is_an_aborted_actuation():
    (raised,) = records_for(op(result=None, error="valve jammed"))
    (envelope,) = records_for(op(result={"error": "no binding"}))
    assert raised.outcome == envelope.outcome == "aborted"


def test_a_long_running_action_is_one_record_its_end_completes():
    rows: list = []
    record = recorder(rows.append)
    record(op(result=_status("running")))
    assert rows[-1].outcome is None
    assert rows[-1].evidence == "urn:uuid:7"
    record(op(op="queryaction", arguments=None, result=_status("running"), started=T1))
    assert len(rows) == 1
    record(
        op(
            op="queryaction",
            arguments=None,
            result=_status("completed"),
            started=T1,
            ended=T1 + timedelta(seconds=9),
        )
    )
    assert len(rows) == 2
    assert rows[1].id == rows[0].id
    assert rows[1].outcome == "succeeded"
    assert rows[1].result == {"to": 1}
    assert rows[1].phenomenon_time == T0
    record(op(op="queryaction", arguments=None, result=_status("completed"), started=T1))
    assert len(rows) == 2


def test_a_cancel_ends_the_record_as_canceled():
    rows: list = []
    record = recorder(rows.append)
    record(op(result=_status("running")))
    record(op(op="cancelaction", arguments=None, result=_status("cancelled")))
    assert [r.outcome for r in rows] == [None, "canceled"]
    assert rows[0].id == rows[1].id


def test_a_final_status_with_no_memory_of_the_start_is_still_an_actuation():
    (rec,) = records_for(op(op="queryaction", arguments=None, result=_status("failed")))
    assert (rec.outcome, rec.result, rec.phenomenon_time) == ("aborted", None, T0)


def test_the_actor_comes_from_the_subject_claim_or_the_recorder():
    (rec,) = records_for(op(identity={"roles": ["operator"]}))
    assert rec.actor is None
    (rec,) = records_for(op(identity={"roles": ["operator"]}), actor="urn:agent:x")
    assert rec.actor == "urn:agent:x"
    (rec,) = records_for(op(identity="alice"))
    assert rec.actor == "alice"
    rows: list = []
    recorder(rows.append, actor="urn:agent:y")(op(identity=None))
    assert rows[0].actor == "urn:agent:y"
