"""The default registry (source-free CLI) and the shipped driver skill.

Covers source resolution in thingctx.registry plus the `thingctx registry` and
`thingctx skill` CLI commands."""

from __future__ import annotations

import json
import os

import pytest

from thingctx.cli import main

# --- registry source resolution -------------------------------------------


def test_default_registry_dir_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from thingctx.registry import default_registry_dir

    assert default_registry_dir() == tmp_path / "thingctx" / "registry"


def test_default_sources_env_pathsep_overrides(monkeypatch):
    monkeypatch.setenv("THINGCTX_REGISTRY", f"/a/b{os.pathsep}/c/d")
    from thingctx.registry import default_sources

    assert default_sources() == ["/a/b", "/c/d"]


def test_default_sources_dir_and_sources_file(monkeypatch, tmp_path):
    monkeypatch.delenv("THINGCTX_REGISTRY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "thingctx" / "registry"
    d.mkdir(parents=True)
    (d / "x.td.json").write_text("{}")
    (d / "sources.txt").write_text("# a comment\nhttps://h/things\n\ntdd:https://x\n")
    from thingctx.registry import default_sources

    srcs = default_sources()
    assert str(d) in srcs
    assert "https://h/things" in srcs and "tdd:https://x" in srcs
    assert all(not s.startswith("#") for s in srcs)


def test_default_registry_empty_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.delenv("THINGCTX_REGISTRY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # nothing created
    from thingctx.registry import default_registry

    assert default_registry().fetch() == []


def test_default_registry_reads_td_files(monkeypatch, tmp_path):
    monkeypatch.delenv("THINGCTX_REGISTRY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "thingctx" / "registry"
    d.mkdir(parents=True)
    (d / "t.td.json").write_text(json.dumps({"id": "urn:dev:t", "title": "T"}))
    from thingctx.registry import default_registry

    assert [t["id"] for t in default_registry().fetch()] == ["urn:dev:t"]


# --- thingctx registry ... -------------------------------------------------


def test_registry_add_copies_local_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    src = tmp_path / "things"
    src.mkdir()
    (src / "a.td.json").write_text("{}")
    (src / "b.td.json").write_text("{}")
    assert main(["registry", "add", str(src)]) == 0
    reg = tmp_path / "cfg" / "thingctx" / "registry"
    assert (reg / "a.td.json").is_file() and (reg / "b.td.json").is_file()


def test_registry_add_links_local_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    f = tmp_path / "one.td.json"
    f.write_text("{}")
    assert main(["registry", "add", str(f), "--link"]) == 0
    dest = tmp_path / "cfg" / "thingctx" / "registry" / "one.td.json"
    assert dest.is_symlink()


def test_registry_add_records_url_idempotently(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    url = "https://h/.well-known/wot"
    assert main(["registry", "add", url]) == 0
    assert main(["registry", "add", url]) == 0  # second add is a no-op
    sf = tmp_path / "cfg" / "thingctx" / "registry" / "sources.txt"
    assert sf.read_text().count(url) == 1


def test_registry_add_missing_path_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    with pytest.raises(SystemExit):
        main(["registry", "add", str(tmp_path / "nope")])


def test_registry_list_reports_dir_and_files(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("THINGCTX_REGISTRY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "thingctx" / "registry"
    d.mkdir(parents=True)
    (d / "z.td.json").write_text("{}")
    assert main(["registry", "list"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["registry_dir"] == str(d)
    assert out["files"] == ["z.td.json"]
    assert str(d) in out["sources"]


def test_registry_path_prints_dir(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert main(["registry", "path"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "thingctx" / "registry")


# --- thingctx skill ... ----------------------------------------------------


def test_skill_show_prints_frontmatter(capsys):
    assert main(["skill", "show"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("---\nname: thingctx-driver")
    # app-agnostic: it must not name any specific internal app.
    # the skill must not hardcode any specific consumer app name
    assert "myapp" not in out.lower()


def test_skill_install_writes_and_guards_overwrite(tmp_path):
    dest = tmp_path / "skills"
    assert main(["skill", "install", "--dest", str(dest)]) == 0
    assert (dest / "SKILL.md").read_text().startswith("---\nname: thingctx-driver")
    with pytest.raises(SystemExit):  # exists, no --force
        main(["skill", "install", "--dest", str(dest)])
    assert main(["skill", "install", "--dest", str(dest), "--force"]) == 0


def test_skill_install_default_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["skill", "install"]) == 0
    assert (tmp_path / ".claude" / "skills" / "thingctx" / "SKILL.md").is_file()
