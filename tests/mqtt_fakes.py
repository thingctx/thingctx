# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""An in process MQTT broker for tests: topic matching, retained messages, wills,
the PUBACK reason a broker sends when no subscription matched, and the CONNACK and
SUBACK a client waits for, which the broker can refuse or never send."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from paho.mqtt.client import topic_matches_sub

OK = SimpleNamespace(value=0, is_failure=False)
# The reason a broker gives a CONNECT or SUBSCRIBE it refuses (MQTT 5.0, section 2.4).
NOT_AUTHORIZED = SimpleNamespace(value=0x87, is_failure=True)
# What paho's subscribe returns when the client has no connection.
NO_CONN = 4


class Broker:
    """Topic matching, retained messages and wills: the part of a broker the
    gateway relies on. Delivery is synchronous."""

    def __init__(self) -> None:
        self.clients: list[FakeClient] = []
        self.retained: dict[str, tuple[Any, Any]] = {}
        # Topic filters the broker refuses to subscribe; refuse_connect turns every
        # connection away, and silent answers no CONNECT or SUBSCRIBE at all.
        self.denied: set[str] = set()
        self.refuse_connect = False
        self.silent = False
        # SUBACKs held back until release(), to show what a client does meanwhile.
        self.held: list[Any] | None = None

    def release(self) -> None:
        held, self.held = self.held or [], None
        for answer in held:
            answer()

    def route(self, topic: str, payload: Any, retain: bool, properties: Any) -> bool:
        if retain:
            if payload in ("", b""):
                self.retained.pop(topic, None)
            else:
                self.retained[topic] = (payload, properties)
        matched = [c for c in self.clients if c.connected and c.matches(topic)]
        for client in matched:
            client.deliver(topic, payload, properties)
        return bool(matched)

    def record(self, topic: str) -> Any:
        payload = self.retained.get(topic, ("", None))[0]
        return json.loads(payload) if payload else None


class FakeClient:
    """The slice of a paho client the gateway and the MQTT binding use."""

    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.connected = False
        self.subs: set[str] = set()
        self.will: tuple[str, Any] | None = None
        self.auth: tuple[str | None, str | None] | None = None
        self.on_message: Any = None
        self.on_connect: Any = None
        self.on_publish: Any = None
        self.on_subscribe: Any = None
        self._mid = 0

    def matches(self, topic: str) -> bool:
        return any(topic_matches_sub(sub, topic) for sub in self.subs)

    def deliver(self, topic: str, payload: Any, properties: Any) -> None:
        body = payload.encode() if isinstance(payload, str) else payload
        self.on_message(
            self, None, SimpleNamespace(topic=topic, payload=body, properties=properties)
        )

    def reconnect_delay_set(self, **_kw: Any) -> None:
        pass

    def username_pw_set(self, username: str | None, password: str | None = None) -> None:
        self.auth = (username, password)

    def will_set(self, topic: str, payload: Any, **_kw: Any) -> None:
        self.will = (topic, payload)

    def connect(self, *_a: Any, **_kw: Any) -> None:
        self.reconnect()

    def reconnect(self) -> None:
        self.connected = True
        if self not in self.broker.clients:
            self.broker.clients.append(self)

    def loop_start(self) -> None:
        if self.on_connect is None or self.broker.silent:
            return
        if self.broker.refuse_connect:
            self.connected = False
            self.on_connect(self, None, {}, NOT_AUTHORIZED, None)
        elif self.connected:
            self.on_connect(self, None, {}, OK, None)

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        self.connected = False

    def crash(self) -> None:
        self.connected = False
        if self.will is not None:
            self.broker.route(self.will[0], self.will[1], True, None)

    def subscribe(self, topic: str, qos: int = 0) -> tuple[int, int | None]:
        if not self.connected:
            return NO_CONN, None
        self._mid += 1
        mid, granted = self._mid, topic not in self.broker.denied
        if granted:
            self.subs.add(topic)
            for name, (payload, props) in list(self.broker.retained.items()):
                if topic_matches_sub(topic, name):
                    self.deliver(name, payload, props)
        reason = SimpleNamespace(value=qos, is_failure=False) if granted else NOT_AUTHORIZED
        if self.on_subscribe is not None and not self.broker.silent:
            on_subscribe = self.on_subscribe

            def answer() -> None:
                on_subscribe(self, None, mid, [reason], None)

            if self.broker.held is None:
                answer()
            else:
                self.broker.held.append(answer)
        return 0, mid

    def unsubscribe(self, topics: list[str]) -> None:
        self.subs.difference_update(topics)

    def publish(
        self, topic: str, payload: Any, qos: int = 0, retain: bool = False, properties: Any = None
    ) -> Any:
        matched = self.broker.route(topic, payload, retain, properties) if self.connected else False
        if self.on_publish is not None:
            self.on_publish(self, None, 1, SimpleNamespace(value=0 if matched else 0x10), None)
        return SimpleNamespace(mid=1, wait_for_publish=lambda timeout=None: None)
