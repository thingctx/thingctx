# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""LEAN invariants: the core stays free of the heavy optional dependencies.

`import thingctx` (and `import thingctx.authz`) must run on the standard library
alone, so a consumer who installs the base package and never touches a transport
pulls none of httpx / av / mcp / paho / cryptography / pydantic / litellm. This
is the giveaway's whole promise, so it carries a checked-in test, not only a
bare-env CI job.

The check runs in a subprocess: this test process has already imported the heavy
deps (other tests use them), so only a fresh interpreter can prove the core does
not pull them."""

from __future__ import annotations

import subprocess
import sys
import textwrap

_HEAVY = ["httpx", "av", "mcp", "paho", "cryptography", "pydantic", "litellm", "yaml", "numpy"]


def _modules_after(import_line: str) -> set[str]:
    """Import ``import_line`` in a fresh interpreter and return the top-level
    module names present in sys.modules afterward."""
    code = textwrap.dedent(f"""
        import sys
        {import_line}
        tops = sorted({{name.split(".")[0] for name in sys.modules}})
        print("\\n".join(tops))
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return set(out.stdout.split())


def test_import_thingctx_pulls_no_heavy_dependency():
    # invariant LEAN-1: a bare `import thingctx` loads none of the heavy optional
    # deps; the core runs on the standard library alone.
    loaded = _modules_after("import thingctx")
    leaked = [m for m in _HEAVY if m in loaded]
    assert not leaked, f"import thingctx pulled heavy deps: {leaked}"


def test_import_authz_pulls_no_heavy_dependency():
    # invariant LEAN-1: the authz kernel is heavy-dep-free too, so authorization
    # can be reasoned about and imported without a transport stack.
    loaded = _modules_after("import thingctx.authz")
    leaked = [m for m in _HEAVY if m in loaded]
    assert not leaked, f"import thingctx.authz pulled heavy deps: {leaked}"


def test_building_one_binding_does_not_import_anothers_dep():
    # invariant LEAN-2: building the http binding never pulls another binding's
    # heavy dep (av / paho / mcp). Each built-in imports its own dep locally, at or
    # after construction, so pulling one binding's extra never forces another's.
    loaded = _modules_after(
        "from thingctx.bindings.registry import build_builtin; build_builtin('http')"
    )
    for other in ("av", "paho", "mcp"):
        assert other not in loaded, f"building http pulled {other}"
