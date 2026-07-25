# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""ExecBinding: drive a local subprocess as a transport.

An ``exec://`` form runs a program and returns its exit code and captured
output. The program and its arguments are an argv list carried on the form,
not a shell line: tokens are passed to the OS verbatim, with no shell, so an
argument value can never expand into extra arguments, globs, or shell
metacharacters.

    "restart": {
      "@type": "tc:Destructive",
      "input": {"type": "object", "properties": {"unit": {"type": "string"}}},
      "forms": [{
        "href": "exec://restart",
        "x-thingctx-exec": {"command": ["systemctl", "restart", "{unit}"]}
      }]
    }

``{var}`` placeholders in a token are filled from the action's arguments; the
filled token stays a single argv element. ``cwd`` and ``timeout`` are optional
per-form overrides.

This transport runs arbitrary local programs, so it is not a default binding:
a caller enables it explicitly. The command is read from the Thing
Description, so only enable it for Thing Descriptions you trust. By default a
binding with no ``allow`` list refuses to run anything; pass ``allow`` to name
the programs it may run, or ``allow_any=True`` to trust every command in the TD.
Mark exec actions ``tc:requiresApproval`` / ``tc:Destructive`` so the trust gate
prompts before they run.
"""

from __future__ import annotations

import os
import re
from typing import Any

from thingctx.bindings.base import ProtocolBinding
from thingctx.contracts import implements

_VAR = re.compile(r"\{([^}]+)\}")

# Interpreters that run code given inline, so allowlisting the interpreter name
# would otherwise let a TD run anything (``python -c ...``, ``env python ...``).
_INTERPRETERS = frozenset(
    {
        "python",
        "python2",
        "python3",
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "node",
        "nodejs",
        "deno",
        "bun",
        "ruby",
        "perl",
        "php",
        "osascript",
        "awk",
        "gawk",
        "env",
        "xargs",
        "nohup",
        "setsid",
    }
)
# Versioned interpreter binaries (python3.14, node20, ruby3.2, …) are the same
# risk class as the unversioned names above.
_VERSIONED_INTERPRETER = re.compile(r"^(?:python|ruby|perl|php|node|nodejs)\d+(?:\.\d+)*$")


def _is_interpreter(program: str) -> bool:
    base = os.path.basename(program).lower()
    return base in _INTERPRETERS or bool(_VERSIONED_INTERPRETER.match(base))


# A minimal environment for spawned programs: enough to find and run a binary,
# without leaking the parent's secrets (API keys, cloud creds) to it.
_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "SystemRoot")


@implements(ProtocolBinding)
class ExecBinding:
    """Run a local subprocess named by an ``exec://`` form.

    ``allow`` restricts which programs may run: a token is permitted if it
    matches an entry by full path or by basename. With no ``allow`` list the
    binding refuses every command unless ``allow_any=True`` is set, so enabling
    the transport is never enough on its own to run an arbitrary TD command.
    ``timeout`` is the default per-call limit in seconds; a form may lower it.
    ``inherit_env`` (default ``False``) passes a scrubbed minimal environment to
    the child; set it ``True`` to inherit the full parent environment, and
    ``env`` to add specific variables on top of whichever base is used."""

    scheme = "exec"

    def __init__(
        self,
        *,
        allow: list[str] | set[str] | None = None,
        allow_any: bool = False,
        timeout: float = 30.0,
        inherit_env: bool = False,
        env: dict[str, str] | None = None,
    ) -> None:
        self._allow = set(allow) if allow is not None else None
        self._allow_any = allow_any
        self._timeout = timeout
        self._inherit_env = inherit_env
        self._extra_env = dict(env) if env else {}

    def _permitted(self, argv: list[str]) -> str | None:
        """Return an error string if ``argv`` may not run, else ``None``."""
        program = argv[0]
        if self._allow is None:
            if not self._allow_any:
                return (
                    f"exec binding refuses {program!r}: no allow list configured "
                    "(pass allow=[...] to name permitted programs, or allow_any=True to "
                    "trust every command in the Thing Description)"
                )
            return None
        if not (program in self._allow or os.path.basename(program) in self._allow):
            return f"program not allowed: {program!r}"
        # The program is allowlisted; make sure it is not an interpreter being
        # handed inline code or a module, which would run past the allow list. A
        # legitimate wrapper takes data or file arguments, never flags, so for an
        # interpreter refuse ANY flag argument. A membership test on known flags
        # ("-c", "-m") misses a concatenated form ("-cCODE") and every future
        # flag; keying on the leading "-" catches them all.
        base = os.path.basename(program)
        if _is_interpreter(program) and (
            base.lower() == "env" or any(a.startswith("-") for a in argv[1:])
        ):
            return (
                f"program {program!r} is an interpreter invoked with a flag argument, "
                "which can bypass the allow list; allowlist a concrete wrapper program instead"
            )
        return None

    def _child_env(self) -> dict[str, str] | None:
        if self._inherit_env and not self._extra_env:
            return None  # inherit the parent environment unchanged
        base = (
            dict(os.environ)
            if self._inherit_env
            else {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
        )
        base.update(self._extra_env)
        return base

    async def invoke(self, action, form, arguments):  # noqa: ANN001
        import asyncio

        spec = (getattr(form, "raw", {}) or {}).get("x-thingctx-exec") or {}
        command = spec.get("command")
        if not isinstance(command, list) or not command:
            return {"error": "exec form requires x-thingctx-exec.command as a non-empty argv list"}

        missing: list[str] = []

        def _fill(token: str) -> str:
            def sub(m: re.Match) -> str:
                key = m.group(1)
                if key in arguments:
                    return str(arguments[key])
                missing.append(key)
                return m.group(0)

            return _VAR.sub(sub, token)

        argv = [_fill(str(tok)) for tok in command]
        if missing:
            names = ", ".join(sorted(set(missing)))
            return {"error": f"missing argument(s) for {action.name}: {names}"}

        program = argv[0]
        denied = self._permitted(argv)
        if denied is not None:
            return {"error": denied}

        timeout = float(spec.get("timeout") or self._timeout)
        cwd = spec.get("cwd")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._child_env(),
            )
        except FileNotFoundError:
            return {"error": f"program not found: {program!r}"}
        except OSError as e:  # e.g. permission denied, bad cwd
            return {"error": f"could not start {program!r}: {e}"}

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            proc.kill()
            await proc.wait()
            return {"error": f"{action.name} timed out after {timeout}s", "timeout": True}

        result: dict[str, Any] = {
            "exit_code": proc.returncode,
            "stdout": out.decode(errors="replace").rstrip("\n"),
            "stderr": err.decode(errors="replace").rstrip("\n"),
        }
        if proc.returncode != 0:
            # Surface a nonzero exit as an error so the caller is not told a
            # failed command succeeded.
            result["error"] = f"{program} exited {proc.returncode}"
        return result
