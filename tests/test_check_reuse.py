# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The reuse guard catches the audited reinvention patterns and leaves safe code
alone. A linter that cries wolf gets disabled, so the false-positive cases matter
as much as the true positives."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_reuse.py"
_spec = importlib.util.spec_from_file_location("check_reuse", _SCRIPT)
assert _spec and _spec.loader
check_reuse = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_reuse)


def _write(tmp_path: Path, code: str, name: str = "mod.py") -> str:
    p = tmp_path / name
    p.write_text(code)
    return str(p)


def test_flags_random_for_a_secret(tmp_path: Path) -> None:
    f = _write(tmp_path, "import random\nsession_token = random.random()\n")
    findings = check_reuse.check_file(f)
    assert findings and "secrets" in findings[0]


def test_ignores_random_for_a_non_secret(tmp_path: Path) -> None:
    # A game roll is not a security value; leave it alone.
    f = _write(tmp_path, "import random\ndie_roll = random.randint(1, 6)\n")
    assert check_reuse.check_file(f) == []


def test_flags_secret_compared_with_equality(tmp_path: Path) -> None:
    f = _write(tmp_path, "def ok(token, expected):\n    return token == expected\n")
    findings = check_reuse.check_file(f)
    assert findings and "compare_digest" in findings[0]


def test_flags_secret_not_equal(tmp_path: Path) -> None:
    f = _write(tmp_path, "def bad(api_key, want):\n    return api_key != want\n")
    findings = check_reuse.check_file(f)
    assert findings and "compare_digest" in findings[0]


def test_ignores_non_secret_equality(tmp_path: Path) -> None:
    f = _write(tmp_path, "def f(count, limit):\n    return count == limit\n")
    assert check_reuse.check_file(f) == []


def test_ignores_compare_digest_usage(tmp_path: Path) -> None:
    # The correct pattern must not be flagged.
    f = _write(
        tmp_path,
        "import hmac\ndef ok(token, want):\n    return hmac.compare_digest(token, want)\n",
    )
    assert check_reuse.check_file(f) == []


def test_skips_confine_modules(tmp_path: Path) -> None:
    # The confinement seam owns ctypes/fork by necessity; it is out of scope.
    f = _write(tmp_path, "import random\nkey = random.random()\n", name="confine.py")
    assert check_reuse.check_file(f) == []


def test_skips_tests(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    f = tests_dir / "t.py"
    f.write_text("def f(token, e):\n    return token == e\n")
    assert check_reuse.check_file(str(f)) == []


@pytest.mark.parametrize("code", ["def f(:\n", "x = "])
def test_parse_error_is_not_our_job(tmp_path: Path, code: str) -> None:
    # ruff reports syntax; the guard stays silent rather than double-reporting.
    f = _write(tmp_path, code)
    assert check_reuse.check_file(f) == []
