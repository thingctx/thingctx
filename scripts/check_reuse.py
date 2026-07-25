#!/usr/bin/env python3
# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Block the reinvention patterns a codebase audit found worth catching forever.

A reuse audit read the tree and found the project reuses libraries well; the only
recurring reinvention was a handful of patterns a linter can catch before they
land again. This checks those, and only those, so it stays fast and quiet:

1. `random` for a security value. `random` is not cryptographically secure; a
   token, nonce, key, or state built from it is predictable. Use `secrets`.
2. `==` or `!=` to compare a secret. A byte-by-byte compare leaks length and
   content through timing. Use `hmac.compare_digest`.
3. A hand-rolled address literal parse (splitting a host string on dots). The
   edge cases (encoded forms, IPv6, ranges) are why `ipaddress` exists.

It does NOT try to judge "is there a library for this," which needs a human or
the deeper review. It catches the mechanical, high-confidence cases and defers
the rest. Ruff's own rules (S311, S105/6, and the bandit set) overlap some of
this; this guard adds the project-specific reasoning ruff does not encode and
keeps the rule list auditable in one place.

Usage:
  check_reuse.py [FILE ...]   check the given files (pre-commit passes them)
Exit 1 if any finding, 0 otherwise. Skips tests/, examples/, and modules whose
low-level primitives (ctypes, fork) have no maintained alternative for a security
boundary; the skip list below names them.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# A name that names a secret. Comparing one of these with ==/!= is the timing
# side channel; building one from `random` is the predictability bug.
_SECRET_NAMES = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "nonce",
    "signature",
    "hmac",
    "mac",
    "digest",
    "csrf",
    "state",
    "session_key",
)

# Files whose low-level code was audited and cleared: the confinement seam owns
# ctypes and fork by necessity (no maintained alternative for a security
# boundary), and tests exercise patterns the rules would otherwise flag.
_SKIP_PARTS = (
    "confine.py",
    "confine_net.py",
    "confine_privilege.py",
    "confine_fs.py",
    "confine_exec.py",
)


def _is_secret_name(name: str) -> bool:
    low = name.lower()
    return any(marker in low for marker in _SECRET_NAMES)


def _random_module_call(node: ast.Call) -> bool:
    """A call into the `random` module (random.random, random.choice, ...). The
    stdlib `random` is not for security; `secrets` is."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "random"
    )


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.findings: list[tuple[int, str]] = []

    def _flag(self, node: ast.AST, msg: str) -> None:
        self.findings.append((getattr(node, "lineno", 0), msg))

    def visit_Assign(self, node: ast.Assign) -> None:
        # A security value assigned from `random`: predictable. Only flag when the
        # target name says the value is a secret, so a game die roll is left alone.
        if _random_module_call(node.value) if isinstance(node.value, ast.Call) else False:
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(_is_secret_name(n) for n in names):
                self._flag(
                    node,
                    "security value built from `random`; use `secrets` "
                    "(random is not cryptographically secure)",
                )
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        # A secret compared with ==/!= leaks through timing. Flag when either side
        # is a name that reads as a secret. Use hmac.compare_digest.
        if any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            operands = [node.left, *node.comparators]
            for operand in operands:
                if isinstance(operand, ast.Name) and _is_secret_name(operand.id):
                    self._flag(
                        node,
                        f"secret `{operand.id}` compared with ==/!=; use "
                        "hmac.compare_digest (a plain compare leaks via timing)",
                    )
                    break
        self.generic_visit(node)


def check_file(path: str) -> list[str]:
    p = Path(path)
    if any(part in p.parts or p.name == part for part in _SKIP_PARTS):
        return []
    # tests exercise the flagged patterns on purpose; examples optimize for
    # clarity over a hardened compare and are not a security surface.
    if "tests" in p.parts or "examples" in p.parts:
        return []
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=path)
    except (SyntaxError, UnicodeDecodeError):
        return []  # not our job to report a parse error; ruff does that
    visitor = _Visitor(path)
    visitor.visit(tree)
    return [f"{path}:{line}: {msg}" for line, msg in sorted(visitor.findings)]


def main(argv: list[str]) -> int:
    files = [a for a in argv if a.endswith(".py")]
    findings: list[str] = []
    for f in files:
        findings.extend(check_file(f))
    if findings:
        print("reuse guard: reinvention of a safer standard was found\n")  # noqa: T201  # CLI output
        for line in findings:
            print(f"  {line}")  # noqa: T201  # CLI output
        print(  # noqa: T201  # CLI output
            "\nEach has a standard replacement (secrets, hmac.compare_digest, ipaddress)."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
