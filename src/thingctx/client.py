"""Consume a TD and return a ready LLMHost.

    host = await thingctx.from_url("http://device.local/.well-known/wot")
    host = thingctx.from_file("pump.td.json")
    host = thingctx.from_td(td_dict)

Each builds a ThingClient and wraps it in an LLMHost. For the pure client,
build a ThingClient directly or read host.client.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from thingctx.contrib.llm import LLMHost
from thingctx.invokers import HttpInvoker, Invoker, LocalInvoker
from thingctx.runtime import ThingClient


def _default_invokers() -> list[Invoker]:
    # HTTP covers http(s) forms; Local covers local:// forms. Add MQTT
    # etc. explicitly via the ``invokers=`` argument when a TD uses them.
    return [HttpInvoker(), LocalInvoker()]


def from_td(
    td: dict[str, Any] | list[dict[str, Any]],
    *,
    model: str = "anthropic/claude-sonnet-4-6",
    invokers: list[Invoker] | None = None,
    validate: bool = False,
    approve: Any = None,
    approve_when: str = "declared",
    **host_kwargs: Any,
) -> LLMHost:
    """From one or more TD dicts. Defaults to HTTP + local invokers;
    pass ``invokers=`` for MQTT/CoAP/custom transports a TD uses.
    ``validate=True`` checks each TD against the W3C TD 1.1 schema.
    ``approve`` / ``approve_when`` gate risky calls (see thingctx.trust)."""
    tds = td if isinstance(td, list) else [td]
    client = ThingClient(
        tds=tds,
        invokers=invokers if invokers is not None else _default_invokers(),
        validate=validate,
        approve=approve,
        approve_when=approve_when,
    )
    return LLMHost(client, model=model, **host_kwargs)


def from_file(
    path: str | Path,
    *,
    model: str = "anthropic/claude-sonnet-4-6",
    invokers: list[Invoker] | None = None,
    **host_kwargs: Any,
) -> LLMHost:
    """From a ``.td.json`` file (one TD or a list of TDs)."""
    data = json.loads(Path(path).read_text())
    return from_td(data, model=model, invokers=invokers, **host_kwargs)


async def from_url(
    url: str,
    *,
    model: str = "anthropic/claude-sonnet-4-6",
    invokers: list[Invoker] | None = None,
    **host_kwargs: Any,
) -> LLMHost:
    """Fetch a live Thing's TD from ``url`` and return a ready host.

    ``url`` points at the Thing Description document (e.g.
    ``http://device.local/.well-known/wot`` or a TD-Directory entry).
    The device side is WoT's, thingctx just consumes the document.
    """
    import httpx

    from thingctx.registry import _user_agent

    async with httpx.AsyncClient(headers={"User-Agent": _user_agent()}) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        td = resp.json()
    return from_td(td, model=model, invokers=invokers, **host_kwargs)
