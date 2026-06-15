"""A registry is anything that yields Thing Descriptions.

The MCP server takes a Registry, not a fixed source, so "where the TDs
come from" is pluggable. Implement one method:

    class Registry(Protocol):
        def fetch(self) -> list[dict]: ...   # the current TDs

Built in: FileRegistry (a dir or file), TDDRegistry (a W3C Thing
Description Directory), and from_args() which picks per argument. Your own
source (a database, an inventory service, mDNS) is just another class with
a fetch().
"""

from __future__ import annotations

import json
import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class Registry(Protocol):
    def fetch(self) -> list[dict]:
        """Return the current set of Thing Descriptions."""
        ...


class FileRegistry:
    """TDs from a directory of *.td.json, a single file, or a URL that
    returns one TD. Re-reads on each fetch."""

    def __init__(self, source: str, timeout: float = 10.0) -> None:
        self.source = source
        self.timeout = timeout

    def fetch(self) -> list[dict]:
        s = self.source
        if s.startswith(("http://", "https://")):
            return [_get_json(s, self.timeout)]
        if os.path.isdir(s):
            files = [
                os.path.join(s, f)
                for f in sorted(os.listdir(s))
                if f.endswith((".td.json", ".json"))
            ]
            return [json.loads(open(f).read()) for f in files]
        return [json.loads(open(s).read())]


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
    def __init__(self, registries):
        self.registries = registries

    def fetch(self) -> list[dict]:
        out: list[dict] = []
        for r in self.registries:
            out.extend(r.fetch())
        return out


def from_arg(arg: str) -> Registry:
    """Pick a registry from one argument: a `tdd:URL` or a `/things` URL is
    a Thing Description Directory; anything else (a dir, file, or single-TD
    URL) is a FileRegistry."""
    if arg.startswith("tdd:"):
        return TDDRegistry(arg[4:])
    if arg.startswith(("http://", "https://")) and arg.rstrip("/").endswith("/things"):
        return TDDRegistry(arg)
    return FileRegistry(arg)


def from_args(args: list[str]) -> Registry:
    """One registry from many args (mix files, dirs, URLs, TDDs)."""
    regs = [from_arg(a) for a in args]
    return regs[0] if len(regs) == 1 else _Multi(regs)


def _user_agent() -> str:
    """A real User-Agent. Some hosts (e.g. Cloudflare) reject the default
    ``Python-urllib/x.y`` UA with HTTP 403, which would break fetching a TD
    from a hosted registry."""
    try:
        from importlib.metadata import version

        return f"thingctx/{version('thingctx')}"
    except Exception:  # noqa: BLE001
        return "thingctx"


def _get_json(url: str, timeout: float):
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())
