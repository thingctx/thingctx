# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""A registry is anything that yields Thing Descriptions.

The MCP server takes a Registry, not a fixed source, so "where the TDs
come from" is pluggable. Implement one method:

    class Registry(Protocol):
        def fetch(self) -> list[dict]: ...   # the current TDs

Built in: FileRegistry (a dir, a file, or a URL serving a TD or a catalog
index), TDDRegistry (a W3C Thing Description Directory), and from_args()
which picks per argument. Your own
source (a database, an inventory service, mDNS) is just another class with
a fetch().
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import urljoin


@runtime_checkable
class Registry(Protocol):
    def fetch(self) -> list[dict]:
        """Return the current set of Thing Descriptions."""


class FileRegistry:
    """TDs from a directory of *.td.json, a single file, or a URL that
    returns one TD or a catalog index of TDs. Re-reads on each fetch."""

    def __init__(self, source: str, timeout: float = 10.0) -> None:
        self.source = source
        self.timeout = timeout

    def fetch(self) -> list[dict]:
        s = self.source
        if s.startswith(("http://", "https://")):
            return _tds_from_url(s, self.timeout)
        path = Path(s)
        if path.is_dir():
            files = sorted(p for p in path.iterdir() if p.name.endswith((".td.json", ".json")))
            return [_read_json_file(p) for p in files]
        return [_read_json_file(path)]


class TDDRegistry:
    """TDs from a W3C Thing Description Directory: one URL, a whole fabric
    of devices. The TDD lists Things at its /things endpoint."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def fetch(self) -> list[dict]:
        url = self.base_url
        if not url.endswith("/things"):
            url += "/things"
        data = _get_json(url, self.timeout)
        if isinstance(data, dict):  # some TDDs wrap the list
            data = data.get("members") or data.get("things") or [data]
        return list(data)


class _Multi:
    def __init__(self, registries: list[Registry]) -> None:
        self.registries = registries

    def fetch(self) -> list[dict]:
        out: list[dict] = []
        for r in self.registries:
            out.extend(r.fetch())
        return out


def from_arg(arg: str) -> Registry:
    """Pick a registry from one argument: a `tdd:URL` or a `/things` URL is
    a Thing Description Directory; anything else (a dir, a file, or a URL
    serving a TD or a catalog index) is a FileRegistry."""
    if arg.startswith("tdd:"):
        return TDDRegistry(arg[4:])
    if arg.startswith(("http://", "https://")) and arg.rstrip("/").endswith("/things"):
        return TDDRegistry(arg)
    return FileRegistry(arg)


def from_args(args: list[str]) -> Registry:
    """One registry from many args (mix files, dirs, URLs, TDDs). With no args,
    an empty registry (yields no TDs) rather than an error."""
    regs = [from_arg(a) for a in args]
    if not regs:
        return _Multi([])
    return regs[0] if len(regs) == 1 else _Multi(regs)


def default_registry_dir() -> Path:
    """The per-user default registry directory,
    ``$XDG_CONFIG_HOME/thingctx/registry`` (``~/.config`` when unset), matching
    the token store's convention (see thingctx.auth.store)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "thingctx" / "registry"


def default_sources() -> list[str]:
    """The sources the default registry resolves, so a caller can drive Things
    with no explicit source:

    - ``$THINGCTX_REGISTRY`` if set (``os.pathsep``-separated), else
    - the default registry directory (its ``*.td.json`` / ``*.json`` files) plus
      any non-comment lines in its ``sources.txt`` (URLs / ``tdd:`` directories
      that cannot be stored as files).
    """
    env = os.environ.get("THINGCTX_REGISTRY")
    if env:
        return [s for s in (p.strip() for p in env.split(os.pathsep)) if s]
    out: list[str] = []
    d = default_registry_dir()
    if d.is_dir():
        out.append(str(d))
    sources_file = d / "sources.txt"
    if sources_file.is_file():
        for line in sources_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def default_registry() -> Registry:
    """A registry over the user's default Things (see :func:`default_sources`).
    Empty when nothing is configured, so a source-less ``thingctx list`` shows
    nothing and ``thingctx invoke`` reports an unknown action, never a crash."""
    return from_args(default_sources())


def _user_agent() -> str:
    """A real User-Agent. Some hosts (e.g. Cloudflare) reject the default
    ``Python-urllib/x.y`` UA with HTTP 403, which would break fetching a TD
    from a hosted registry."""
    try:
        return f"thingctx/{version('thingctx')}"
    except Exception:
        return "thingctx"


# A Thing Description is a small JSON document. Cap what a fetch or a file read
# will pull into memory so a hostile or misconfigured source cannot return a
# huge body and exhaust it. Override with THINGCTX_MAX_TD_BYTES (0 disables).
DEFAULT_MAX_TD_BYTES = 16 * 1024 * 1024


def _max_td_bytes() -> int | None:
    v = os.environ.get("THINGCTX_MAX_TD_BYTES")
    if v is None:
        return DEFAULT_MAX_TD_BYTES
    v = v.strip()
    if v in ("", "0"):
        return None
    return int(v)


def _read_json_file(path: str | Path) -> dict:
    path = Path(path)
    limit = _max_td_bytes()
    size = path.stat().st_size
    if limit is not None and size > limit:
        raise ValueError(
            f"Thing Description file {str(path)!r} is {size} bytes, over the {limit} limit"
        )
    return cast("dict", json.loads(path.read_text(encoding="utf-8")))


def _get_json(url: str, timeout: float) -> Any:
    # Only http(s): a registry URL must not be able to read a local file or reach
    # a custom scheme handler through urlopen.
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"registry URL must be http(s), got {url!r}")
    limit = _max_td_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})  # noqa: S310 (scheme checked above)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (scheme checked above)
        if limit is None:
            raw = r.read()
        else:
            # Read one byte past the cap so an oversized body is detected, not
            # silently truncated into invalid JSON.
            raw = r.read(limit + 1)
            if len(raw) > limit:
                raise ValueError(f"Thing Description at {url!r} exceeds the {limit}-byte limit")
        headers = getattr(r, "headers", None)
    ctype = (headers.get("Content-Type", "") if headers else "") or "unknown content type"
    try:
        return json.loads(raw.decode())
    except ValueError as e:
        raise ValueError(
            f"{url} did not return JSON ({ctype}); expected a Thing Description or a catalog index"
        ) from e


def _tds_from_url(url: str, timeout: float) -> list[dict]:
    # A trailing slash names a directory of TDs; its index.json is the catalog.
    if url.endswith("/"):
        url += "index.json"
    data = _get_json(url, timeout)
    return _tds_from_payload(data, url, lambda u: _get_json(u, timeout))


def _tds_from_payload(data: Any, base_url: str, get: Callable[[str], Any]) -> list[dict]:
    """TDs from one fetched JSON payload.

    Three shapes: a single TD (a dict; anything with an @context, or any
    dict without a "things" list), a bare list of TDs, or a catalog index
    (a dict whose "things" entries are inline TDs or references). A
    reference names its TD in "served_at", "href", or "file", resolved
    against the catalog URL, so a static registry needs only an index file
    beside its TDs.
    """
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if not isinstance(data, dict):
        # ValueError, not TypeError: the fetched payload has the wrong SHAPE, this
        # is not a caller passing a wrong-typed argument.
        raise ValueError(f"{base_url} did not return a TD or a catalog index")  # noqa: TRY004
    if "@context" in data or not isinstance(data.get("things"), list):
        return [data]

    out: list[dict] = []
    for entry in data["things"]:
        if not isinstance(entry, dict):
            continue
        if "@context" in entry:
            out.append(entry)
            continue
        ref = entry.get("served_at") or entry.get("href") or entry.get("file")
        if isinstance(ref, str) and ref:
            out.append(get(urljoin(base_url, ref)))
    return out
