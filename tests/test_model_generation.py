# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The files in _generated match records.yaml and are what the LinkML commands write.

Needs the ``models`` extra (LinkML). Skipped without it.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("linkml_runtime", reason="needs the models extra")

REPO = pathlib.Path(__file__).resolve().parent.parent
GENERATOR = REPO / "scripts" / "generate_model.py"


def _check() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_generated_terms_are_current() -> None:
    done = _check()
    assert done.returncode == 0, f"run scripts/generate_model.py\n{done.stderr}"


def test_the_check_fails_when_the_schema_moves_ahead(tmp_path: pathlib.Path) -> None:
    """A tripwire nobody has seen trip is a tripwire nobody can trust."""
    schema = REPO / "src" / "thingctx" / "models" / "sosa" / "records.yaml"
    original = schema.read_text()
    try:
        schema.write_text(original.replace("sosa:usedProcedure", "sosa:madeBySensor"))
        assert _check().returncode == 1, "the schema changed and the check passed"
    finally:
        schema.write_text(original)
    assert _check().returncode == 0, "the schema was not restored"


def test_the_two_ruff_pins_agree() -> None:
    """The generator runs ruff format on terms.py. If the models extra pinned a
    different ruff than dev, the drift check would fail with no schema change."""
    import tomllib

    with (REPO / "pyproject.toml").open("rb") as handle:
        extras = tomllib.load(handle)["project"]["optional-dependencies"]
    pins = {
        group: next(p for p in extras[group] if p.startswith("ruff")) for group in ("dev", "models")
    }
    assert pins["dev"] == pins["models"], pins


def test_the_output_is_what_the_linkml_commands_write() -> None:
    """records.py and records.schema.json equal the output of gen-pydantic and
    gen-json-schema run directly, byte for byte."""
    model = REPO / "src" / "thingctx" / "models" / "sosa"
    schema = str(model / "records.yaml")
    generated = model / "_generated"
    # Resolved next to this interpreter, because the venv is not always on PATH.
    tools = pathlib.Path(sys.executable).parent
    commands = {
        generated / "records.py": [
            str(tools / "gen-pydantic"),
            "--meta",
            "None",
            "--emptylist-for-multivalued-slots",
            "--template-dir",
            str(REPO / "scripts" / "model_templates"),
            schema,
        ],
        generated / "records.schema.json": [str(tools / "gen-json-schema"), schema],
    }
    for path, command in commands.items():
        done = subprocess.run(command, capture_output=True, text=True, check=False, cwd=REPO)
        assert done.returncode == 0, done.stderr
        tool = pathlib.Path(command[0]).name
        assert done.stdout == path.read_text(), f"{path.name} is not what {tool} writes"
