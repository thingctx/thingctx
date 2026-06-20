"""``Secret``: a zero-dependency container for in-process secret material.

Masks its value in ``repr`` / ``str`` / tracebacks, requires an explicit
``get_secret_value()`` to read, blocks pickling and copying, holds the value in a
wipeable ``bytearray`` zeroed on :meth:`Secret.wipe`, on context-exit, and on
drop, and can optionally lock the buffer out of swap. In-process hardening only;
it cannot guarantee a value is gone from memory.
"""

from __future__ import annotations

import hmac
import os

__all__ = ["Secret"]

_REDACTED = "***"


def _as_bytes(value: str | bytes | bytearray) -> bytearray:
    if isinstance(value, str):
        return bytearray(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray)):
        return bytearray(value)
    raise TypeError(f"Secret accepts str or bytes, not {type(value).__name__}")


# Best-effort, opt-in memory locking; a no-op where unavailable or not permitted.


def _libc():
    import ctypes
    import ctypes.util

    name = ctypes.util.find_library("c")
    return ctypes.CDLL(name, use_errno=True) if name else None


def _try_lock(buf: bytearray):
    """Pin ``buf`` in RAM; return a handle to unlock later, or ``None``. Best
    effort: failures are swallowed."""
    if not buf:
        return None
    try:
        import ctypes

        lib = _libc()
        if lib is None or not hasattr(lib, "mlock"):
            return None
        carr = (ctypes.c_char * len(buf)).from_buffer(buf)
        if lib.mlock(carr, len(buf)) == 0:
            return carr
    except Exception:  # noqa: BLE001  -- best effort only
        return None
    return None


def _try_unlock(handle, length: int) -> None:
    if handle is None:
        return
    try:
        lib = _libc()
        if lib is not None and hasattr(lib, "munlock"):
            lib.munlock(handle, length)
    except Exception:  # noqa: BLE001
        pass


class Secret:
    """Hold a secret value with masked display, explicit unwrap, and wiping.

    Construct from ``str`` or ``bytes``. Read with :meth:`get_secret_value`
    (text) or :meth:`get_secret_bytes` (raw). :meth:`wipe` (or context-manager
    exit) zeroes the buffer; access after wiping raises. ``lock=True`` (or
    ``THINGCTX_MLOCK_SECRETS=1``) attempts to keep the value out of swap.
    """

    __slots__ = ("_buf", "_wiped", "_lock_handle")

    def __init__(self, value: str | bytes | bytearray, *, lock: bool | None = None) -> None:
        self._buf = _as_bytes(value)
        self._wiped = False
        if lock is None:
            lock = os.environ.get("THINGCTX_MLOCK_SECRETS", "") not in ("", "0", "false")
        self._lock_handle = _try_lock(self._buf) if lock else None

    # -- reading ---------------------------------------------------------- #

    def _raw(self) -> bytes:
        if self._wiped:
            raise RuntimeError("secret has been wiped")
        return bytes(self._buf)

    def get_secret_value(self) -> str:
        """The secret as text. Call only at the point of use."""
        return self._raw().decode("utf-8")

    def get_secret_bytes(self) -> bytes:
        """The secret as raw bytes."""
        return self._raw()

    # -- wiping ----------------------------------------------------------- #

    def wipe(self) -> None:
        """Overwrite the backing buffer with zeros and release any memory lock.
        Idempotent; the secret is unreadable afterwards."""
        if self._wiped:
            return
        try:
            for i in range(len(self._buf)):
                self._buf[i] = 0
        finally:
            _try_unlock(self._lock_handle, len(self._buf))
            self._lock_handle = None
            self._wiped = True

    def __enter__(self) -> Secret:
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.wipe()

    def __del__(self) -> None:
        try:
            self.wipe()
        except Exception:  # noqa: BLE001  -- never raise from a finalizer
            pass

    # -- constant-time comparison ----------------------------------------- #

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            try:
                ob = other._raw()
            except RuntimeError:
                return False
        elif isinstance(other, str):
            ob = other.encode("utf-8")
        elif isinstance(other, (bytes, bytearray)):
            ob = bytes(other)
        else:
            return NotImplemented
        try:
            return hmac.compare_digest(self._raw(), ob)
        except RuntimeError:
            return False

    __hash__ = None  # unhashable

    def __bool__(self) -> bool:
        return not self._wiped and len(self._buf) > 0

    # -- masked display --------------------------------------------------- #

    def __repr__(self) -> str:
        return f"Secret({_REDACTED})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return f"Secret({_REDACTED})"

    # -- block serialization / duplication -------------------------------- #

    def __reduce__(self):
        raise TypeError("Secret cannot be pickled")

    def __reduce_ex__(self, protocol):  # noqa: ANN001
        raise TypeError("Secret cannot be pickled")

    def __getstate__(self):
        raise TypeError("Secret cannot be serialized")

    def __copy__(self):
        raise TypeError("Secret cannot be copied")

    def __deepcopy__(self, memo):  # noqa: ANN001
        raise TypeError("Secret cannot be copied")
