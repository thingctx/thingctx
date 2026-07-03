# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
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

from thingctx.bindings import BindingRegistry, ProtocolBinding
from thingctx.contrib.llm import LLMHost
from thingctx.runtime import ThingClient


def from_td(
    td: dict[str, Any] | list[dict[str, Any]],
    *,
    model: str = "anthropic/claude-sonnet-4-6",
    bindings: BindingRegistry | list[ProtocolBinding] | None = None,
    validate: bool = False,
    approve: Any = None,
    approve_when: str = "declared",
    **host_kwargs: Any,
) -> LLMHost:
    """From one or more TD dicts. Defaults to http + local bindings; pass
    ``bindings=`` (a BindingRegistry or a list) for mqtt, media, or a custom
    transport a TD uses. ``validate=True`` checks each TD against the W3C TD 1.1
    schema. ``approve`` / ``approve_when`` gate risky calls (see thingctx.trust)."""
    tds = td if isinstance(td, list) else [td]
    client = ThingClient(
        tds=tds,
        bindings=bindings,
        validate=validate,
        approve=approve,
        approve_when=approve_when,
    )
    return LLMHost(client, model=model, **host_kwargs)


def from_file(
    path: str | Path,
    *,
    model: str = "anthropic/claude-sonnet-4-6",
    bindings: BindingRegistry | list[ProtocolBinding] | None = None,
    **host_kwargs: Any,
) -> LLMHost:
    """From a ``.td.json`` file (one TD or a list of TDs)."""
    data = json.loads(Path(path).read_text())
    return from_td(data, model=model, bindings=bindings, **host_kwargs)


async def from_url(
    url: str,
    *,
    model: str = "anthropic/claude-sonnet-4-6",
    bindings: BindingRegistry | list[ProtocolBinding] | None = None,
    timeout: float = 10.0,
    **host_kwargs: Any,
) -> LLMHost:
    """Fetch a live Thing's TD from ``url`` and return a ready host.

    ``url`` points at the Thing Description document (e.g.
    ``http://device.local/.well-known/wot`` or a TD-Directory entry).
    The device side is WoT's, thingctx just consumes the document. The fetch has
    a ``timeout`` and caps the document size (see ``THINGCTX_MAX_TD_BYTES``) so a
    slow or oversized response cannot hang or exhaust the client.
    """
    import httpx

    from thingctx.registry import _max_td_bytes, _user_agent

    limit = _max_td_bytes()
    async with httpx.AsyncClient(headers={"User-Agent": _user_agent()}, timeout=timeout) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        if limit is not None and len(resp.content) > limit:
            raise ValueError(f"Thing Description at {url!r} exceeds the {limit}-byte limit")
        td = resp.json()
    return from_td(td, model=model, bindings=bindings, **host_kwargs)
