# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A Mosquitto ``acl_file`` for a bus a guarded thingctx MQTT gateway serves.

Every request topic ends in its sender's MQTT User Name, and the gateway runs a
request only when its token's subject is that User Name. These rules are the
broker's half: a caller may publish requests only under its own User Name and read
only its own replies and streams, and only gateway users read requests, which carry
callers' tokens. Callers may also read what the gateway publishes for everyone:
mirrored events and retained descriptions.

A gateway login reads every request, and so every token, for the Things it may
serve, and writes their replies, streams and retained descriptions. When ``serves``
lists a gateway's Things, its login is limited to them, so one gateway cannot read
or answer another's. A lone gateway not listed covers every Thing under the prefix.
With more than one gateway, ``serves`` must list each, or one login could read
every caller's requests and the tokens in them.

When ``grants`` lists a caller's grants, the ``(thing, affordance, op)`` sets the
gateway's policy decision point holds, the caller may send exactly those requests,
so the broker refuses any other before the gateway sees it; the gateway still
decides every request it receives. A grant that limits its arguments compiles to
the same rules as one that does not, because a broker never reads a payload; the
gateway alone enforces the limits. A caller without grants may send any request
under its own User Name, and the gateway alone decides.

The rules name each user rather than using ``%u`` patterns, because Mosquitto
applies a pattern to every user it accepts, listed or not; a user in neither list
may do nothing. Adding a caller or changing a grant means writing the file again
and reloading the broker. Use it with ``allow_anonymous false`` and a
``password_file`` (or certificates), so the broker knows who each user is.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from thingctx.gateways.engine import INVOKE, OBSERVE, READ, SUBSCRIBE, WRITE
from thingctx.thing import _usable_user_name, thing_slug

__all__ = ["acl_file"]

# Topic kinds under <prefix>/<slug>/, as a guarded MQTT gateway lays them out. A
# request topic is one of _REQUESTS followed by its sender's User Name.
_REQUESTS = (
    "actions/+",
    "props/+/read",
    "props/+/write",
    "props/+/observe",
    "events/+/subscribe",
)
# The request kinds a gateway answers with a stream for each caller.
_STREAMS = ("props/+/observe", "events/+/subscribe")
_PUBLISHED = ("events/+", "td")
# The request kind each operation the gateway carries is sent on.
_KIND = {INVOKE: "actions/{}", READ: "props/{}/read", WRITE: "props/{}/write"}
_KIND[OBSERVE] = "props/{}/observe"
_KIND[SUBSCRIBE] = "events/{}/subscribe"


def _slug(thing: str) -> str:
    """The topic level of ``thing``, by the slug the gateway serves it under."""
    slug = thing_slug(thing)
    if slug == "_instances":
        raise ValueError(f"{thing!r} would take _instances, a level thingctx reserves")
    return slug


def _user(name: str) -> str:
    """``name`` if it can be the User Name level of a guarded request topic, by the
    rule the thingctx client applies to its own."""
    if not _usable_user_name(name):
        raise ValueError(f"{name!r} cannot be a user on a guarded gateway bus")
    return name


def _level(name: str) -> str:
    """An affordance name as one topic level in a rule; ``*`` grants every one."""
    if name == "*":
        return "+"
    if not name or not name.isprintable() or any(c in "/+#?" or c.isspace() for c in name):
        raise ValueError(f"{name!r} cannot be one topic level in a rule")
    return name


def _granted(prefix: str, user: str, grants: Iterable[tuple[str, ...]]) -> list[str]:
    """The rules that let ``user`` send exactly the requests ``grants`` allow and
    read their replies and streams. An operation the MQTT gateway does not carry
    adds no rule."""
    lines: list[str] = []
    for thing, affordance, op in sorted({grant[:3] for grant in grants}):
        ops = list(_KIND) if op == "*" else [op]
        for one in ops:
            if one not in _KIND:
                continue
            request = f"{prefix}/{_slug(thing)}/{_KIND[one].format(_level(affordance))}/{user}"
            lines += [f"topic write {request}", f"topic read {request}/reply"]
            if one in (SUBSCRIBE, OBSERVE):
                lines.append(f"topic read {request}/stream")
    return list(dict.fromkeys(lines))


def _serving(prefix: str, slugs: Iterable[str]) -> list[str]:
    """A gateway's rules for the Things whose slugs it serves; ``+`` serves all."""
    lines: list[str] = []
    for slug in slugs:
        lines += [f"topic read {prefix}/{slug}/{kind}/+" for kind in _REQUESTS]
        lines += [f"topic write {prefix}/{slug}/{kind}/+/reply" for kind in _REQUESTS]
        lines += [f"topic write {prefix}/{slug}/{kind}/+/stream" for kind in _STREAMS]
        lines += [f"topic write {prefix}/{slug}/{kind}" for kind in _PUBLISHED]
    return lines


def acl_file(
    *,
    gateways: Iterable[str],
    callers: Iterable[str],
    prefix: str = "tc",
    grants: Mapping[str, Iterable[tuple[str, ...]]] | None = None,
    serves: Mapping[str, Iterable[str]] | None = None,
) -> str:
    """The ``acl_file`` text granting ``gateways`` (broker user names) the gateway's
    side of the bus and ``callers`` the caller's side, under ``prefix``. A gateway
    named in ``serves`` covers only the Things it lists, by id; a caller named in
    ``grants`` may send only the requests its grants allow. A user named in
    neither list may do nothing on the bus. Only a lone gateway may go unlisted,
    because an unlisted login reads every caller's requests and the tokens in them.
    Raises ``ValueError`` for a name that cannot be one topic level, a Thing whose
    slug is the reserved ``_instances``, or more than one gateway when ``serves``
    does not list each."""
    names = [_user(user) for user in gateways]
    serves = serves or {}
    unlisted = [user for user in names if user not in serves]
    if len(names) > 1 and unlisted:
        raise ValueError(f"with more than one gateway, serves must list each: {unlisted}")
    unknown = sorted(set(serves) - set(names))
    if unknown:
        raise ValueError(f"serves names users that are not gateways: {unknown}")
    lines = [f"# thingctx MQTT gateway bus under {prefix}/"]
    for user in names:
        lines += ["", f"user {user}"]
        if user in serves:
            lines += _serving(prefix, sorted({_slug(thing) for thing in serves[user]}))
        else:
            lines += _serving(prefix, ["+"])
    for user in map(_user, callers):
        lines += ["", f"user {user}"]
        if grants is not None and user in grants:
            lines += _granted(prefix, user, grants[user])
        else:
            lines += [f"topic write {prefix}/+/{kind}/{user}" for kind in _REQUESTS]
            lines += [f"topic read {prefix}/+/{kind}/{user}/reply" for kind in _REQUESTS]
            lines += [f"topic read {prefix}/+/{kind}/{user}/stream" for kind in _STREAMS]
        lines += [f"topic read {prefix}/+/{kind}" for kind in _PUBLISHED]
    return "\n".join(lines) + "\n"
