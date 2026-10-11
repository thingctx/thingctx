# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Drive a ROS 2 system over rosbridge, with no ROS client library on this host.

``rosbridge_suite`` exposes topics, services and actions as JSON over one
WebSocket, so a Thing Description can name a ROS interface in a form and a host
that never installed ROS can read a robot's state and act on it.

A form's href is ``rosbridge://host:port/ros/name?type=pkg/msg/Name``: the path is
the ROS name and the query carries the ROS message, service or action type. The
form needs nothing else. The binding picks topic, service or action from standard
Thing Description fields, with the same rule the runtime uses:

- a property read or subscribe is a topic subscribe
- an action with ``synchronous: false``, or with ``queryaction`` on a form, is a
  ROS action goal
- an action with an ``output`` schema is a service call
- any other action publishes to a topic

A ROS action goal is a Thing Description asynchronous action. ``invoke_async``
sends the goal and returns a handle, ``query_action`` reports the latest feedback
and then the result, and ``cancel_action`` cancels the goal on the robot. ``invoke``
sends the same goal and waits for its result; cancelling the waiting task cancels
the goal on the robot.

rosbridge reports a wrong topic name or ``?type`` only in its own log, never to the
client, so on a read that mistake shows up as a timeout. A service call or a goal
that rosbridge rejects does come back, as ``result: false`` with the error text,
and the binding raises ``RosbridgeError`` with that text.

One short lived WebSocket per operation, except a subscription or a goal, which
holds its socket until it ends.

Behind the ``ros`` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import websockets

from thingctx.bindings.base import AsyncAction, ProtocolBinding, Readable, Subscribable
from thingctx.contracts import implements
from thingctx.lifecycle import ActionStatus

if TYPE_CHECKING:
    from thingctx.thing import WoTAction, WoTForm

__all__ = ["RosbridgeBinding", "RosbridgeError"]

# Prefix of the ids this binding puts on subscribe frames. rosbridge echoes an id
# and never reads it, so it only has to be unique on one socket.
_ID_PREFIX = "thingctx"

# action_msgs/msg/GoalStatus.
_GOAL_STATUS = {
    0: "unknown",
    1: "accepted",
    2: "executing",
    3: "canceling",
    4: "succeeded",
    5: "canceled",
    6: "aborted",
}

# A finished goal's status as a Thing Description action status. Any other
# terminal status is "failed".
_TD_STATUS = {"succeeded": "completed", "canceled": "cancelled"}

# Finished goals stay queryable for every holder of a handle (the runtime's waiter
# and a canceller can hold the same goal). The oldest go once this many are kept.
_KEEP_FINISHED = 100


class RosbridgeError(Exception):
    """A rosbridge operation failed: no connection, no reply within the timeout, or
    rosbridge rejected the service call or the goal."""


def _endpoint(form: WoTForm) -> tuple[str, str, str | None]:
    """Split ``rosbridge://host:port/a/b?type=pkg/msg/T`` into the WebSocket URL
    ``ws://host:port``, the ROS name ``/a/b``, and the type ``pkg/msg/T`` or None.
    rosbridge needs the type to advertise a topic, call a service or send a goal."""
    u = urlparse(form.href)
    host = u.hostname or "localhost"
    port = u.port or 9090
    ros_name = u.path or "/"
    ros_type = parse_qs(u.query).get("type", [None])[0]
    return f"ws://{host}:{port}", ros_name, ros_type


def _is_goal(action: WoTAction) -> bool:
    """True when the Thing Description marks the action as long running, which is what
    a ROS action is: ``synchronous: false``, or ``queryaction`` on one of its forms.
    The runtime decides an action is asynchronous with the same rule."""
    if action.synchronous is False:
        return True
    return any("queryaction" in f.op for f in action.forms)


def _object(arguments: Any, ros_name: str) -> dict[str, Any]:
    """A ROS message, request or goal is an object, so the action input must be one."""
    if not isinstance(arguments, dict):
        raise RosbridgeError(
            f"{ros_name} needs an object as input and got {type(arguments).__name__}; "
            "declare the action input with type object"
        )
    return arguments


def _goal_id(status: ActionStatus) -> str:
    """The goal id a status handle carries, as ``urn:uuid:<goal id>`` in its href."""
    return str(status.href).removeprefix("urn:uuid:")


@dataclass
class _Goal:
    """One goal in flight: the socket it lives on, its latest feedback, and the
    future that holds its result or the error that ended it."""

    ws: Any
    action: str
    outcome: asyncio.Future[dict[str, Any]]
    feedback: Any = None
    follower: asyncio.Task[None] | None = None


@implements(ProtocolBinding, Readable, Subscribable, AsyncAction)
class RosbridgeBinding:
    """One ROS 2 system, driven over rosbridge.

    ``read`` takes one sample off a topic and ``subscribe`` yields every message on
    one. ``invoke`` calls a service, sends a goal and waits for its result, or
    publishes to a topic. ``invoke_async``, ``query_action`` and ``cancel_action``
    run a goal as a Thing Description asynchronous action.
    """

    scheme = "rosbridge"

    def __init__(self, *, timeout: float = 20.0, max_message_bytes: int | None = 16 << 20) -> None:
        """Args:
        timeout: how long one operation may wait to connect, for a message, a
            service response or a goal result. Generous by default because a
            simulator that is still warming up publishes nothing at all.
        max_message_bytes: the largest frame accepted from the bridge, 16 MiB by
            default. ``None`` removes the limit; a camera topic may need that.
        """
        self._timeout = timeout
        self._max_message_bytes = max_message_bytes
        self._goals: dict[str, _Goal] = {}

    async def _connect(self, ws_url: str) -> Any:
        try:
            return await websockets.connect(
                ws_url, max_size=self._max_message_bytes, open_timeout=self._timeout
            )
        except (TimeoutError, asyncio.TimeoutError):
            raise RosbridgeError(f"no connection to {ws_url} within {self._timeout}s") from None

    async def _frame(self, ws: Any, deadline: float, waiting_for: str) -> dict[str, Any]:
        """The next frame on ``ws``, or ``RosbridgeError`` when ``deadline`` passes first."""
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RosbridgeError(f"{waiting_for} within {self._timeout}s")
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except (TimeoutError, asyncio.TimeoutError):
            raise RosbridgeError(f"{waiting_for} within {self._timeout}s") from None
        frame: dict[str, Any] = json.loads(raw)
        return frame

    async def read(self, prop: Any, form: WoTForm) -> Any:
        """Read a property's current value: subscribe to its topic, return the
        first message, then unsubscribe. rosbridge has no "read latest" for a
        topic, so one sample of the live stream is the read."""
        ws_url, topic, msg_type = _endpoint(form)
        async with await self._connect(ws_url) as ws:
            return await self._one_sample(ws, topic, msg_type)

    async def subscribe(
        self, target: Any, form: WoTForm, args: dict[str, Any] | None = None
    ) -> Any:
        """Open a live topic subscription and yield each message. The socket is
        closed when the consumer stops iterating."""
        ws_url, topic, msg_type = _endpoint(form)
        ws = await self._connect(ws_url)
        try:
            await ws.send(_subscribe_frame(topic, msg_type))
        except BaseException:
            await ws.close()
            raise

        async def _stream() -> Any:
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("op") == "publish" and msg.get("topic") == topic:
                        yield msg.get("msg")
            finally:
                await ws.close()

        return _stream()

    async def invoke(self, action: WoTAction, form: WoTForm, arguments: dict[str, Any]) -> Any:
        """Run an action as a ROS action goal, a service call, or a topic publish.

        A goal waits for its result and returns it as a dict with ``status``,
        ``status_name``, ``succeeded``, ``values`` and the latest ``feedback``. A
        service call returns the response. A publish returns ``{"ok": True}`` as
        soon as the message is sent.
        """
        ws_url, ros_name, ros_type = _endpoint(form)
        arguments = _object(arguments, ros_name)
        if _is_goal(action):
            return await self._goal_result(await self.invoke_async(action, form, arguments))
        async with await self._connect(ws_url) as ws:
            if action.output_schema is not None:
                return await self._call_service(ws, ros_name, arguments, ros_type)
            if ros_type is None:
                raise RosbridgeError(
                    f"form for {ros_name} publishes to a topic, so its href needs "
                    "?type=<pkg>/msg/<Name> to advertise it"
                )
            await self._publish(ws, ros_name, ros_type, arguments)
            return {"ok": True, "topic": ros_name}

    async def invoke_async(
        self, action: WoTAction, form: WoTForm, arguments: dict[str, Any]
    ) -> ActionStatus:
        """Send the action as a ROS goal and return a running status handle. The
        goal keeps its socket until the result arrives or the goal is cancelled."""
        ws_url, ros_name, ros_type = _endpoint(form)
        arguments = _object(arguments, ros_name)
        if ros_type is None:
            raise RosbridgeError(
                f"form for {ros_name} is an asynchronous action, so its href "
                "needs ?type=<pkg>/action/<Name> to send a goal"
            )
        # The client owns the goal id; the ROS actions design requires a UUID.
        goal_id = str(uuid.uuid4())
        frame = {
            "op": "send_action_goal",
            "id": goal_id,
            "action": ros_name,
            "action_type": ros_type,
            "args": arguments,
            "feedback": True,
        }
        ws = await self._connect(ws_url)
        try:
            await ws.send(json.dumps(frame))
        except BaseException:
            await ws.close()
            raise
        goal = _Goal(ws=ws, action=ros_name, outcome=asyncio.get_running_loop().create_future())
        goal.follower = asyncio.create_task(self._follow(goal, goal_id))
        self._goals[goal_id] = goal
        self._prune()
        return ActionStatus(
            status="running",
            output={"feedback": None},
            href=f"urn:uuid:{goal_id}",
            form=form,
            thing_id=action.thing_id,
            name=action.name,
        )

    async def query_action(self, status: Any) -> Any:
        """The goal's state now: running with the latest feedback, or completed,
        cancelled or failed with the result."""
        if status.terminal:
            return status
        goal_id = _goal_id(status)
        goal = self._goals.get(goal_id)
        if goal is None:
            return self._ended(status, "failed", error=f"no goal {goal_id} in flight")
        return self._status_of(goal, status)

    async def cancel_action(self, status: Any) -> Any:
        """Cancel the goal on the robot and report how it ended."""
        if status.terminal:
            return status
        goal_id = _goal_id(status)
        goal = self._goals.get(goal_id)
        if goal is None:
            return self._ended(status, "failed", error=f"no goal {goal_id} in flight")
        await self._cancel(goal, goal_id)
        await self._settled(goal)
        return self._status_of(goal, status)

    async def _follow(self, goal: _Goal, goal_id: str) -> None:
        """Collect a goal's feedback and its result off its socket, then close it."""
        try:
            async for raw in goal.ws:
                msg = json.loads(raw)
                if msg.get("id") != goal_id:
                    continue
                if msg.get("op") == "action_feedback":
                    goal.feedback = msg.get("values")
                elif msg.get("op") == "action_result":
                    if msg.get("result", True):
                        goal.outcome.set_result(_goal_outcome(msg, goal.feedback))
                    else:
                        goal.outcome.set_exception(
                            RosbridgeError(f"goal on {goal.action} rejected: {msg.get('values')}")
                        )
                    return
            self._fail(goal, f"connection closed before {goal.action} returned a result")
        except Exception as exc:
            self._fail(goal, f"{goal.action}: {exc}")
        finally:
            await goal.ws.close()

    @staticmethod
    def _fail(goal: _Goal, reason: str) -> None:
        if not goal.outcome.done():
            goal.outcome.set_exception(RosbridgeError(reason))

    async def _settled(self, goal: _Goal) -> bool:
        """Wait up to the timeout for the goal to end. True when it did."""
        done, _ = await asyncio.wait({goal.outcome}, timeout=self._timeout)
        return bool(done)

    async def _goal_result(self, status: ActionStatus) -> dict[str, Any]:
        """Wait for a goal sent by ``invoke_async``; cancel it on the robot if the
        timeout passes or the waiting task is cancelled."""
        goal_id = _goal_id(status)
        goal = self._goals[goal_id]
        try:
            if not await self._settled(goal):
                await self._cancel(goal, goal_id)
                raise RosbridgeError(f"no action_result for {goal.action} within {self._timeout}s")
        except asyncio.CancelledError:
            await self._cancel(goal, goal_id)
            raise
        finally:
            if not goal.outcome.done():
                # The cancel frame is on the wire; nothing waits for the robot's answer.
                self._goals.pop(goal_id, None)
                if goal.follower is not None:
                    goal.follower.cancel()
        return goal.outcome.result()

    def _prune(self) -> None:
        finished = [gid for gid, g in self._goals.items() if g.outcome.done()]
        for gid in finished[: max(0, len(finished) - _KEEP_FINISHED)]:
            del self._goals[gid]

    def _status_of(self, goal: _Goal, status: ActionStatus) -> ActionStatus:
        if not goal.outcome.done():
            return ActionStatus(
                status="running",
                output={"feedback": goal.feedback},
                href=status.href,
                form=status.form,
                thing_id=status.thing_id,
                name=status.name,
            )
        error = goal.outcome.exception()
        if error is not None:
            return self._ended(status, "failed", error=str(error))
        outcome = goal.outcome.result()
        return self._ended(status, _TD_STATUS.get(outcome["status_name"], "failed"), output=outcome)

    @staticmethod
    def _ended(status: ActionStatus, state: str, **fields: Any) -> ActionStatus:
        return ActionStatus(
            status=state,
            href=status.href,
            form=status.form,
            thing_id=status.thing_id,
            name=status.name,
            **fields,
        )

    async def _cancel(self, goal: _Goal, goal_id: str) -> None:
        """Send the cancel. The socket may already be dead; this runs on failure paths."""
        with contextlib.suppress(Exception):
            await goal.ws.send(
                json.dumps({"op": "cancel_action_goal", "id": goal_id, "action": goal.action})
            )

    async def _one_sample(self, ws: Any, topic: str, msg_type: str | None) -> Any:
        """Subscribe, wait for the first publish on ``topic``, unsubscribe."""
        await ws.send(_subscribe_frame(topic, msg_type))
        try:
            deadline = asyncio.get_running_loop().time() + self._timeout
            while True:
                msg = await self._frame(
                    ws,
                    deadline,
                    f"no message on {topic} (a wrong topic name or ?type looks the same)",
                )
                if msg.get("op") == "publish" and msg.get("topic") == topic:
                    return msg.get("msg")
        finally:
            # The socket may already be dead; the first error is the one to keep.
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"op": "unsubscribe", "topic": topic}))

    async def _publish(self, ws: Any, topic: str, msg_type: str, msg: dict[str, Any]) -> None:
        """Advertise the topic, publish one message, unadvertise. rosbridge needs
        the advertise before it will route a publish for a topic it has not seen."""
        await ws.send(json.dumps({"op": "advertise", "topic": topic, "type": msg_type}))
        await ws.send(json.dumps({"op": "publish", "topic": topic, "msg": msg}))
        # No ack exists for a publish. The pause gives rosbridge time to create the
        # publisher and send before the unadvertise removes it.
        await asyncio.sleep(0.2)
        await ws.send(json.dumps({"op": "unadvertise", "topic": topic}))

    async def _call_service(
        self, ws: Any, service: str, arguments: dict[str, Any], service_type: str | None = None
    ) -> Any:
        """Issue a ``call_service`` and return the response ``values``. Raises when
        rosbridge reports the call failed."""
        frame: dict[str, Any] = {"op": "call_service", "service": service, "args": arguments}
        if service_type is not None:
            frame["type"] = service_type
        await ws.send(json.dumps(frame))
        deadline = asyncio.get_running_loop().time() + self._timeout
        while True:
            msg = await self._frame(ws, deadline, f"no service_response for {service}")
            if msg.get("op") == "service_response" and msg.get("service") == service:
                if not msg.get("result", True):
                    raise RosbridgeError(f"service {service} failed: {msg.get('values')}")
                return msg.get("values", {})


def _goal_outcome(msg: dict[str, Any], feedback: Any) -> dict[str, Any]:
    """Shape an ``action_result`` frame into an outcome. ``status`` is kept as the
    GoalStatus integer and named; ``succeeded`` is true only for STATUS_SUCCEEDED."""
    raw = msg.get("status")
    status = raw if isinstance(raw, int) else None
    out: dict[str, Any] = {
        "status": status,
        "status_name": _GOAL_STATUS.get(status, "unknown") if status is not None else "unknown",
        "succeeded": status == 4,
        "values": msg.get("values"),
    }
    if feedback is not None:
        out["feedback"] = feedback
    return out


def _subscribe_frame(topic: str, msg_type: str | None) -> str:
    """A rosbridge ``subscribe`` frame. ``type`` is optional, and with it rosbridge
    can set the subscription up before any publisher is live."""
    frame: dict[str, Any] = {"op": "subscribe", "topic": topic, "id": f"{_ID_PREFIX}:{topic}"}
    if msg_type is not None:
        frame["type"] = msg_type
    return json.dumps(frame)
