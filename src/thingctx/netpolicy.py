# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Shared URL and filesystem guardrails for third-party input.

A Thing Description is third-party input: its form hrefs, and the arguments an
LLM or caller fills into them, decide what the client connects to and where
bytes land. These helpers give the transports one place to enforce two checks
that are always safe to apply:

* scheme allowlisting, so a response-chained handoff URL or a fetched document
  cannot jump to ``file:``, ``data:``, or another unexpected scheme, and
* write-path confinement, so a download destination cannot escape an intended
  directory or be redirected through a symlink.

A Web of Things client legitimately talks to link-local and private addresses
(a camera at ``192.168.x.y``, a hub at ``device.local``), so private-address
blocking is opt-in (``block_private=True``) rather than the default. Callers that
process fully untrusted TDs can turn it on; the scheme and path guards carry no
such tradeoff and are applied unconditionally where a scheme jump or a stray
write is never legitimate.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Collection
from os import PathLike
from pathlib import Path
from urllib.parse import urlsplit

__all__ = [
    "WEB_SCHEMES",
    "PolicyError",
    "check_url",
    "confine_path",
    "is_private_host",
    "require_scheme",
    "resolve_is_private",
    "url_scheme",
]

# The schemes a plain document fetch or an HTTP response handoff may use.
WEB_SCHEMES = frozenset({"http", "https"})


class PolicyError(ValueError):
    """A URL or path was refused by a safety policy."""


def url_scheme(url: str) -> str:
    """The lowercased URI scheme of ``url`` (``""`` when it has none)."""
    return (urlsplit(url).scheme or "").lower()


def require_scheme(url: str, allowed: Collection[str], *, what: str = "URL") -> str:
    """Return ``url`` if its scheme is in ``allowed``, else raise ``PolicyError``.

    Guards a scheme jump that is never legitimate for the caller (for example a
    response that hands back a ``file://`` "next" URL for an HTTP chain)."""
    scheme = url_scheme(url)
    if scheme not in allowed:
        raise PolicyError(
            f"{what} scheme {scheme or '(none)'!r} is not allowed; "
            f"expected one of {sorted(allowed)}"
        )
    return url


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse ``host`` as an IP literal, or return None. Accepts the canonical
    forms plus the non-canonical IPv4 encodings a URL parser and the OS resolver
    both accept (decimal ``2130706433``, hex ``0x7f.1``, octal, and short forms
    like ``127.1``); ``ipaddress`` alone rejects those, which would let a
    loopback or metadata address written that way slip past a private check."""
    host = (host or "").strip("[]")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # inet_aton accepts the non-canonical IPv4 encodings; normalize to a literal.
    try:
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
    except OSError:
        return None


def is_private_host(host: str) -> bool:
    """Whether ``host`` is an IP literal in a private, loopback, link-local,
    reserved, multicast, or unspecified range. A hostname (not an IP literal)
    returns ``False`` here; use :func:`resolve_is_private` to resolve it first.
    Non-canonical IPv4 encodings (decimal, hex, octal, short form) are normalized
    first, so ``2130706433`` is recognized as loopback."""
    ip = _as_ip(host)
    if ip is None:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolve_is_private(host: str) -> bool:
    """Like :func:`is_private_host`, but also resolves a hostname and returns
    ``True`` if any resolved address is private. Best-effort: a resolution
    failure is treated as not-private (the connection attempt will fail on its
    own)."""
    if is_private_host(host):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return any(is_private_host(ai[4][0]) for ai in infos)


def check_url(
    url: str,
    *,
    allowed_schemes: Collection[str] = WEB_SCHEMES,
    block_private: bool = False,
    resolve: bool = True,
    what: str = "URL",
) -> str:
    """Validate ``url`` and return it. Always enforces ``allowed_schemes``; when
    ``block_private`` is set, refuses a host that is, or resolves to, a private,
    loopback, or link-local address. Resolution is on by default: a private-host
    block that skipped hostnames would miss ``localhost`` and a cloud metadata
    name, so pass ``resolve=False`` only when the host is already a literal."""
    require_scheme(url, allowed_schemes, what=what)
    if block_private:
        host = urlsplit(url).hostname or ""
        private = resolve_is_private(host) if resolve else is_private_host(host)
        if private:
            raise PolicyError(
                f"{what} host {host!r} is a private or loopback address (blocked by policy)"
            )
    return url


def confine_path(dest: str | PathLike[str], *, base: str | PathLike[str] | None = None) -> Path:
    """Validate a write destination and return it as a :class:`Path`.

    Refuses writing through a symlink at the destination (a symlink swap that
    would redirect the write elsewhere). When ``base`` is given, the destination
    must resolve inside it: a relative ``dest`` is taken under ``base`` and an
    absolute or traversing ``dest`` that escapes ``base`` is refused."""
    p = Path(dest)
    if p.is_symlink():
        raise PolicyError(f"refusing to write through a symlink: {str(dest)!r}")
    if base is not None:
        base_r = Path(base).resolve()
        target = (p if p.is_absolute() else base_r / p).resolve()
        try:
            target.relative_to(base_r)
        except ValueError:
            raise PolicyError(
                f"destination {str(dest)!r} escapes the allowed directory {str(base)!r}"
            ) from None
        return target
    return p
