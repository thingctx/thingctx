# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Sandboxed filesystem handler for the registry ``filesystem`` Thing.

Bound when ``thingctx[filesystem]`` is installed (entry point
``thingctx.local_handlers`` / ``filesystem``). Confines every path under
``THINGCTX_FS_ROOT``; refuse every call when that env is unset. Method names
match the TD forms: ``readFile``, ``writeFile``, ``listDir``.
"""

from __future__ import annotations

import os
from pathlib import Path

from thingctx.netpolicy import PolicyError, confine_path

# Single-read / single-write byte cap. Override with THINGCTX_FS_MAX_BYTES.
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024


def _root() -> Path:
    raw = (os.environ.get("THINGCTX_FS_ROOT") or "").strip()
    if not raw:
        raise PolicyError(
            "THINGCTX_FS_ROOT is unset: refusing filesystem access "
            "(set it to a directory you are willing to expose)"
        )
    return Path(raw).expanduser().resolve()


def _max_bytes() -> int:
    raw = (os.environ.get("THINGCTX_FS_MAX_BYTES") or "").strip()
    if not raw:
        return _DEFAULT_MAX_BYTES
    try:
        n = int(raw)
    except ValueError as exc:
        raise PolicyError(f"THINGCTX_FS_MAX_BYTES must be an integer, got {raw!r}") from exc
    if n <= 0:
        raise PolicyError("THINGCTX_FS_MAX_BYTES must be positive")
    return n


def _confine(path: str) -> Path:
    """Resolve ``path`` under the sandbox root; refuse escapes and outbound symlinks."""
    return confine_path(path, base=_root())


class FilesystemHandler:
    """In-process handler for ``urn:thingctx:filesystem``."""

    def readFile(self, path: str) -> dict:  # noqa: N802 - matches TD action name
        p = _confine(path)
        if not p.is_file():
            raise FileNotFoundError(f"not a file: {path!r}")
        size = p.stat().st_size
        cap = _max_bytes()
        if size > cap:
            raise PolicyError(
                f"file {path!r} is {size} bytes; cap is {cap} (THINGCTX_FS_MAX_BYTES)"
            )
        return {"path": path, "contents": p.read_text(encoding="utf-8", errors="replace")}

    def writeFile(self, path: str, contents: str) -> dict:  # noqa: N802
        data = contents if isinstance(contents, str) else str(contents)
        cap = _max_bytes()
        encoded = data.encode("utf-8")
        if len(encoded) > cap:
            raise PolicyError(
                f"write of {len(encoded)} bytes exceeds cap {cap} (THINGCTX_FS_MAX_BYTES)"
            )
        p = _confine(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(encoded)
        return {"path": path, "written": len(encoded)}

    def listDir(self, path: str = ".") -> dict:  # noqa: N802
        p = _confine(path)
        if not p.is_dir():
            raise NotADirectoryError(f"not a directory: {path!r}")
        return {"path": path, "entries": sorted(os.listdir(p))}


def make_filesystem_handler() -> FilesystemHandler:
    """Entry-point factory for ``thingctx.local_handlers``."""
    return FilesystemHandler()
