# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Connect a user-authorized Thing on demand, from inside the MCP bridge.

When a tool call needs a user token the store does not have yet, the bridge asks
the connected client to confirm (MCP elicitation), runs the one-time browser
consent locally, stores the refresh token, and lets the call proceed. The agent
never handles the credential: the token lives only in the local token store and
thingctx attaches it when it reaches the system.

The OAuth client (the registered app's id and secret) is operator supplied, one
file per provider under ``~/.config/thingctx/oauth-clients/<token-host>.json``
(Google-style ``installed``/``web`` client-secrets JSON). It is never in a TD and
never in the agent config, so the whole path scales to many Things and many
providers with no secret in the agent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _clients_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "thingctx" / "oauth-clients"


def _code_scheme(thing: Any):
    """The Thing's oauth2 authorization-code scheme, or None."""
    for s in getattr(thing, "security_schemes", {}).values():
        if getattr(s, "scheme", None) == "oauth2" and getattr(s, "flow", "") in (
            "code",
            "authorization_code",
        ):
            return s
    return None


def _client_file_for(token_url: str) -> Path | None:
    """The operator's OAuth client file for a provider, keyed by the token
    endpoint host (``oauth2.googleapis.com.json``). One file per provider serves
    every Thing that uses it."""
    host = urlparse(token_url).netloc
    if not host:
        return None
    path = _clients_dir() / f"{host}.json"
    return path if path.exists() else None


def _load_client(path: Path) -> dict:
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    blob = data.get("installed") or data.get("web") or data
    return {
        "client_id": blob.get("client_id", ""),
        "client_secret": blob.get("client_secret"),
    }


def _has_token(thing_id: str, scheme) -> bool:
    """Whether the store already holds a refresh token for this Thing + scope."""
    from thingctx.auth.store import default_token_store, token_key

    scopes = tuple(getattr(scheme, "scopes", ()) or ())
    key = token_key(thing_id, getattr(scheme, "token", ""), scopes)
    return bool(default_token_store().get(key))


def thing_for_tool(client: Any, tool: str):
    """The Thing that owns a ``<slug>.<name>`` tool, or None."""
    action = client.action_for(tool) if hasattr(client, "action_for") else None
    tid = getattr(action, "thing_id", None)
    if tid is None:
        slug = tool.split(".", 1)[0]
        from thingctx.thing import thing_slug

        for t in client.things:
            if thing_slug(t.id) == slug:
                return t
        return None
    for t in client.things:
        if t.id == tid:
            return t
    return None


CONNECT_TOOL = "connect"


def connect_status(client: Any) -> list[dict]:
    """Every user-authorized Thing in the registry and whether it is connected.
    Drives the ``connect`` tool's listing and lets the agent see what needs a
    sign in before it tries an action."""
    out = []
    for t in client.things:
        scheme = _code_scheme(t)
        if scheme is None:
            continue
        out.append(
            {
                "thing": t.id,
                "title": t.title or t.id,
                "connected": _has_token(t.id, scheme),
            }
        )
    return out


def _thing_by_name(client: Any, name: str):
    """Match a Thing by id, title, or tool-namespace slug, exact or as a
    substring of the title (what a user would type: "calendar", "google
    calendar"). Returns the single match, or None when zero or ambiguous."""
    from thingctx.thing import thing_slug

    n = (name or "").strip().lower()
    if not n:
        return None
    exact = [
        t
        for t in client.things
        if n in (t.id.lower(), (t.title or "").lower(), thing_slug(t.id).lower())
    ]
    if exact:
        return exact[0]
    # Fall back to a title substring, but only when it names exactly one Thing.
    fuzzy = [t for t in client.things if n in (t.title or "").lower()]
    return fuzzy[0] if len(fuzzy) == 1 else None


async def connect_tool(client: Any, args: dict, session: Any) -> dict:
    """The ``connect`` tool body. With no ``thing`` argument, list what needs a
    sign in. With one, run consent for that Thing. Always confirms with the human
    (elicitation) before a browser opens, so an agent call cannot silently start
    a login."""
    name = (args or {}).get("thing")
    if not name:
        status = connect_status(client)
        pending = [s["title"] for s in status if not s["connected"]]
        if pending:
            msg = f"Say 'connect {pending[0]}' (or another) to sign in: " + ", ".join(pending)
        else:
            msg = "Nothing needs connecting."
        return {"services": status, "message": msg}
    thing = _thing_by_name(client, name)
    if thing is None:
        return {"error": f"no Thing matches {name!r}"}
    scheme = _code_scheme(thing)
    if scheme is None:
        return {"error": f"{thing.title or thing.id} does not use a user sign in."}
    if _has_token(thing.id, scheme):
        return {"connected": True, "message": f"{thing.title or thing.id} is already connected."}
    err = await _run_connect(thing, scheme, session)
    if err:
        return {"error": err}
    return {"connected": True, "message": f"Connected {thing.title or thing.id}."}


async def _run_connect(thing: Any, scheme: Any, session: Any) -> str | None:
    """Confirm with the human, run the one-time browser consent, store the token.
    Shared by the ``connect`` tool and the auto-connect path. Returns None on
    success, or an error string (no client file, declined, or consent failed)."""
    client_file = _client_file_for(getattr(scheme, "token", ""))
    if client_file is None:
        host = urlparse(getattr(scheme, "token", "")).netloc or "the provider"
        return (
            f"{thing.title} needs a one-time sign in, but no OAuth client is "
            f"configured for {host}. Add the client-secrets file at "
            f"{_clients_dir()}/{host}.json."
        )

    label = thing.title or thing.id
    # Ask the connected client (the agent's UI) to confirm before a browser opens.
    # An agent-triggered call must never silently start a login (the browser only
    # opens after the human approves here).
    if session is not None:
        try:
            result = await session.elicit(
                message=(
                    f"Connect {label}? This opens a browser once so you can sign in "
                    f"and grant access. No password or token is shared with the agent."
                ),
                requestedSchema={"type": "object", "properties": {}},
            )
            if getattr(result, "action", None) != "accept":
                return f"{label} was not connected (sign in declined)."
        except Exception:  # noqa: BLE001 - client without elicitation: fall through to consent
            pass

    from thingctx.auth.oauth_consent import login

    creds = _load_client(client_file)
    try:
        login(
            authorization_url=getattr(scheme, "authorization", ""),
            token_url=getattr(scheme, "token", ""),
            client_id=creds["client_id"],
            client_secret=creds.get("client_secret"),
            scopes=tuple(getattr(scheme, "scopes", ()) or ()),
            owner_id=thing.id,
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean message, not a traceback
        return f"Could not connect {label}: {exc}"
    return None


async def ensure_connected(client: Any, tool: str, session: Any) -> str | None:
    """If ``tool``'s Thing needs a user token the store lacks, ask the client to
    confirm, run consent, and store the token. Returns None when nothing to do or
    the connect succeeded; returns an error string when it cannot proceed."""
    thing = thing_for_tool(client, tool)
    if thing is None:
        return None
    scheme = _code_scheme(thing)
    if scheme is None or _has_token(thing.id, scheme):
        return None  # not a user-auth Thing, or already connected
    return await _run_connect(thing, scheme, session)
