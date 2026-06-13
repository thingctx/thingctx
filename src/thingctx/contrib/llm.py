"""LLMHost: a tool-calling loop over a ThingClient. litellm is imported
lazily here only, so the pure ThingClient has no LLM dependency.

    client = ThingClient(tds=[td], invokers=[HttpInvoker()])
    host = LLMHost(client, model="anthropic/claude-sonnet-4-6")
    print(await host.chat("read temp-1 and report it"))
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from thingctx.runtime import ThingClient, to_text


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
        model: str = "anthropic/claude-sonnet-4-6",
        system: str | None = None,
        max_rounds: int = 8,
        chat_fn: ChatFn | None = None,
        resilient: bool = False,
    ) -> None:
        self._client = client
        self._model = model
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
        return self._client.list_actions()

    async def chat(self, prompt: str) -> str:
        """Run the tool-calling loop for one prompt; return the final text
        answer. See resilient for the weaker-model guards."""
        return await self._run(prompt)

    async def _run(self, user_content) -> str:
        # user_content is a plain string or OpenAI multimodal content
        # (a list of text/image_url parts), so a VLM host can pass images.
        messages: list[dict[str, Any]] = []
        if self._system:
            messages.append({"role": "system", "content": self._system})
        messages.append({"role": "user", "content": user_content})

        tools = self._client.list_actions()
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
                    result_text = to_text(await self._client.invoke(name, args))
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
