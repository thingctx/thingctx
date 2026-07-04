# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The shell half of the bridge: ``thingctx list`` / ``thingctx invoke``.

These cover the CLI's own logic , argv parsing, body assembly, the trust
approver, output serialization, and exit codes , with a fake client in place of
the real registry build (which the runtime/mcp suites already exercise)."""

from __future__ import annotations

import json
import sys

import pytest

from thingctx.cli import _build_args, _cli_approver, _coerce_arg, main


class FakeClient:
    def __init__(self, result=None, surface=None, media=None):
        self._result = result
        self._surface = surface or []
        self._media = media or []
        self.calls: list[tuple[str, dict]] = []
        self.approval = None

    def set_approval(self, approve, approve_when=None):
        self.approval = (approve, approve_when)

    async def call_tool(self, name, args=None):
        self.calls.append((name, args))
        return self._result(name, args) if callable(self._result) else self._result

    def tool_surface(self):
        return self._surface

    def list_media(self):
        return self._media

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@pytest.fixture
def cli(monkeypatch):
    """Patch the registry build so the CLI dispatches to a fake client; expose
    what the CLI passed through (registry args, approve_when, the client)."""
    seen: dict = {}

    def make(result=None, surface=None, media=None):
        fc = FakeClient(result, surface, media)
        seen["client"] = fc
        import thingctx.integrations.mcp as mcp
        import thingctx.registry as reg

        monkeypatch.setattr(reg, "from_args", lambda args: ("REG", tuple(args)))
        monkeypatch.setattr(reg, "default_registry", lambda: ("DEFAULT_REG",))
        monkeypatch.setattr(mcp, "_credentials_from_env", lambda: {})

        def cfr(registry, credentials=None, approve_when="declared", verbose=False):
            seen["registry"] = registry
            seen["credentials"] = credentials
            seen["approve_when"] = approve_when
            seen["verbose"] = verbose
            return fc

        monkeypatch.setattr(mcp, "client_from_registry", cfr)
        return fc

    seen["make"] = make
    return seen


# --- arg coercion + body assembly -----------------------------------------


def test_coerce_arg_json_types_and_strings():
    assert _coerce_arg("3") == 3
    assert _coerce_arg("3.5") == 3.5
    assert _coerce_arg("true") is True
    assert _coerce_arg("null") is None
    assert _coerce_arg('["a","b"]') == ["a", "b"]
    assert _coerce_arg('{"k":1}') == {"k": 1}
    # A plain string and a file path are not JSON, so they pass through as is.
    assert _coerce_arg("hello") == "hello"
    assert _coerce_arg("/tmp/clip.mp4") == "/tmp/clip.mp4"
    assert _coerce_arg("file:///tmp/clip.mp4") == "file:///tmp/clip.mp4"


def test_build_args_json_then_arg_overrides():
    body = _build_args(["title=clip", "count=2"], '{"title":"old","keep":true}')
    assert body == {"title": "clip", "keep": True, "count": 2}


def test_build_args_rejects_bad_json_and_bad_arg():
    with pytest.raises(SystemExit):
        _build_args(None, "{not json")
    with pytest.raises(SystemExit):
        _build_args(None, "[1,2]")  # not an object
    with pytest.raises(SystemExit):
        _build_args(["noequals"], None)


# --- approver -------------------------------------------------------------


class _Req:
    tool_name = "youtube.videosDelete"
    reason = "TD-declared"


def test_approver_yes_allows_without_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    assert _cli_approver(True)(_Req()) is True


def test_approver_non_tty_denies(monkeypatch):
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    assert _cli_approver(False)(_Req()) is False


def test_approver_tty_prompts(monkeypatch):
    monkeypatch.setattr(sys, "stdin", type("S", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert _cli_approver(False)(_Req()) is True
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert _cli_approver(False)(_Req()) is False


# --- invoke end to end (against the fake client) --------------------------


def test_invoke_prints_json_and_exits_zero(cli, capsys):
    cli["make"](result={"id": "v1", "ok": True})
    rc = main(["invoke", "./reg", "youtube.videosInsert", "--arg", "title=clip"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"id": "v1", "ok": True}
    # The CLI built one registry from the source and dispatched the slug name.
    assert cli["registry"] == ("REG", ("./reg",))
    assert cli["client"].calls == [("youtube.videosInsert", {"title": "clip"})]


def test_invoke_default_registry_when_no_source(cli):
    fc = cli["make"](result={"ok": True})
    rc = main(["invoke", "youtube.videosInsert", "--arg", "title=clip"])
    assert rc == 0
    assert cli["registry"] == ("DEFAULT_REG",)
    assert fc.calls == [("youtube.videosInsert", {"title": "clip"})]


def test_invoke_two_positionals_is_source_then_action(cli):
    cli["make"](result={})
    main(["invoke", "./reg", "x.y"])
    assert cli["registry"] == ("REG", ("./reg",))
    assert cli["client"].calls[0][0] == "x.y"


def test_invoke_three_positionals_errors():
    with pytest.raises(SystemExit):
        main(["invoke", "a", "b", "c"])


def test_invoke_quiet_by_default_and_verbose_flag(cli, monkeypatch):
    monkeypatch.delenv("THINGCTX_VERBOSE", raising=False)
    cli["make"](result={})
    main(["invoke", "x.y"])
    assert cli["verbose"] is False
    main(["invoke", "x.y", "--verbose"])
    assert cli["verbose"] is True


def test_invoke_verbose_from_env(cli, monkeypatch):
    monkeypatch.setenv("THINGCTX_VERBOSE", "1")
    cli["make"](result={})
    main(["invoke", "x.y"])
    assert cli["verbose"] is True


def test_list_quiet_by_default(cli, monkeypatch):
    monkeypatch.delenv("THINGCTX_VERBOSE", raising=False)
    cli["make"](surface=[])
    main(["list"])
    assert cli["verbose"] is False


def test_invoke_string_result_passes_through(cli, capsys):
    cli["make"](result="WEBVTT\n\nsub")
    rc = main(["invoke", "./reg", "captions.translate", "--arg", "source=http://x"])
    assert rc == 0
    assert capsys.readouterr().out == "WEBVTT\n\nsub\n"


def test_invoke_error_envelope_exits_one(cli, capsys):
    cli["make"](result={"error": "boom"})
    rc = main(["invoke", "./reg", "x.y"])
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == ""  # stdout stays clean for capture/pipes
    assert json.loads(cap.err) == {"error": "boom"}


def test_invoke_raised_exception_becomes_clean_error(cli, capsys):
    def boom(name, args):
        raise RuntimeError("transport down")

    cli["make"](result=boom)
    rc = main(["invoke", "./reg", "x.y"])
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    err = json.loads(cap.err)
    assert err["error"] == "transport down" and err["type"] == "RuntimeError"


def test_invoke_json_body_merged_with_arg(cli, capsys):
    cli["make"](result={})
    main(["invoke", "./reg", "x.y", "--json", '{"a":1,"b":2}', "--arg", "b=9"])
    assert cli["client"].calls[0][1] == {"a": 1, "b": 9}


def test_invoke_writes_out_file(cli, tmp_path, capsys):
    cli["make"](result={"id": "v1"})
    dest = tmp_path / "result.json"
    rc = main(["invoke", "./reg", "x.y", "--out", str(dest)])
    assert rc == 0
    assert json.loads(dest.read_text()) == {"id": "v1"}
    assert "wrote" in capsys.readouterr().err  # nothing on stdout, note on stderr


def test_invoke_approve_when_default_and_override(cli):
    fc = cli["make"](result={})
    main(["invoke", "./reg", "x.y"])
    assert cli["approve_when"] == "declared"
    assert fc.approval[1] == "declared"
    main(["invoke", "./reg", "x.y", "--approve-when", "all"])
    assert cli["approve_when"] == "all"


def test_invoke_yes_flag_wires_allowing_approver(cli):
    fc = cli["make"](result={})
    main(["invoke", "./reg", "x.y", "--yes"])
    approver = fc.approval[0]
    assert approver(_Req()) is True  # --yes approves unattended


# --- list -----------------------------------------------------------------


def test_list_prints_surface_and_media(cli, capsys):
    cli["make"](
        surface=[
            {
                "name": "youtube.videosInsert",
                "kind": "action",
                "description": "upload",
                "input_schema": {"type": "object"},
            }
        ],
        media=["cam.stream"],
    )
    rc = main(["list", "./reg"])
    assert rc == 0
    listing = json.loads(capsys.readouterr().out)
    names = {e["name"] for e in listing}
    assert names == {"youtube.videosInsert", "cam.stream"}
    media = next(e for e in listing if e["name"] == "cam.stream")
    assert media["kind"] == "media"


def test_list_default_registry_when_no_source(cli, capsys):
    cli["make"](surface=[{"name": "a.b", "kind": "action", "description": "", "input_schema": {}}])
    rc = main(["list"])
    assert rc == 0
    assert cli["registry"] == ("DEFAULT_REG",)
    assert [e["name"] for e in json.loads(capsys.readouterr().out)] == ["a.b"]
