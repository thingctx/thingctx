# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Durable storage for long-lived OAuth refresh tokens.

A refresh token outlives the process, so the in-memory access-token cache
(``AuthContext.cache``) cannot hold it: the user consents once and every later
run refreshes silently. That requires a place to keep the refresh token between
runs. This module is that place.

A refresh token is a bearer secret with a long life, so the file backend writes
``0600`` and never logs a value. The contract is the :class:`TokenStore`
protocol; swap in an OS-keychain backend by implementing the same three methods.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = ["TokenStore", "MemoryTokenStore", "FileTokenStore", "token_key", "default_token_store"]


def token_key(owner_id: str | None, token_url: str, scopes: tuple[str, ...] | list[str]) -> str:
    """Stable key for one user's grant: owner (the Thing id at resolve time),
    token endpoint, and the sorted scope set. The same inputs at consent time
    and at refresh time must produce the same key, so a token persisted by
    ``thingctx auth login`` is found by the provider."""
    scope = " ".join(sorted(s for s in (scopes or ()) if s))
    return f"{owner_id or ''}|{token_url}|{scope}"


@runtime_checkable
class TokenStore(Protocol):
    """Persist and retrieve a token record (a JSON-able mapping that carries at
    least ``refresh_token``) keyed by :func:`token_key`."""

    def get(self, key: str) -> dict[str, Any] | None:
        pass

    def set(self, key: str, record: dict[str, Any]) -> None:
        pass

    def delete(self, key: str) -> None:
        pass


class MemoryTokenStore:
    """In-process store. Useful for tests and for a single long-lived process;
    it does not survive a restart, so it does not solve the persistence gap on
    its own."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        rec = self._data.get(key)
        return dict(rec) if rec is not None else None

    def set(self, key: str, record: dict[str, Any]) -> None:
        self._data[key] = dict(record)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


def _default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "thingctx" / "tokens.json"


class FileTokenStore:
    """A single JSON file of token records, created ``0600`` so other users
    cannot read the refresh tokens. The default location is
    ``$XDG_CONFIG_HOME/thingctx/tokens.json`` (``~/.config`` when unset)."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else _default_path()

    def _load(self) -> dict[str, Any]:
        # Open without following a symlink: a token file swapped for a link must
        # not redirect the read (or a later write) to another location.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return {}
        except OSError as exc:  # e.g. ELOOP when the path is a symlink
            raise OSError(f"refusing to read token store at {self.path!r}: {exc}") from exc
        try:
            with os.fdopen(fd, encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> dict[str, Any] | None:
        rec = self._load().get(key)
        return dict(rec) if isinstance(rec, dict) else None

    def set(self, key: str, record: dict[str, Any]) -> None:
        data = self._load()
        data[key] = dict(record)
        self._write(data)

    def delete(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        # Create the store dir 0700 (only the owner may list it) and refuse to
        # write through a symlink at the token path.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        if self.path.is_symlink():
            raise OSError(f"refusing to write token store through a symlink: {self.path!r}")
        # Write through a temp file in the same dir, 0600 before any secret
        # lands, then atomically replace.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tokens-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)


_DEFAULT_STORE: TokenStore | None = None


def default_token_store() -> TokenStore:
    """The process-wide default :class:`FileTokenStore`, shared by the built-in
    provider and ``thingctx auth login`` so consent and refresh agree on where
    tokens live."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = FileTokenStore()
    return _DEFAULT_STORE
