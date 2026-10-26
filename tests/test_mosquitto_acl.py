# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The Mosquitto acl_file for a guarded gateway bus: only gateway users read
requests, which carry callers' tokens, and each caller sends and reads only under
its own User Name."""

from __future__ import annotations

import pytest

from thingctx.gateways.mosquitto import acl_file


def _rules(text: str) -> dict[str, set[tuple[str, str]]]:
    rules: dict[str, set[tuple[str, str]]] = {}
    user = ""
    for line in text.splitlines():
        if line.startswith("user "):
            user = line.removeprefix("user ")
            rules[user] = set()
        elif line.startswith("topic "):
            _, access, topic = line.split(" ", 2)
            rules[user].add((access, topic))
    return rules


def test_only_gateway_users_read_requests() -> None:
    rules = _rules(acl_file(gateways=["gw-1"], callers=["alice"], prefix="site"))
    requests = {
        "site/+/actions/+/+",
        "site/+/props/+/read/+",
        "site/+/props/+/write/+",
        "site/+/events/+/subscribe/+",
    }
    own = {
        "site/+/actions/+/alice",
        "site/+/props/+/read/alice",
        "site/+/props/+/write/alice",
        "site/+/events/+/subscribe/alice",
    }
    assert {("read", t) for t in requests} <= rules["gw-1"]
    assert {("write", t) for t in own} <= rules["alice"]
    assert not any(
        access in ("read", "readwrite") and topic in requests | own
        for access, topic in rules["alice"]
    )


def test_callers_read_what_the_gateway_publishes_and_cannot_write_it() -> None:
    rules = _rules(acl_file(gateways=["gw-1"], callers=["alice"]))
    shared = {"tc/+/td", "tc/+/events/+"}
    assert {("read", t) for t in {*shared, "tc/+/actions/+/alice/reply"}} <= rules["alice"]
    assert {("write", t) for t in {*shared, "tc/+/actions/+/+/reply"}} <= rules["gw-1"]
    assert all(access == "read" for access, topic in rules["alice"] if not topic.endswith("/alice"))


def test_a_caller_reads_only_its_own_replies_and_streams() -> None:
    rules = _rules(acl_file(gateways=["gw-1"], callers=["alice", "bob"]))
    assert ("write", "tc/+/events/+/subscribe/+/stream") in rules["gw-1"]
    for user, other in (("alice", "bob"), ("bob", "alice")):
        assert ("read", f"tc/+/events/+/subscribe/{user}/stream") in rules[user]
        assert ("read", f"tc/+/actions/+/{user}/reply") in rules[user]
        assert not any(other in topic for _, topic in rules[user])
        assert not any(topic.endswith(("/+/reply", "/+/stream")) for _, topic in rules[user])


def test_the_rules_for_one_gateway_and_one_caller() -> None:
    assert acl_file(gateways=["gw-1"], callers=["alice"]) == (
        "# thingctx MQTT gateway bus under tc/\n"
        "\n"
        "user gw-1\n"
        "topic read tc/+/actions/+/+\n"
        "topic read tc/+/props/+/read/+\n"
        "topic read tc/+/props/+/write/+\n"
        "topic read tc/+/props/+/observe/+\n"
        "topic read tc/+/events/+/subscribe/+\n"
        "topic write tc/+/actions/+/+/reply\n"
        "topic write tc/+/props/+/read/+/reply\n"
        "topic write tc/+/props/+/write/+/reply\n"
        "topic write tc/+/props/+/observe/+/reply\n"
        "topic write tc/+/events/+/subscribe/+/reply\n"
        "topic write tc/+/props/+/observe/+/stream\n"
        "topic write tc/+/events/+/subscribe/+/stream\n"
        "topic write tc/+/events/+\n"
        "topic write tc/+/td\n"
        "\n"
        "user alice\n"
        "topic write tc/+/actions/+/alice\n"
        "topic write tc/+/props/+/read/alice\n"
        "topic write tc/+/props/+/write/alice\n"
        "topic write tc/+/props/+/observe/alice\n"
        "topic write tc/+/events/+/subscribe/alice\n"
        "topic read tc/+/actions/+/alice/reply\n"
        "topic read tc/+/props/+/read/alice/reply\n"
        "topic read tc/+/props/+/write/alice/reply\n"
        "topic read tc/+/props/+/observe/alice/reply\n"
        "topic read tc/+/events/+/subscribe/alice/reply\n"
        "topic read tc/+/props/+/observe/alice/stream\n"
        "topic read tc/+/events/+/subscribe/alice/stream\n"
        "topic read tc/+/events/+\n"
        "topic read tc/+/td\n"
    )


@pytest.mark.parametrize(
    "name", ["", "a/b", "a+b", "#", "a?b", "a b", "alice\nuser mallory", "reply"]
)
def test_a_user_name_that_cannot_be_one_topic_level_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="cannot be a user"):
        acl_file(gateways=["gw-1"], callers=[name])
    with pytest.raises(ValueError, match="cannot be a user"):
        acl_file(gateways=[name], callers=["alice"])


PUMP = "urn:demo:pump"


def test_grants_compile_to_exactly_the_requests_they_allow() -> None:
    grants = {
        "alice": {
            (PUMP, "stop", "invokeaction"),
            (PUMP, "rpm", "readproperty"),
            (PUMP, "telemetry", "subscribeevent"),
        }
    }
    rules = _rules(acl_file(gateways=["gw-1"], callers=["alice"], grants=grants))

    assert rules["alice"] == {
        ("write", "tc/pump/actions/stop/alice"),
        ("read", "tc/pump/actions/stop/alice/reply"),
        ("write", "tc/pump/props/rpm/read/alice"),
        ("read", "tc/pump/props/rpm/read/alice/reply"),
        ("write", "tc/pump/events/telemetry/subscribe/alice"),
        ("read", "tc/pump/events/telemetry/subscribe/alice/reply"),
        ("read", "tc/pump/events/telemetry/subscribe/alice/stream"),
        ("read", "tc/+/events/+"),
        ("read", "tc/+/td"),
    }


def test_a_wildcard_grant_compiles_to_a_wildcard_level() -> None:
    rules = _rules(acl_file(gateways=[], callers=["ops"], grants={"ops": {(PUMP, "*", "*")}}))

    writes = {topic for access, topic in rules["ops"] if access == "write"}
    assert writes == {
        "tc/pump/actions/+/ops",
        "tc/pump/props/+/read/ops",
        "tc/pump/props/+/write/ops",
        "tc/pump/props/+/observe/ops",
        "tc/pump/events/+/subscribe/ops",
    }


def test_a_caller_given_no_grants_sends_nothing_and_one_not_given_any_keeps_its_level() -> None:
    rules = _rules(acl_file(gateways=[], callers=["idle", "open"], grants={"idle": set()}))

    assert not any(access == "write" for access, _ in rules["idle"])
    assert ("write", "tc/+/actions/+/open") in rules["open"]


def test_an_observe_grant_compiles_to_its_request_and_its_stream() -> None:
    grants = {"alice": {(PUMP, "rpm", "observeproperty")}}
    rules = _rules(acl_file(gateways=["gw-1"], callers=["alice"], grants=grants))

    assert {
        ("write", "tc/pump/props/rpm/observe/alice"),
        ("read", "tc/pump/props/rpm/observe/alice/reply"),
        ("read", "tc/pump/props/rpm/observe/alice/stream"),
    } <= rules["alice"]
    assert ("write", "tc/+/props/+/observe/+/stream") in rules["gw-1"]
    assert not any("/read/" in topic for _, topic in rules["alice"])


def test_an_operation_the_gateway_does_not_carry_adds_no_rule() -> None:
    grants = {"alice": {(PUMP, "flush", "queryaction"), (PUMP, "flush", "cancelaction")}}
    rules = _rules(acl_file(gateways=[], callers=["alice"], grants=grants))

    assert not any(access == "write" for access, _ in rules["alice"])


@pytest.mark.parametrize("name", ["", "a/b", "a+b", "#", "a?b", "a b", "stop\nuser mallory"])
def test_an_affordance_that_cannot_be_one_topic_level_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="cannot be one topic level"):
        acl_file(gateways=[], callers=["alice"], grants={"alice": {(PUMP, name, "invokeaction")}})


def test_a_gateway_given_its_things_reads_and_answers_only_theirs() -> None:
    serves = {
        "gw-door": ["urn:demo:door"],
        "gw-agents": ["urn:demo:agent:watcher", "urn:demo:channel:chat"],
    }
    rules = _rules(acl_file(gateways=["gw-door", "gw-agents"], callers=[], serves=serves))

    door = {topic for _, topic in rules["gw-door"]}
    assert ("read", "tc/door/actions/+/+") in rules["gw-door"]
    assert ("write", "tc/door/td") in rules["gw-door"]
    assert all(t.startswith("tc/door/") for t in door)
    agents = {topic.split("/")[1] for _, topic in rules["gw-agents"]}
    assert agents == {"watcher", "chat"}


@pytest.mark.parametrize("serves", [None, {"gw-door": ["urn:demo:door"]}])
def test_more_than_one_gateway_must_each_be_given_its_things(serves) -> None:
    with pytest.raises(ValueError, match=r"serves must list each: \['gw-any'"):
        acl_file(gateways=["gw-any", "gw-door"], callers=[], serves=serves)


def test_serves_may_name_only_gateways() -> None:
    with pytest.raises(
        ValueError, match=r"serves names users that are not gateways: \['gw-typo'\]"
    ):
        acl_file(gateways=["gw-1"], callers=[], serves={"gw-typo": ["urn:demo:door"]})


def test_a_thing_on_the_reserved_instances_level_is_refused() -> None:
    reserved = "urn:demo:_instances"
    with pytest.raises(ValueError, match="a level thingctx reserves"):
        acl_file(gateways=["gw-1"], callers=[], serves={"gw-1": [reserved]})
    with pytest.raises(ValueError, match="a level thingctx reserves"):
        acl_file(
            gateways=[], callers=["alice"], grants={"alice": {(reserved, "stop", "invokeaction")}}
        )
