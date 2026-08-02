# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""CLI input failures are concise user errors, not Python tracebacks."""

from __future__ import annotations

import urllib.error
import urllib.request

import httpx
import pytest

from thingctx.cli import main


def _exit_message(argv: list[str]) -> str:
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    return str(exc_info.value)


def test_lint_missing_file_reports_read_error(tmp_path):
    source = tmp_path / "missing.td.json"

    message = _exit_message(["lint", str(source)])

    assert message.startswith(f"cannot read Thing Description {source}: ")
    assert "No such file" in message


def test_lint_malformed_file_reports_parse_error(tmp_path):
    source = tmp_path / "broken.td.json"
    source.write_text("{", encoding="utf-8")

    message = _exit_message(["lint", str(source)])

    assert message.startswith(f"cannot parse Thing Description {source}: ")


def test_lint_unreachable_url_reports_read_error(monkeypatch):
    source = "https://things.example/missing.td.json"

    def fail(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    message = _exit_message(["lint", source])

    assert message == f"cannot read Thing Description {source}: <urlopen error connection refused>"


def test_import_openapi_missing_file_reports_read_error(tmp_path):
    source = tmp_path / "missing.yaml"

    message = _exit_message(["import", "openapi", str(source)])

    assert message.startswith(f"cannot read OpenAPI spec {source}: ")
    assert "No such file" in message


def test_import_openapi_malformed_yaml_reports_parse_error(tmp_path):
    source = tmp_path / "broken.yaml"
    source.write_text("openapi: [", encoding="utf-8")

    message = _exit_message(["import", "openapi", str(source)])

    assert message.startswith(f"cannot parse OpenAPI spec {source}: ")


def test_import_openapi_unreachable_url_reports_read_error(monkeypatch):
    source = "https://apis.example/missing.yaml"

    def fail(*args, **kwargs):
        request = httpx.Request("GET", source)
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(httpx, "get", fail)

    message = _exit_message(["import", "openapi", source])

    assert message == f"cannot read OpenAPI spec {source}: connection refused"
