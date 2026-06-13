"""tc:PromptTemplate: a prompt template, declared as an action annotated
@type "tc:PromptTemplate". Its input is the template arguments; expanding
it yields a list of {role, content} messages.

A TD opts in via @context + @type:

    "@context": ["https://www.w3.org/2022/wot/td/v1.1",
                 {"tc": "https://thingctx.dev/vocab#"}],
    "actions": {
      "diagnose": {
        "@type": "tc:PromptTemplate",
        "input":  {"type": "object", "properties": {"severity": {"type": "string"}}},
        "output": {"type": "array"},
        "forms":  [{"href": "..."}]
      }
    }

    from thingctx.extensions.prompts import list_prompts, get_prompt
    list_prompts(client)
    await get_prompt(client, "pump.diagnose", {"severity": "high"})
"""

from __future__ import annotations

from typing import Any

TERM = "tc:PromptTemplate"


def list_prompts(client: Any) -> list[dict[str, Any]]:
    """Actions annotated @type tc:PromptTemplate. Returns [{name,
    description, arguments}] with arguments from the input schema."""
    out: list[dict[str, Any]] = []
    from thingctx.thing import _tool_name

    for thing in client.things:
        for action in thing.actions.values():
            if not action.has_type(TERM):
                continue
            schema = action.input_schema or {}
            props = schema.get("properties", {})
            required = set(schema.get("required", []))
            out.append(
                {
                    "name": _tool_name(thing.id, action.name),
                    "description": action.description,
                    "arguments": [
                        {
                            "name": k,
                            "description": v.get("description", ""),
                            "required": k in required,
                        }
                        for k, v in props.items()
                    ],
                }
            )
    return out


async def get_prompt(client: Any, name: str, arguments: dict[str, Any] | None = None):
    """Expand a prompt template into [{role, content}] messages. If the
    TD carries tc:template, expand it client-side ({arg} filled from
    arguments); else invoke the action. Raises ValueError if name is not
    a tc:PromptTemplate action."""
    action = client.action_for(name)
    if action is None or not action.has_type(TERM):
        raise ValueError(f"{name!r} is not a tc:PromptTemplate action")
    args = arguments or {}
    template = action.raw.get("tc:template", action.raw.get("template"))
    if template is not None:
        return _expand(template, args)
    return await client.invoke(name, args)


def _expand(template: Any, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Fill {arg} placeholders. A string becomes one user message; a list
    of {role, content} messages is filled per message."""

    def fill(s: str) -> str:
        for k, v in args.items():
            s = s.replace("{" + k + "}", str(v))
        return s

    if isinstance(template, str):
        return [{"role": "user", "content": fill(template)}]
    out = []
    for msg in template:
        content = msg.get("content", "")
        out.append(
            {
                "role": msg.get("role", "user"),
                "content": fill(content) if isinstance(content, str) else content,
            }
        )
    return out


def is_prompt(client: Any, name: str) -> bool:
    """True if ``name`` is a declared prompt-template action."""
    a = client.action_for(name)
    return a is not None and a.has_type(TERM)
