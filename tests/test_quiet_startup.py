"""Quiet-by-default startup: client_from_registry only logs bound local handlers
when asked, so a piped `thingctx list`/`invoke` is clean. Plus the driver skill's
legibility guidance ships."""

from __future__ import annotations

from types import SimpleNamespace

import thingctx.bindings as bindings
from thingctx.integrations.mcp import client_from_registry


def _registry():
    return SimpleNamespace(fetch=lambda: [{"id": "urn:dev:x", "title": "X"}])


def test_client_from_registry_silent_by_default(capsys, monkeypatch):
    monkeypatch.setattr(bindings, "discover_local_handlers", lambda slugs: {"x": object()})
    client_from_registry(_registry())
    err = capsys.readouterr().err
    assert "bound local handler" not in err


def test_client_from_registry_logs_when_verbose(capsys, monkeypatch):
    monkeypatch.setattr(bindings, "discover_local_handlers", lambda slugs: {"x": object()})
    client_from_registry(_registry(), verbose=True)
    err = capsys.readouterr().err
    assert "bound local handler(s) for x" in err


def test_no_handlers_is_silent_even_when_verbose(capsys, monkeypatch):
    monkeypatch.setattr(bindings, "discover_local_handlers", lambda slugs: {})
    client_from_registry(_registry(), verbose=True)
    assert "bound local handler" not in capsys.readouterr().err


def test_skill_documents_legible_commands():
    from thingctx.cli import _skill_text

    text = _skill_text()
    assert "Legible commands" in text
    # the PATH guidance: no package-runner / project-path prefix
    assert "on PATH" in text or "expected on PATH" in text
