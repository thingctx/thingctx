# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""URL and filesystem guardrails for third-party input.

A TD's form hrefs, and the arguments an LLM fills into them, decide what the
client connects to and where bytes land. Two guards apply unconditionally: scheme
allowlisting (a handoff URL cannot jump to ``file:`` or ``data:``) and write-path
confinement (a download cannot escape a directory or follow a symlink).

Private-address blocking is opt-in (``block_private``), because a WoT client
legitimately talks to ``192.168.x.y`` and ``device.local``. Turn it on for fully
untrusted TDs.
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
    "resolve_and_pin",
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

    Blocks a scheme jump, e.g. a response that hands back a ``file://`` next URL
    for an HTTP chain."""
    scheme = url_scheme(url)
    if scheme not in allowed:
        raise PolicyError(
            f"{what} scheme {scheme or '(none)'!r} is not allowed; "
            f"expected one of {sorted(allowed)}"
        )
    return url


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse ``host`` as an IP literal, or return None.

    Also accepts the non-canonical IPv4 encodings the OS resolver accepts (decimal
    ``2130706433``, hex, octal, short forms like ``127.1``). ``ipaddress`` rejects
    those, which would let a loopback or metadata address written that way slip
    past a private check."""
    host = (host or "").strip("[]")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
    except OSError:
        return None


def is_private_host(host: str) -> bool:
    """Whether ``host`` is an IP literal in a private, loopback, link-local,
    reserved, multicast, or unspecified range.

    A hostname returns ``False``; use :func:`resolve_is_private` to resolve it
    first. Non-canonical IPv4 encodings are normalized first, so ``2130706433``
    reads as loopback."""
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


def _resolved_addrs(host: str) -> list[str]:
    """The addresses ``host`` resolves to, as IP strings.

    A sockaddr is family shaped: AF_INET and AF_INET6 start with the address
    string, while a family the socket module does not model arrives as
    ``(sa_family, raw_bytes)``. An entry carrying no address string cannot be
    judged by :func:`is_private_host`, so it is refused rather than skipped:
    this is the SSRF gate, and an answer that cannot be read must not read as
    public. Propagates ``OSError`` from ``getaddrinfo``; each caller sets its
    own policy for a failed resolution."""
    addrs: list[str] = []
    for info in socket.getaddrinfo(host, None):
        addr = info[4][0]
        if not isinstance(addr, str):
            raise PolicyError(f"host {host!r} resolved to an unreadable address entry")
        addrs.append(addr)
    return addrs


def resolve_is_private(host: str) -> bool:
    """Like :func:`is_private_host`, but resolves a hostname first and returns
    ``True`` if any resolved address is private.

    A resolution failure is treated as not-private; the connection attempt will
    fail on its own. An address the policy cannot read is treated as private,
    since it cannot be shown to be public."""
    if is_private_host(host):
        return True
    try:
        addrs = _resolved_addrs(host)
    except OSError:
        return False
    except PolicyError:
        return True
    return any(is_private_host(addr) for addr in addrs)


def resolve_and_pin(host: str, *, what: str = "URL") -> str:
    """Resolve ``host`` once, refuse it if it (or any address it resolves to) is
    private/loopback/link-local/metadata, and return the single validated IP to
    connect to.

    This closes the DNS-rebinding TOCTOU that a plain resolve-then-check leaves
    open: :func:`resolve_is_private` validates the resolved set, but the OS
    re-resolves at connect time, so a name that answers a public IP at check time
    and a private one at connect time slips past. Returning the exact validated
    address lets the caller pin the socket to it, so the address that was checked
    is the address that is dialed (Saltzer and Schroeder: no gap between check and
    use).

    A literal IP is validated and returned as-is. A hostname is resolved with
    ``getaddrinfo``; every returned address must pass, and the first is pinned. A
    resolution failure raises, rather than falling through to an unpinned connect
    that would re-resolve unchecked."""
    if is_private_host(host):
        raise PolicyError(
            f"{what} host {host!r} is a private or loopback address (blocked by policy)"
        )
    ip = _as_ip(host)
    if ip is not None:
        # A literal IP: already validated as public above; connect to it directly.
        return host
    try:
        addrs = _resolved_addrs(host)
    except OSError as exc:
        raise PolicyError(f"{what} host {host!r} could not be resolved") from exc
    except PolicyError as exc:
        # Re-raise with the caller's context, so every refusal from this function
        # names what was being resolved, not just the host.
        raise PolicyError(f"{what} host {host!r} resolved to an unreadable address") from exc
    if not addrs:
        raise PolicyError(f"{what} host {host!r} resolved to no address")
    # Every resolved address must be public: a name that answers both a public and
    # a private A-record must not be reachable by racing to the private one.
    for addr in addrs:
        if is_private_host(addr):
            raise PolicyError(
                f"{what} host {host!r} resolves to a private or loopback address "
                f"{addr!r} (blocked by policy)"
            )
    return addrs[0]


def check_url(
    url: str,
    *,
    allowed_schemes: Collection[str] = WEB_SCHEMES,
    block_private: bool = False,
    resolve: bool = True,
    what: str = "URL",
) -> str:
    """Validate ``url`` and return it.

    Always enforces ``allowed_schemes``. When ``block_private`` is set, refuses a
    host that is, or resolves to, a private/loopback/link-local address.
    Resolution is on by default; skipping it would miss ``localhost`` and a cloud
    metadata name, so pass ``resolve=False`` only when the host is already a
    literal."""
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
    """Validate a read/write destination and return it as a :class:`Path`.

    Refuses acting through a symlink (a swap could redirect the read or write).
    When ``base`` is given, the destination must resolve inside it: a relative
    ``dest`` is taken under ``base``; an absolute or traversing ``dest`` that
    escapes is refused.

    With no ``base`` there is no configured root to bound the path to, so the
    fail-safe default (Saltzer and Schroeder) refuses the traversal vector: a
    relative ``dest`` that climbs out of the working directory with ``..`` is
    refused, rather than silently resolving to ``../../secret``. An absolute
    ``dest`` is still permitted here: with no root it is the operator's own local
    path (a trusted-machine ``file://`` ingest / download), not attacker-derived,
    and the callers that take untrusted, potentially-relative input (an upload
    ``file://``, a chained download destination) pass a root when they mean to
    confine, or run with one set. Configure a root (``THINGCTX_FS_ROOT`` /
    ``THINGCTX_DOWNLOAD_DIR``) to bound absolute paths too."""
    p = Path(dest)
    if p.is_symlink():
        raise PolicyError(f"refusing to act through a symlink: {str(dest)!r}")
    if base is None:
        # No root to confine to. Block a relative traversal out of the working
        # directory; leave an absolute path (a trusted local target) as given.
        if p.is_absolute():
            return p
        resolved = (Path.cwd() / p).resolve()
        try:
            resolved.relative_to(Path.cwd())
        except ValueError:
            raise PolicyError(
                f"destination {str(dest)!r} escapes the working directory; "
                "set THINGCTX_FS_ROOT / THINGCTX_DOWNLOAD_DIR to allow a path outside it"
            ) from None
        return resolved
    base_r = Path(base).resolve()
    target = (p if p.is_absolute() else base_r / p).resolve()
    try:
        target.relative_to(base_r)
    except ValueError:
        raise PolicyError(
            f"destination {str(dest)!r} escapes the allowed directory {str(base)!r}"
        ) from None
    return target
