# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""LLMHost: a tool-calling loop over a ThingClient. litellm is imported
lazily here only, so the pure ThingClient has no LLM dependency.

    client = ThingClient(tds=[td], bindings=[HttpBinding()])
    host = LLMHost(client, model="openai/gpt-4o-mini")
    print(await host.chat("read temp-1 and report it"))
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from thingctx.runtime import ThingClient, to_text


def default_model() -> str:
    """The model the convenience helpers use when the caller names none. Reads
    ``THINGCTX_MODEL`` so a deployment picks its own provider and version without
    a code change; the fallback is a small, widely available tool-calling model.
    Returns a litellm-style ``provider/model`` string."""
    return os.environ.get("THINGCTX_MODEL", "openai/gpt-4o-mini")


def _summary_from_memo(memo: dict) -> str:
    """Fallback answer from the gathered tool results."""
    if not memo:
        return "(no answer)"
    parts = [f"{name}: {result}" for (name, _args), result in memo.items()]
    return "Completed: " + "; ".join(parts)


def _get(obj: Any, key: str) -> Any:
    """Read key from a dict or an object (litellm returns objects)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# (messages, tools) -> an assistant message dict (OpenAI shape).
ChatFn = Callable[
    [list[dict[str, Any]], list[dict[str, Any]]],
    Awaitable[dict[str, Any]],
]


class LLMHost:
    """Run an LLM tool-calling loop against a ThingClient."""

    def __init__(
        self,
        client: ThingClient,
        *,
        model: str | None = None,
        system: str | None = None,
        max_rounds: int = 8,
        chat_fn: ChatFn | None = None,
        resilient: bool = False,
    ) -> None:
        self._client = client
        self._model = model or default_model()
        self._system = system
        self._max_rounds = max_rounds
        self._chat_fn = chat_fn
        # resilient=True caches repeated calls and forces a final no-tools
        # answer, for weaker models that loop on tools. Off by default.
        self._resilient = resilient

    @property
    def client(self) -> ThingClient:
        return self._client

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        return self._openai_tools()

    def _openai_tools(self) -> list[dict[str, Any]]:
        """The client's full tool surface in OpenAI function-tool shape: actions
        plus property get/set, bulk read, and long-running cancel tools."""
        tools = []
        for t in self._client.tool_surface():
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
            )
        return tools

    async def chat(self, prompt: str) -> str:
        """Run the tool-calling loop for one prompt; return the final text
        answer. See resilient for the weaker-model guards."""
        return await self._run(prompt)

    async def see(self, image: Any, instruction: str) -> str:
        """Run one vision turn over an image (or several) plus an instruction,
        with the Thing's tools available. ``image`` is a media :class:`Frame`,
        raw JPEG bytes, an image URL (http(s) or data URL), or a list of any of
        these. A list is sent as a sequence of stills; a portable clip the
        model reads as motion over time, on any vision capable backend.

            frame = await anext(await client.frames("cam-1__watch"))
            print(await host.see(frame, "Is anyone at the door?"))

            clip = await sample_frames(await client.frames("cam-1__watch"), count=6)
            print(await host.see(clip, "Describe what happens in this clip."))
        """
        images = list(image) if isinstance(image, list | tuple) else [image]
        content: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
        for im in images:
            content.append({"type": "image_url", "image_url": {"url": self._as_image_url(im)}})
        return await self._run(content)

    async def see_video(self, video: Any, instruction: str) -> str:
        """Run one vision turn over a video clip plus an instruction, sent as a
        native ``video_url`` content part. ``video`` is a video URL (http(s) or
        a ``data:video/...;base64,`` URL), a local file path, or raw bytes; a
        path or bytes is inlined as a data URL (more reliable than asking the
        provider to fetch a remote URL).

        Unlike :meth:`see`, the model receives the clip itself, so this needs a
        provider with native video input (a hosted or self hosted multimodal
        model). Image only backends do not accept it; use :meth:`see` with a list
        of frames there.

            print(await host.see_video(url, "Summarize this clip."))
        """
        content = [
            {"type": "text", "text": instruction},
            {"type": "video_url", "video_url": {"url": self._as_video_url(video)}},
        ]
        return await self._run(content)

    @staticmethod
    def _as_video_url(video: Any) -> str:
        import base64
        import os

        if isinstance(video, bytes | bytearray):
            payload = base64.b64encode(bytes(video)).decode("ascii")
            return "data:video/mp4;base64," + payload
        if isinstance(video, str):
            # A URL (remote or data:) passes through; a local file is inlined.
            if "://" in video or video.startswith("data:"):
                return video
            if os.path.isfile(video):
                with open(video, "rb") as fh:
                    payload = base64.b64encode(fh.read()).decode("ascii")
                return "data:video/mp4;base64," + payload
            return video
        raise TypeError("see_video() expects a URL, a file path, or video bytes")

    @staticmethod
    def _as_image_url(image: Any) -> str:
        if isinstance(image, str):
            return image
        if isinstance(image, bytes | bytearray):
            import base64

            payload = base64.b64encode(bytes(image)).decode("ascii")
            return "data:image/jpeg;base64," + payload
        from thingctx.bindings.builtin.media import Frame
        from thingctx.bindings.builtin.media.encode import frame_to_data_url

        if isinstance(image, Frame):
            return frame_to_data_url(image)
        raise TypeError("see() expects a Frame, JPEG bytes, or an image URL")

    async def _run(self, user_content) -> str:
        # user_content is a plain string or OpenAI multimodal content
        # (a list of text/image_url parts), so a VLM host can pass images.
        messages: list[dict[str, Any]] = []
        if self._system:
            messages.append({"role": "system", "content": self._system})
        messages.append({"role": "user", "content": user_content})

        tools = self._openai_tools()
        chat = self._chat_fn or self._litellm_chat
        memo: dict[tuple, str] = {}  # only used when resilient

        for _ in range(self._max_rounds):
            assistant = await chat(messages, tools)
            messages.append(assistant)
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                return assistant.get("content") or ""

            all_repeats = True
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
                if self._resilient and (name, raw_args) in memo:
                    result_text = memo[(name, raw_args)]  # cached; don't re-run
                else:
                    all_repeats = False
                    result_text = to_text(await self._client.call_tool(name, args))
                    if self._resilient:
                        memo[(name, raw_args)] = result_text
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", name),
                        "name": name,
                        "content": result_text,
                    }
                )

            # resilient: all calls were repeats, so force a no-tools turn.
            if self._resilient and all_repeats:
                final = await chat(messages, [])
                return final.get("content") or _summary_from_memo(memo)

        if self._resilient:
            final = await chat(messages, [])
            return final.get("content") or _summary_from_memo(memo)
        return "(max tool rounds reached)"

    async def monitor(
        self,
        event_or_property: str,
        instruction: str,
        *,
        max_events: int = 10,
        on_reaction=None,
    ) -> list[str]:
        """Subscribe and run an LLM turn per pushed value, with the
        Thing's actions available. Returns per-event answers; stops after
        max_events. on_reaction(value, answer) runs after each."""
        stream = await self._client.subscribe(event_or_property)
        reactions: list[str] = []
        count = 0
        async for value in stream:
            answer = await self.chat(
                f"{instruction}\n\nTelemetry just arrived from "
                f"{event_or_property}: {to_text(value)}"
            )
            reactions.append(answer)
            if on_reaction is not None:
                res = on_reaction(value, answer)
                if hasattr(res, "__await__"):
                    await res
            count += 1
            if count >= max_events:
                break
        return reactions

    async def summarize_telemetry(
        self,
        event_or_property: str,
        instruction: str,
        *,
        samples: int = 5,
    ) -> str:
        """Collect samples pushed values, then run one LLM turn over the
        batch."""
        stream = await self._client.subscribe(event_or_property)
        collected: list[Any] = []
        async for value in stream:
            collected.append(value)
            if len(collected) >= samples:
                break
        return await self.chat(
            f"{instruction}\n\nHere are {len(collected)} telemetry "
            f"readings from {event_or_property}: {to_text(collected)}"
        )

    async def _litellm_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        import asyncio

        import litellm  # imported lazily; pure client has no LLM dep

        resp = await asyncio.to_thread(
            litellm.completion,
            model=self._model,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
        )
        msg = resp["choices"][0]["message"]
        content = _get(msg, "content")
        tool_calls = _get(msg, "tool_calls") or []
        out: dict[str, Any] = {"role": "assistant"}
        # An assistant turn with tool_calls must carry content=None (not
        # ""), or some models re-issue the call instead of answering.
        out["content"] = content if not tool_calls else (content or None)
        if tool_calls:
            out["tool_calls"] = [
                {
                    "id": _get(tc, "id"),
                    "type": "function",
                    "function": {
                        "name": _get(_get(tc, "function"), "name"),
                        "arguments": _get(_get(tc, "function"), "arguments"),
                    },
                }
                for tc in tool_calls
            ]
        return out
