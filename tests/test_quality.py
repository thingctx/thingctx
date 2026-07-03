"""The reserved ``tc:quality`` output envelope.

Covers the contract only , the key, its shape, and reading a verdict. thingctx
never computes a score, so there is nothing domain-specific to test here; the
adopter (the capability) fills the slot."""

from __future__ import annotations

from thingctx import QUALITY_KEY, is_suspect, make_quality, quality_of
from thingctx.quality import SUSPECT_VERDICTS


def test_quality_key_is_namespaced():
    assert QUALITY_KEY == "tc:quality"
    assert SUSPECT_VERDICTS == frozenset({"suspect", "bad"})


def test_make_quality_minimal_and_full():
    assert make_quality("ok") == {"tc:quality": {"verdict": "ok"}}
    assert make_quality("bad", score=0.1, reason="why", signals={"r": 0.9}) == {
        "tc:quality": {"verdict": "bad", "score": 0.1, "reason": "why", "signals": {"r": 0.9}}
    }
    # An unmeasured field is omitted, not sent as null/empty.
    assert "signals" not in make_quality("ok", signals={})["tc:quality"]
    assert "score" not in make_quality("ok")["tc:quality"]


def test_quality_of_reads_reserved_key_and_bare_form():
    out = {"data": 1, **make_quality("suspect", reason="r")}
    assert quality_of(out) == {"verdict": "suspect", "reason": "r"}
    assert quality_of({"quality": {"verdict": "ok"}}) == {"verdict": "ok"}


def test_quality_of_none_when_absent_or_malformed():
    assert quality_of({"data": 1}) is None  # no signal is distinct from "ok"
    assert quality_of("not a dict") is None
    assert quality_of({"tc:quality": {"score": 0.5}}) is None  # missing verdict
    assert quality_of({"tc:quality": "nope"}) is None


def test_is_suspect_only_on_suspect_or_bad():
    assert is_suspect(make_quality("suspect"))
    assert is_suspect(make_quality("bad"))
    assert not is_suspect(make_quality("ok"))
    assert not is_suspect({"data": 1})


def test_shipped_skill_documents_the_rule():
    # The guardrail rides the driver skill: the shipped SKILL.md must tell an
    # agent to stop on a suspect/bad verdict.
    from thingctx.cli import _skill_text

    text = _skill_text()
    assert "tc:quality" in text
    assert "suspect" in text and "bad" in text
