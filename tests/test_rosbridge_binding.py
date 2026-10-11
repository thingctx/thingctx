# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A robot behind a real rosbridge socket, and the frames a form makes us send.

What is proved here is the wire: a real WebSocket server speaks the rosbridge
protocol back, and every assertion is about what left the binding or what the
binding did with what came in. The two ways this transport goes wrong are a
subscription that is never taken down (a robot accumulates them until the bridge
stops routing) and a sample credited to the wrong topic (one sensor's value
reported as another's), so both get their own test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from thingctx.bindings.builtin.rosbridge import RosbridgeBinding, RosbridgeError
from thingctx.thing import parse_thing

# Traffic the socket carries that no affordance under test asked for. A rosbridge
# socket is multiplexed, so a consumer sees topics it did not subscribe to.
OTHER_TOPIC = "/scan"

ROBOT = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:demo:rover:1",
    "title": "Rover",
    "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
    "security": ["nosec_sc"],
    "properties": {
        "odometry": {
            "type": "object",
            "observable": True,
            "forms": [
                {
                    "href": "rosbridge://127.0.0.1:{port}/odom?type=nav_msgs/Odometry",
                    "op": ["readproperty", "observeproperty"],
                }
            ],
        },
        "battery": {
            "type": "object",
            "forms": [
                {
                    "href": "rosbridge://127.0.0.1:{port}/battery?type=sensor_msgs/BatteryState",
                    "op": ["readproperty"],
                }
            ],
        },
    },
    "actions": {
        "drive": {
            "input": {
                "type": "object",
                "properties": {
                    "linear": {"type": "object", "properties": {"x": {"type": "number"}}},
                    "angular": {"type": "object", "properties": {"z": {"type": "number"}}},
                },
            },
            "forms": [
                {
                    "href": "rosbridge://127.0.0.1:{port}/cmd_vel?type=geometry_msgs/Twist",
                    "op": ["invokeaction"],
                }
            ],
        },
        "say": {
            "input": {"type": "object", "properties": {"data": {"type": "string"}}},
            "forms": [
                {
                    "href": "rosbridge://127.0.0.1:{port}/chatter?type=std_msgs/String",
                    "op": ["invokeaction"],
                }
            ],
        },
        "dock": {
            "input": {"type": "object", "properties": {}},
            "output": {
                "type": "object",
                "properties": {"success": {"type": "boolean"}, "message": {"type": "string"}},
            },
            "forms": [
                {
                    "href": "rosbridge://127.0.0.1:{port}/dock?type=std_srvs/Trigger",
                    "op": ["invokeaction"],
                }
            ],
        },
        "navigate": {
            "synchronous": False,
            "input": {"type": "object", "properties": {"order": {"type": "integer"}}},
            "forms": [
                {
                    "href": "rosbridge://127.0.0.1:{port}/fibonacci?type=action_tutorials_interfaces/action/Fibonacci",
                    "op": ["invokeaction", "queryaction", "cancelaction"],
                }
            ],
        },
        "forever": {
            "synchronous": False,
            "input": {"type": "object", "properties": {}},
            "forms": [
                {
                    "href": "rosbridge://127.0.0.1:{port}/endless?type=a/action/B",
                    "op": ["invokeaction"],
                }
            ],
        },
        "unrouted": {
            "input": {"type": "object", "properties": {}},
            "forms": [
                {"href": "rosbridge://127.0.0.1:{port}/nowhere", "op": ["invokeaction"]},
            ],
        },
    },
}


class _Rosbridge:
    """A rosbridge endpoint on a real socket: it routes what it is asked to route, answers
    a service call, and keeps every frame it heard."""

    def __init__(self) -> None:
        self.server: Any = None
        self.port = 0
        self.heard: list[dict[str, Any]] = []
        self.closed = 0
        self.off_topic_sent = 0
        self.silent: set[str] = set()
        self.service_result = True
        self.service_values: dict[str, Any] = {"success": True, "message": "docked"}
        self.drop_on_subscribe = False
        self.action_status = 4  # GoalStatus.STATUS_SUCCEEDED
        self.action_result = True
        self.action_values: dict[str, Any] = {"sequence": [0, 1, 1, 2]}
        self.feedback_frames = 2
        self.never_finish: set[str] = set()

    async def start(self) -> None:
        self.server = await websockets.serve(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    def frames(self, op: str) -> list[dict[str, Any]]:
        return [f for f in self.heard if f.get("op") == op]

    async def _serve(self, ws: Any) -> None:
        pumps: dict[str, asyncio.Task[None]] = {}
        try:
            # A consumer that gives up on a read closes with 1011, which arrives
            # here as a failed read rather than the end of the iteration.
            with contextlib.suppress(ConnectionClosed):
                async for raw in ws:
                    frame = json.loads(raw)
                    self.heard.append(frame)
                    op = frame.get("op")
                    if op == "subscribe":
                        if self.drop_on_subscribe:
                            await ws.close()
                            break
                        pumps[frame["topic"]] = asyncio.create_task(self._pump(ws, frame["topic"]))
                    elif op == "unsubscribe":
                        pump = pumps.pop(frame["topic"], None)
                        if pump is not None:
                            pump.cancel()
                    elif op == "send_action_goal":
                        pumps[f"goal:{frame['id']}"] = asyncio.create_task(
                            self._run_goal(ws, frame)
                        )
                    elif op == "cancel_action_goal":
                        goal = pumps.pop(f"goal:{frame['id']}", None)
                        if goal is not None:
                            goal.cancel()
                            await ws.send(
                                json.dumps(
                                    {
                                        "op": "action_result",
                                        "id": frame["id"],
                                        "action": frame["action"],
                                        "status": 5,
                                        "result": True,
                                        "values": {},
                                    }
                                )
                            )
                    elif op == "call_service":
                        await ws.send(
                            json.dumps(
                                {
                                    "op": "service_response",
                                    "service": frame["service"],
                                    "values": self.service_values,
                                    "result": self.service_result,
                                }
                            )
                        )
        finally:
            for pump in pumps.values():
                pump.cancel()
            self.closed += 1

    async def _run_goal(self, ws: Any, frame: dict[str, Any]) -> None:
        """Play an action server: feedback while it runs, then one result. The id is
        echoed because that is what correlates them."""
        goal_id, action = frame["id"], frame["action"]
        with contextlib.suppress(ConnectionClosed, asyncio.CancelledError):
            sent = 0
            while True:
                await asyncio.sleep(0.02)
                if action in self.never_finish or sent < self.feedback_frames:
                    await ws.send(
                        json.dumps(
                            {
                                "op": "action_feedback",
                                "id": goal_id,
                                "action": action,
                                "values": {"partial": sent},
                            }
                        )
                    )
                    sent += 1
                    if action in self.never_finish:
                        continue
                    if sent < self.feedback_frames:
                        continue
                await ws.send(
                    json.dumps(
                        {
                            "op": "action_result",
                            "id": goal_id,
                            "action": action,
                            "status": self.action_status,
                            "result": self.action_result,
                            "values": self.action_values,
                        }
                    )
                )
                return

    async def _pump(self, ws: Any, topic: str) -> None:
        """Publish on a subscribed topic, with a frame for another topic ahead of every one."""
        seq = 0
        with contextlib.suppress(ConnectionClosed):
            while True:
                await asyncio.sleep(0.02)
                await ws.send(
                    json.dumps(
                        {
                            "op": "publish",
                            "topic": OTHER_TOPIC,
                            "msg": {"from_topic": OTHER_TOPIC, "seq": 99},
                        }
                    )
                )
                self.off_topic_sent += 1
                if topic in self.silent:
                    continue
                seq += 1
                await ws.send(
                    json.dumps(
                        {
                            "op": "publish",
                            "topic": topic,
                            "msg": {"from_topic": topic, "seq": seq},
                        }
                    )
                )


@pytest.fixture
async def bridge() -> Any:
    endpoint = _Rosbridge()
    await endpoint.start()
    yield endpoint
    await endpoint.stop()


def _thing(port: int) -> Any:
    return parse_thing(json.loads(json.dumps(ROBOT).replace("{port}", str(port))))


async def _until(predicate: Any, timeout: float = 2.0) -> None:
    """Wait for something the server observes. A frame is on the wire before the server has
    read it, so asserting on `heard` right after a call races it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"the server never saw it within {timeout}s")


async def test_a_property_read_takes_one_sample_and_then_unsubscribes(bridge: Any) -> None:
    """A read that leaves its subscription up leaks one per read on the robot,
    and the bridge keeps pushing that topic to a socket nobody reads."""
    thing = _thing(bridge.port)
    prop = thing.properties["odometry"]
    got = await RosbridgeBinding(timeout=2).read(prop, prop.forms[0])

    assert got["from_topic"] == "/odom"
    await _until(lambda: any(f["topic"] == "/odom" for f in bridge.frames("unsubscribe")))
    # One sample means one subscribe, not one per message seen.
    assert len(bridge.frames("subscribe")) == 1
    # rosbridge can set the subscription up without waiting for a live publisher
    # only when the frame names the type, and the form is where that type lives.
    assert bridge.frames("subscribe")[0]["type"] == "nav_msgs/Odometry"


async def test_a_read_that_finds_nothing_still_unsubscribes(bridge: Any) -> None:
    """The failure path is where a subscription leak hides: a robot that is not
    publishing yet is read over and over, and every attempt leaves one behind."""
    bridge.silent.add("/battery")
    thing = _thing(bridge.port)
    prop = thing.properties["battery"]

    with pytest.raises(RosbridgeError, match="no message on /battery"):
        await RosbridgeBinding(timeout=0.4).read(prop, prop.forms[0])

    await _until(lambda: any(f["topic"] == "/battery" for f in bridge.frames("unsubscribe")))


async def test_a_read_ignores_a_publish_on_another_topic(bridge: Any) -> None:
    """A read that took any publish on its socket would report the laser's
    reading as the robot's odometry, and the value would look entirely plausible.
    """
    thing = _thing(bridge.port)
    prop = thing.properties["odometry"]
    binding = RosbridgeBinding(timeout=2)

    for _ in range(3):
        got = await binding.read(prop, prop.forms[0])
        assert got["from_topic"] == "/odom"

    # Without this the test would pass against a bridge that sent no other
    # traffic at all, and would prove nothing about the filtering.
    assert bridge.off_topic_sent >= 3


async def test_a_subscription_yields_every_message_on_its_topic(bridge: Any) -> None:
    """A stream that drops or reorders samples turns a trajectory into a lie; the
    sequence numbers make a gap visible instead of plausible."""
    thing = _thing(bridge.port)
    prop = thing.properties["odometry"]
    stream = await RosbridgeBinding(timeout=2).subscribe(prop, prop.forms[0])

    seen = []
    async for msg in stream:
        seen.append(msg)
        if len(seen) == 3:
            break
    await stream.aclose()

    assert [m["seq"] for m in seen] == [1, 2, 3]
    assert all(m["from_topic"] == "/odom" for m in seen)


async def test_a_subscription_closes_its_socket_when_the_consumer_stops(bridge: Any) -> None:
    """A stream that holds its socket after the consumer walks away keeps the
    robot publishing into a reader that will never read again, and the leak is
    invisible from this side."""
    thing = _thing(bridge.port)
    prop = thing.properties["odometry"]
    stream = await RosbridgeBinding(timeout=2).subscribe(prop, prop.forms[0])

    async for _ in stream:
        break
    assert bridge.closed == 0
    await stream.aclose()

    await _until(lambda: bridge.closed == 1)


async def test_a_service_action_calls_the_service_and_returns_its_values(bridge: Any) -> None:
    """An action routed to a topic instead of a service returns ok without ever
    asking the robot anything, so a docking request that never happened reads as
    a success."""
    thing = _thing(bridge.port)
    action = thing.actions["dock"]
    got = await RosbridgeBinding(timeout=2).invoke(action, action.forms[0], {"force": True})

    assert got == {"success": True, "message": "docked"}
    called = bridge.frames("call_service")
    assert [f["service"] for f in called] == ["/dock"]
    assert called[0]["args"] == {"force": True}
    assert bridge.frames("publish") == []


async def test_a_service_that_answers_failure_raises_rather_than_returning_data(
    bridge: Any,
) -> None:
    """`result: false` is the robot refusing. Handed back as a value it becomes an output
    an agent reads as the outcome of a docking that did not happen."""
    bridge.service_result = False
    bridge.service_values = {"success": False, "message": "no dock in range"}
    thing = _thing(bridge.port)
    action = thing.actions["dock"]

    with pytest.raises(RosbridgeError, match="no dock in range"):
        await RosbridgeBinding(timeout=2).invoke(action, action.forms[0], {})


async def test_a_topic_action_advertises_before_it_publishes(bridge: Any) -> None:
    """rosbridge drops a publish for a topic it has not been told about, so an
    advertise that arrives second is a command the robot silently never gets."""
    thing = _thing(bridge.port)
    action = thing.actions["say"]
    got = await RosbridgeBinding(timeout=2).invoke(action, action.forms[0], {"data": "hello"})

    assert got["ok"] is True
    ops = [f["op"] for f in bridge.heard if f.get("topic") == "/chatter"]
    assert ops == ["advertise", "publish", "unadvertise"]
    assert bridge.frames("advertise")[0]["type"] == "std_msgs/String"


async def test_a_publish_sends_the_input_as_the_message(bridge: Any) -> None:
    """The action input is the ROS message. The binding does not reshape it, so the
    Thing Description input schema has to mirror the message definition."""
    thing = _thing(bridge.port)
    action = thing.actions["drive"]
    twist = {"linear": {"x": 0.5}, "angular": {"z": -1.25}}
    await RosbridgeBinding(timeout=2).invoke(action, action.forms[0], twist)

    assert bridge.frames("publish")[0]["msg"] == twist


async def test_a_scalar_input_is_refused_with_the_reason(bridge: Any) -> None:
    """A ROS message is an object. The runtime unwraps a scalar input schema to a bare
    value, which rosbridge would silently drop."""
    thing = _thing(bridge.port)
    action = thing.actions["drive"]
    with pytest.raises(RosbridgeError, match="type object"):
        await RosbridgeBinding(timeout=2).invoke(action, action.forms[0], 0.5)  # type: ignore[arg-type]


async def test_a_message_type_the_mapper_does_not_know_is_published_unchanged(
    bridge: Any,
) -> None:
    """A form may already carry a fully shaped ROS message. A mapper that reshaped it
    anyway would make every message type it does not know unsendable, which is nearly
    all of them."""
    thing = _thing(bridge.port)
    action = thing.actions["say"]
    payload = {"data": "hello", "header": {"frame_id": "base_link"}}
    await RosbridgeBinding(timeout=2).invoke(action, action.forms[0], payload)

    assert bridge.frames("publish")[0]["msg"] == payload


async def test_an_action_form_that_names_no_ros_type_says_what_is_missing(bridge: Any) -> None:
    """An action with no output publishes to a topic, and rosbridge needs the message
    type to advertise one. The error must name the ``?type=`` query parameter."""
    thing = _thing(bridge.port)
    action = thing.actions["unrouted"]

    with pytest.raises(RosbridgeError) as raised:
        await RosbridgeBinding(timeout=2).invoke(action, action.forms[0], {})

    assert "?type=" in str(raised.value)
    assert "/nowhere" in str(raised.value)


async def test_a_read_of_a_silent_topic_gives_up_inside_its_own_budget(bridge: Any) -> None:
    """A topic nobody publishes on is the normal state of a simulator that is
    still starting. A read that waits forever holds the caller open, and one that
    answers None reports silence as a reading."""
    bridge.silent.add("/battery")
    thing = _thing(bridge.port)
    prop = thing.properties["battery"]

    started = asyncio.get_running_loop().time()
    with pytest.raises(RosbridgeError):
        await RosbridgeBinding(timeout=0.4).read(prop, prop.forms[0])

    assert asyncio.get_running_loop().time() - started < 2.0


async def test_a_read_whose_socket_the_bridge_drops_raises_rather_than_hanging(
    bridge: Any,
) -> None:
    """A bridge restarting under a running fleet drops sockets. The read has to end, or the
    caller waits on a robot that is no longer on the other end."""
    bridge.drop_on_subscribe = True
    thing = _thing(bridge.port)
    prop = thing.properties["odometry"]

    with pytest.raises((RosbridgeError, ConnectionClosed)):
        await RosbridgeBinding(timeout=2).read(prop, prop.forms[0])


# --------------------------------------------------------------------------- #
# ROS 2 actions: a long running goal, its outcome, and cancellation.
# --------------------------------------------------------------------------- #


async def test_an_action_form_sends_a_goal_and_waits_for_its_result(bridge: Any) -> None:
    """An action marked ``synchronous: false`` is sent as a ROS action goal, not
    published to a topic."""
    thing = _thing(bridge.port)
    action = thing.actions["navigate"]
    out = await RosbridgeBinding(timeout=3).invoke(action, action.forms[0], {"order": 4})

    assert out["succeeded"] is True
    assert out["status"] == 4
    assert out["status_name"] == "succeeded"
    assert out["values"] == {"sequence": [0, 1, 1, 2]}
    sent = bridge.frames("send_action_goal")
    assert len(sent) == 1
    assert sent[0]["action"] == "/fibonacci"
    assert sent[0]["action_type"] == "action_tutorials_interfaces/action/Fibonacci"
    # Nothing was published: a goal is not a topic write.
    assert bridge.frames("publish") == []


async def test_the_goal_id_is_a_uuid_and_differs_per_goal(bridge: Any) -> None:
    """An object address would be reused after a collection and correlate one
    goal's result to another's wait."""
    import uuid as _uuid

    thing = _thing(bridge.port)
    action = thing.actions["navigate"]
    binding = RosbridgeBinding(timeout=3)
    await binding.invoke(action, action.forms[0], {"order": 1})
    await binding.invoke(action, action.forms[0], {"order": 1})

    ids = [f["id"] for f in bridge.frames("send_action_goal")]
    assert len(set(ids)) == 2, "two goals shared an id"
    for goal_id in ids:
        _uuid.UUID(goal_id.rsplit(":", 1)[-1])  # raises if it is not a UUID


async def test_two_goals_in_flight_do_not_cross(bridge: Any) -> None:
    """The failure the UUID prevents, exercised rather than argued."""
    thing = _thing(bridge.port)
    action = thing.actions["navigate"]
    binding = RosbridgeBinding(timeout=3)
    bridge.action_values = {"sequence": [0, 1]}
    a, b = await asyncio.gather(
        binding.invoke(action, action.forms[0], {"order": 1}),
        binding.invoke(action, action.forms[0], {"order": 2}),
    )
    assert a["status_name"] == b["status_name"] == "succeeded"
    assert len({f["id"] for f in bridge.frames("send_action_goal")}) == 2


async def test_feedback_is_reported_when_the_goal_sent_any(bridge: Any) -> None:
    """The goal always asks for feedback. The result carries the latest message when
    the server sent any and omits the key when it sent none."""
    thing = _thing(bridge.port)
    action = thing.actions["navigate"]

    bridge.feedback_frames = 3
    out = await RosbridgeBinding(timeout=3).invoke(action, action.forms[0], {"order": 3})
    assert out["feedback"]["partial"] == 2
    assert bridge.frames("send_action_goal")[-1]["feedback"] is True

    bridge.feedback_frames = 0
    out = await RosbridgeBinding(timeout=3).invoke(action, action.forms[0], {"order": 3})
    assert "feedback" not in out


async def test_an_aborted_goal_reports_the_status_it_ended_in(bridge: Any) -> None:
    """Started and then failed is a different fact from succeeded."""
    thing = _thing(bridge.port)
    action = thing.actions["navigate"]
    bridge.action_status = 6  # STATUS_ABORTED; rosbridge still reports result true
    bridge.action_values = {}
    out = await RosbridgeBinding(timeout=3).invoke(action, action.forms[0], {"order": 4})

    assert out["succeeded"] is False
    assert out["status"] == 6
    assert out["status_name"] == "aborted"


async def test_a_goal_that_never_finishes_is_cancelled_on_the_way_out(bridge: Any) -> None:
    """A binding that only raised would leave the robot moving."""
    thing = _thing(bridge.port)
    bridge.never_finish.add("/endless")
    action = thing.actions["forever"]

    with pytest.raises(RosbridgeError, match="no action_result"):
        await RosbridgeBinding(timeout=0.3).invoke(action, action.forms[0], {})

    await _until(lambda: bridge.frames("cancel_action_goal"))
    cancelled = bridge.frames("cancel_action_goal")[0]
    assert cancelled["action"] == "/endless"
    assert cancelled["id"] == bridge.frames("send_action_goal")[0]["id"]


async def test_cancelling_the_caller_cancels_the_goal(bridge: Any) -> None:
    """Same property from the other direction: the caller gives up rather than the
    clock running out."""
    thing = _thing(bridge.port)
    bridge.never_finish.add("/fibonacci")
    action = thing.actions["navigate"]
    task = asyncio.create_task(
        RosbridgeBinding(timeout=5).invoke(action, action.forms[0], {"order": 9})
    )
    await _until(lambda: bridge.frames("send_action_goal"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _until(lambda: bridge.frames("cancel_action_goal"))


async def test_the_binding_reads_no_vocabulary_of_its_own(bridge: Any) -> None:
    """The binding decides topic, service or action from standard Thing Description
    fields and the href. No form carries a thingctx specific term."""
    thing = _thing(bridge.port)
    for name in ("drive", "dock", "navigate"):
        for form in thing.actions[name].forms:
            assert not [key for key in form.raw if ":" in key and not key.startswith("@")], (
                f"{name} carries a prefixed form term"
            )

    unrouted = thing.actions["unrouted"]
    with pytest.raises(RosbridgeError) as raised:
        await RosbridgeBinding(timeout=1).invoke(unrouted, unrouted.forms[0], {})
    assert "?type=" in str(raised.value)


async def test_a_result_with_no_usable_status_reads_as_unknown(bridge: Any) -> None:
    """A server that omits the status, or sends a string, must not crash the
    caller or produce a status a policy would read."""
    thing = _thing(bridge.port)
    action = thing.actions["navigate"]
    bridge.action_status = "SUCCEEDED"  # a string where GoalStatus is an int
    out = await RosbridgeBinding(timeout=3).invoke(action, action.forms[0], {"order": 1})
    assert out["status"] is None
    assert out["status_name"] == "unknown"


async def test_a_goal_rosbridge_rejects_raises_with_its_reason(bridge: Any) -> None:
    """rosbridge answers a goal it cannot send (no server, unknown type) with
    result false, status unknown and the error text. That is a failure to raise,
    not an outcome to return."""
    thing = _thing(bridge.port)
    action = thing.actions["navigate"]
    bridge.action_result, bridge.action_status = False, 0
    bridge.action_values = "action server /fibonacci not available"
    with pytest.raises(RosbridgeError, match="not available"):
        await RosbridgeBinding(timeout=3).invoke(action, action.forms[0], {"order": 4})


# --------------------------------------------------------------------------- #
# The asynchronous action lifecycle: start, poll, cancel.
# --------------------------------------------------------------------------- #


async def test_an_async_goal_reports_feedback_while_running_and_cancels(bridge: Any) -> None:
    """A goal started with invoke_async is a Thing Description asynchronous action:
    query_action shows the latest feedback, cancel_action stops it on the robot."""
    thing = _thing(bridge.port)
    bridge.never_finish.add("/fibonacci")
    action = thing.actions["navigate"]
    binding = RosbridgeBinding(timeout=3)

    status = await binding.invoke_async(action, action.forms[0], {"order": 9})
    assert status.status == "running"
    assert status.href.startswith("urn:uuid:")

    await _until(lambda: True)  # let the fake send a couple of feedback frames
    await asyncio.sleep(0.1)
    polled = await binding.query_action(status)
    assert polled.status == "running"
    assert polled.output["feedback"]["partial"] >= 1

    ended = await binding.cancel_action(polled)
    assert ended.status == "cancelled"
    assert ended.output["status_name"] == "canceled"
    cancelled = bridge.frames("cancel_action_goal")[0]
    assert cancelled["id"] == bridge.frames("send_action_goal")[0]["id"]


async def test_a_finished_goal_queries_as_completed_with_its_result(bridge: Any) -> None:
    thing = _thing(bridge.port)
    action = thing.actions["navigate"]
    binding = RosbridgeBinding(timeout=3)

    status = await binding.invoke_async(action, action.forms[0], {"order": 4})
    for _ in range(100):
        status = await binding.query_action(status)
        if status.terminal:
            break
        await asyncio.sleep(0.02)

    assert status.status == "completed"
    assert status.output["values"] == {"sequence": [0, 1, 1, 2]}
    assert status.output["feedback"]["partial"] == 1
    # A terminal handle stays terminal; nothing is re-sent.
    assert (await binding.query_action(status)).status == "completed"
    assert (await binding.cancel_action(status)).status == "completed"


async def test_the_runtime_can_cancel_a_goal_an_agent_started(bridge: Any) -> None:
    """The cancel tool the runtime advertises for an asynchronous action reaches the
    robot through this binding, so a running goal can be stopped mid flight."""
    from thingctx import ThingClient

    bridge.never_finish.add("/fibonacci")
    td = json.loads(json.dumps(ROBOT).replace("{port}", str(bridge.port)))
    client = ThingClient(tds=[td], bindings=[RosbridgeBinding(timeout=5)], approve_when="never")
    names = [t["name"] for t in client.tool_surface()]
    start = next(n for n in names if n.endswith("navigate"))
    assert f"{start}__cancel" in names

    running = asyncio.create_task(client.invoke(start, {"order": 9}, wait=True))
    await _until(lambda: bridge.frames("send_action_goal"))
    cancelled = await client.call_tool(f"{start}__cancel", {})
    assert cancelled["status"] == "cancelled", cancelled
    final = await running
    assert final.status == "cancelled"
    assert bridge.frames("cancel_action_goal")
