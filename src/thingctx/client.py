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
from typing import TYPE_CHECKING, Any

from thingctx.bindings import BindingRegistry, ProtocolBinding
from thingctx.contrib.llm import LLMHost
from thingctx.registry import _max_td_bytes, _user_agent
from thingctx.runtime import ThingClient

if TYPE_CHECKING:
    from thingctx.trust import ApprovePolicy


def from_td(
    td: dict[str, Any] | list[dict[str, Any]],
    *,
    model: str | None = None,
    bindings: BindingRegistry | list[ProtocolBinding] | None = None,
    validate: bool = False,
    approve: Any = None,
    approve_when: ApprovePolicy = "declared",
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
    model: str | None = None,
    bindings: BindingRegistry | list[ProtocolBinding] | None = None,
    **host_kwargs: Any,
) -> LLMHost:
    """From a ``.td.json`` file (one TD or a list of TDs)."""
    data = json.loads(Path(path).read_text())
    return from_td(data, model=model, bindings=bindings, **host_kwargs)


async def from_url(
    url: str,
    *,
    model: str | None = None,
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

    limit = _max_td_bytes()
    async with httpx.AsyncClient(headers={"User-Agent": _user_agent()}, timeout=timeout) as http:
        # Stream and enforce the cap while reading, so an oversized document is
        # cut off mid-download rather than fully buffered before the check.
        async with http.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if limit is not None and total > limit:
                    raise ValueError(f"Thing Description at {url!r} exceeds the {limit}-byte limit")
                chunks.append(chunk)
        td = json.loads(b"".join(chunks))
    return from_td(td, model=model, bindings=bindings, **host_kwargs)
