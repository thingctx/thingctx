# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The ``thingctx`` command line.

    thingctx import openapi <spec> [--out td.json] [--base-url URL] [--id ID]
    thingctx lint <td>
    thingctx auth login [--td td.json] [--security NAME] [--client-secrets-file f]
    thingctx list [<source>]
    thingctx invoke [<source>] <action> [--arg K=V ...] [--json '{...}'] [--out FILE]
    thingctx registry add <source> [--link] | list | path
    thingctx skill install [--dest DIR] [--force] | show

``<spec>`` is a file path or http(s) URL (JSON or YAML). With ``--out`` the TD
is written there; otherwise it is printed to stdout. ``lint`` reads a TD and
reports whether an agent can use it; it exits 1 on any error-severity finding.
``auth login`` runs a one-time browser consent for a user-authorized
(authorization-code) scheme and stores the refresh token so later runs refresh
silently.

``list`` and ``invoke`` drive from the shell what the MCP bridge drives from a
client. ``<source>`` is the same TD registry the ``thingctx-mcp`` binary takes (a
dir of ``*.td.json``, a file, an http(s) URL, or a ``tdd:`` directory);
``<action>`` is a slug-qualified tool name (``youtube.videosInsert``). The call
runs through the same client the bridge uses (local handlers, bindings, stored
auth, trust gate), with the result as JSON on stdout. With no ``<source>`` they
read the per-user default registry (``thingctx registry``).

``skill`` ships an app-agnostic agent skill that teaches an agent to drive any
registered Thing through these commands; ``skill install`` copies it under
``~/.claude/skills/``.
"""

from __future__ import annotations
from thingctx import __version__

import argparse
import asyncio
import json
import os
import shutil
import sys
import urllib.request
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from thingctx.runtime import ThingClient
    from thingctx.trust import ApprovePolicy


def _load_td(source: str) -> dict:
    """Read one TD from a file path or an http(s) URL."""
    if source.startswith(("http://", "https://")):
        # A fixed timeout so a stalled server cannot hang the command indefinitely.
        with urllib.request.urlopen(source, timeout=30) as resp:  # noqa: S310 (scheme checked above)
            return cast(dict, json.loads(resp.read().decode("utf-8")))
    return cast(dict, json.loads(Path(source).read_text(encoding="utf-8")))


def _cmd_lint(args: argparse.Namespace) -> int:
    # loaded per subcommand so the CLI starts fast
    from .lint import lint_td  # noqa: PLC0415

    findings = lint_td(_load_td(args.td))
    for f in findings:
        print(f"{f.severity:6} {f.target}  [{f.rule}] {f.message}", file=sys.stderr)  # noqa: T201  # CLI output
    errors = sum(1 for f in findings if f.severity == "error")
    if not findings:
        print("ok: no lint findings", file=sys.stderr)  # noqa: T201  # CLI output
    return 1 if errors else 0


def _cmd_import_openapi(args: argparse.Namespace) -> int:
    # loaded per subcommand so the CLI starts fast
    from .openapi import from_openapi, load_spec  # noqa: PLC0415

    spec = load_spec(args.spec)
    td = from_openapi(spec, base_url=args.base_url, id=args.id, title=args.title)
    out = json.dumps(td, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"wrote {args.out} ({len(td.get('actions', {}))} actions)", file=sys.stderr)  # noqa: T201  # CLI output
    else:
        sys.stdout.write(out)
    return 0


def _client_from_secrets_file(path: str) -> dict[str, str]:
    """Read a Google-style client-secrets JSON (``installed``/``web`` wrapper)
    into client_id/secret and auth/token endpoints."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    blob = data.get("installed") or data.get("web") or data
    return {
        "client_id": blob.get("client_id", ""),
        "client_secret": blob.get("client_secret"),
        "authorization_url": blob.get("auth_uri", ""),
        "token_url": blob.get("token_uri", ""),
    }


def _scheme_from_td(path: str, name: str | None) -> tuple[dict[str, Any], str | None]:
    """Pull an authorization-code security scheme (endpoints + scopes) and the
    Thing id from a TD file. Picks the only code-flow scheme when ``name`` is
    omitted."""
    td = json.loads(Path(path).read_text(encoding="utf-8"))
    defs = td.get("securityDefinitions") or {}
    code = {
        n: d for n, d in defs.items() if d.get("scheme") == "oauth2" and d.get("flow") == "code"
    }
    if name:
        chosen = defs.get(name)
        if chosen is None:
            raise SystemExit(f"security scheme {name!r} not found in {path}")
    elif len(code) == 1:
        chosen = next(iter(code.values()))
    elif not code:
        raise SystemExit(f"no authorization-code (flow=code) scheme in {path}")
    else:
        raise SystemExit(
            f"multiple code schemes in {path}; pass --security NAME ({', '.join(code)})"
        )
    return {
        "authorization_url": chosen.get("authorization", ""),
        "token_url": chosen.get("token", ""),
        "scopes": list(chosen.get("scopes") or ()),
    }, td.get("id")


def _cmd_auth_login(args: argparse.Namespace) -> int:
    # loaded per subcommand so the CLI starts fast
    from .auth.oauth_consent import login  # noqa: PLC0415
    from .auth.store import FileTokenStore  # noqa: PLC0415

    cfg: dict[str, Any] = {"authorization_url": "", "token_url": "", "scopes": []}
    owner = args.owner
    if args.td:
        td_cfg, td_id = _scheme_from_td(args.td, args.security)
        cfg.update(td_cfg)
        owner = owner or td_id
    if args.client_secrets_file:
        cfg.update(
            {k: v for k, v in _client_from_secrets_file(args.client_secrets_file).items() if v}
        )
    # Explicit flags override everything.
    if args.authorization_url:
        cfg["authorization_url"] = args.authorization_url
    if args.token_url:
        cfg["token_url"] = args.token_url
    if args.scope:
        cfg["scopes"] = args.scope
    client_id = args.client_id or cfg.get("client_id")
    client_secret = args.client_secret or cfg.get("client_secret")

    missing = [k for k in ("authorization_url", "token_url") if not cfg.get(k)] + (
        [] if client_id else ["client-id"]
    )
    if missing or not client_id:
        raise SystemExit(f"missing required config: {', '.join(missing)}")
    client_id = str(client_id)

    store = FileTokenStore(args.store) if args.store else None
    login(
        authorization_url=cfg["authorization_url"],
        token_url=cfg["token_url"],
        client_id=client_id,
        client_secret=client_secret,
        scopes=tuple(cfg["scopes"]),
        owner_id=owner,
        store=store,
        offline=not args.no_offline,
        open_browser=not args.no_browser,
    )
    print(f"thingctx: stored refresh token for owner {owner!r}", file=sys.stderr)  # noqa: T201  # CLI output
    return 0


def _coerce_arg(value: str) -> Any:
    """A ``--arg`` value that parses as JSON becomes that type; anything else
    stays a string. A file path is not valid JSON, so it passes through unchanged;
    the media path and the bindings accept a path or ``file://`` URL as is."""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


def _build_args(arg_pairs: list[str] | None, json_blob: str | None) -> dict[str, Any]:
    """Assemble the action body: an optional ``--json`` object overlaid by each
    ``--arg KEY=VALUE`` (so a scalar flag overrides the same key in the blob)."""
    body: dict[str, Any] = {}
    if json_blob:
        try:
            parsed = json.loads(json_blob)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--json is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SystemExit("--json must be a JSON object")
        body.update(parsed)
    for pair in arg_pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--arg must be KEY=VALUE, got {pair!r}")
        key, raw = pair.split("=", 1)
        body[key] = _coerce_arg(raw)
    return body


def _cli_approver(assume_yes: bool) -> Callable[[Any], bool]:
    """Approve a trust-gated call: ``--yes`` allows it unattended; otherwise ask
    on a TTY. No TTY and no ``--yes`` denies (the safe default for cron/CI)."""

    def approve(req: Any) -> bool:
        if assume_yes:
            return True
        if not sys.stdin.isatty():
            return False
        prompt = f"Approve {req.tool_name}? reason: {req.reason} [y/N] "
        try:
            answer = input(prompt)
        except EOFError:
            return False
        return answer.strip().lower() in ("y", "yes")

    return approve


def _emit(result: Any, out: str | None) -> None:
    """Serialize an invoke result to ``--out`` (a path), or stdout when ``out``
    is missing or ``-``. Bytes go out raw, a str as is, anything else as JSON."""
    dest: Path | None = None
    if out and out != "-":
        # loaded per subcommand so the CLI starts fast
        from thingctx.netpolicy import confine_path  # noqa: PLC0415

        # Refuse to write through a symlink, and (when THINGCTX_DOWNLOAD_DIR is
        # set) keep the output inside it: --out often comes from an agent-driven
        # skill, not a human at a prompt.
        dest = confine_path(out, base=os.environ.get("THINGCTX_DOWNLOAD_DIR") or None)
    if isinstance(result, bytes | bytearray):
        data = bytes(result)
        if dest is not None:
            dest.write_bytes(data)
            print(f"wrote {out} ({len(data)} bytes)", file=sys.stderr)  # noqa: T201  # CLI output
        else:
            sys.stdout.buffer.write(data)
        return
    text = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
    if not text.endswith("\n"):
        text += "\n"
    if dest is not None:
        dest.write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)  # noqa: T201  # CLI output
    else:
        sys.stdout.write(text)


def _verbose(args: argparse.Namespace) -> bool:
    """Startup logs are off by default (clean pipes / agent transcripts); enable
    with ``--verbose`` or a truthy ``THINGCTX_VERBOSE``."""
    return bool(getattr(args, "verbose", False) or os.environ.get("THINGCTX_VERBOSE"))


def _registry_client(
    source: str | None, approve_when: ApprovePolicy, *, verbose: bool = False
) -> ThingClient:
    """Build the same client the MCP bridge builds (local handlers + http/media
    bindings, per-Thing env secrets, stored OAuth consent). With no ``source``,
    the per-user default registry (see thingctx.registry.default_sources)."""
    # loaded per subcommand so the CLI starts fast
    from .integrations.mcp import _credentials_from_env, client_from_registry  # noqa: PLC0415
    from .registry import default_registry, from_args  # noqa: PLC0415

    registry = from_args([source]) if source else default_registry()
    return client_from_registry(
        registry, credentials=_credentials_from_env(), approve_when=approve_when, verbose=verbose
    )


def _split_source_action(spec: list[str]) -> tuple[str | None, str]:
    """``[action]`` (default registry) or ``[source, action]`` (explicit
    source). One token is the action; two are source then action."""
    if len(spec) == 1:
        return None, spec[0]
    if len(spec) == 2:
        return spec[0], spec[1]
    raise SystemExit("usage: thingctx invoke [<source>] <action> [options]")


def _cmd_invoke(args: argparse.Namespace) -> int:
    source, action = _split_source_action(args.spec)
    body = _build_args(args.arg, args.json)
    # The --approve-when flag is argparse-validated, but THINGCTX_APPROVE_WHEN is
    # not; clamp an unrecognized env value to the safe default rather than letting
    # it silently degrade downstream.
    raw_when = args.approve_when or os.environ.get("THINGCTX_APPROVE_WHEN", "declared")
    # The membership test IS the validation, so the result is a valid ApprovePolicy;
    # mypy does not narrow `in` over a Literal, hence the cast.
    approve_when = cast(
        "ApprovePolicy",
        raw_when if raw_when in ("declared", "destructive", "all", "never") else "declared",
    )

    async def run() -> Any:
        client = _registry_client(source, approve_when, verbose=_verbose(args))
        client.set_approval(_cli_approver(args.yes), approve_when=approve_when)
        async with client:
            return await client.call_tool(action, body)

    try:
        result = asyncio.run(run())
    except Exception as exc:
        # A failure becomes a clean error, not a traceback dumped at a script.
        result = {"error": str(exc), "type": type(exc).__name__}
    # Failure (a raised error or an error envelope from the runtime) goes to
    # stderr with a non-zero exit, so stdout stays clean for ``$(...)`` capture
    # and pipes; only a successful result is written to stdout / ``--out``.
    if isinstance(result, dict) and "error" in result:
        print(json.dumps(result, default=str), file=sys.stderr)  # noqa: T201  # CLI output
        return 1
    _emit(result, args.out)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    async def run() -> list[dict[str, Any]]:
        client = _registry_client(args.source, "never", verbose=_verbose(args))
        async with client:
            entries = [
                {k: entry.get(k) for k in ("name", "kind", "description", "input_schema")}
                for entry in client.tool_surface()
            ]
            entries.extend(
                {
                    "name": name,
                    "kind": "media",
                    "description": f"media stream {name} (consume with frames())",
                    "input_schema": None,
                }
                for name in client.list_media()
            )
            return entries

    try:
        entries = asyncio.run(run())
    except Exception as exc:
        # A malformed source becomes a clean error, not a traceback.
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)  # noqa: T201  # CLI output
        return 1
    _emit(entries, args.out)
    return 0


def _cmd_registry_add(args: argparse.Namespace) -> int:
    # loaded per subcommand so the CLI starts fast
    from .registry import default_registry_dir  # noqa: PLC0415

    d = default_registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    src = args.source
    # A URL or a TD Directory cannot be stored as a file; record it as a source
    # line the default registry reads alongside its TD files.
    if src.startswith(("http://", "https://", "tdd:")):
        sources_file = d / "sources.txt"
        existing = (
            [ln.strip() for ln in sources_file.read_text(encoding="utf-8").splitlines()]
            if sources_file.is_file()
            else []
        )
        if src not in existing:
            with sources_file.open("a", encoding="utf-8") as fh:
                fh.write(src + "\n")
        print(f"thingctx: recorded source {src} in {sources_file}", file=sys.stderr)  # noqa: T201  # CLI output
        return 0
    p = Path(src)
    if not p.exists():
        raise SystemExit(f"no such path: {src}")
    files = sorted(set(p.glob("*.json"))) if p.is_dir() else [p]
    if not files:
        raise SystemExit(f"no *.json TDs in {src}")
    for f in files:
        dest = d / f.name
        if args.link:
            if dest.exists() or dest.is_symlink():
                dest.unlink()
            dest.symlink_to(f.resolve())
        else:
            shutil.copy2(f, dest)
    verb = "linked" if args.link else "copied"
    print(f"thingctx: {verb} {len(files)} TD(s) into {d}", file=sys.stderr)  # noqa: T201  # CLI output
    return 0


def _cmd_registry_list(args: argparse.Namespace) -> int:
    # loaded per subcommand so the CLI starts fast
    from .registry import default_registry_dir, default_sources  # noqa: PLC0415

    d = default_registry_dir()
    out = {
        "registry_dir": str(d),
        "sources": default_sources(),
        "files": sorted(f.name for f in d.glob("*.json")) if d.is_dir() else [],
    }
    _emit(out, args.out)
    return 0


def _cmd_registry_path(args: argparse.Namespace) -> int:
    # loaded per subcommand so the CLI starts fast
    from .registry import default_registry_dir  # noqa: PLC0415

    print(default_registry_dir())  # noqa: T201  # CLI output
    return 0


def _skill_text() -> str:
    """The driver skill text, shipped as package data."""

    return (files("thingctx") / "skill" / "SKILL.md").read_text(encoding="utf-8")


def _cmd_skill_show(args: argparse.Namespace) -> int:
    sys.stdout.write(_skill_text())
    return 0


def _cmd_skill_install(args: argparse.Namespace) -> int:
    dest_dir = Path(args.dest) if args.dest else Path.home() / ".claude" / "skills" / "thingctx"
    dest = dest_dir / "SKILL.md"
    if dest.exists() and not args.force:
        raise SystemExit(f"{dest} exists; pass --force to overwrite")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(_skill_text(), encoding="utf-8")
    print(f"thingctx: installed driver skill to {dest}", file=sys.stderr)  # noqa: T201  # CLI output
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="thingctx", description="WoT Thing Description tooling.")
    ap.add_argument(
    "--version",
    action="version",
    version=f"thingctx {__version__}",
)
    sub = ap.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="import a non-WoT description into a TD")
    imp_sub = imp.add_subparsers(dest="source", required=True)
    oa = imp_sub.add_parser("openapi", help="compile an OpenAPI 3.x spec into a TD")
    oa.add_argument("spec", help="OpenAPI spec: file path or http(s) URL (JSON or YAML)")
    oa.add_argument("--out", help="write the TD here (default: stdout)")
    oa.add_argument("--base-url", help="override the server URL from the spec")
    oa.add_argument("--id", help="TD id (default: urn:thingctx:<title-slug>)")
    oa.add_argument("--title", help="Thing title (default: info.title)")
    oa.set_defaults(func=_cmd_import_openapi)

    ln = sub.add_parser("lint", help="report whether an agent can use a TD")
    ln.add_argument("td", help="Thing Description: file path or http(s) URL")
    ln.set_defaults(func=_cmd_lint)

    auth = sub.add_parser("auth", help="manage stored credentials")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    lg = auth_sub.add_parser("login", help="one-time browser consent for a user-authorized API")
    lg.add_argument("--td", help="read endpoints + scopes + owner id from a TD file")
    lg.add_argument("--security", help="security scheme name in the TD (if more than one)")
    lg.add_argument(
        "--client-secrets-file", help="Google-style client secrets JSON (installed/web)"
    )
    lg.add_argument("--client-id", help="OAuth client id")
    lg.add_argument("--client-secret", help="OAuth client secret (public clients omit it)")
    lg.add_argument("--authorization-url", help="authorization endpoint (overrides TD)")
    lg.add_argument("--token-url", help="token endpoint (overrides TD)")
    lg.add_argument("--scope", action="append", help="requested scope (repeatable)")
    lg.add_argument("--owner", help="Thing id the token authorizes (default: TD id)")
    lg.add_argument("--store", help="token store file (default: ~/.config/thingctx/tokens.json)")
    lg.add_argument(
        "--no-browser", action="store_true", help="print the URL, do not open a browser"
    )
    lg.add_argument(
        "--no-offline", action="store_true", help="do not request offline access (no refresh token)"
    )
    lg.set_defaults(func=_cmd_auth_login)

    src_help = "TD source: a dir of *.td.json, a file, an http(s) URL, or tdd:URL"
    default_src_note = " (default: your configured registry, see `thingctx registry`)"

    ls = sub.add_parser("list", help="list the callable actions of a TD registry")
    ls.add_argument("source", nargs="?", help=src_help + default_src_note)
    ls.add_argument("--out", help="write the listing here ('-' or omitted: stdout)")
    ls.add_argument("--verbose", action="store_true", help="print startup logs to stderr")
    ls.set_defaults(func=_cmd_list)

    inv = sub.add_parser("invoke", help="invoke an action of a registered TD")
    inv.add_argument(
        "spec",
        nargs="+",
        metavar="[source] action",
        help="an action name (uses your configured registry)" + ", or a source then an action",
    )
    inv.add_argument(
        "--arg",
        action="append",
        metavar="KEY=VALUE",
        help="scalar argument (repeatable); a JSON-typed value is parsed, anything "
        "else (incl. a file path) stays a string",
    )
    inv.add_argument("--json", help="full argument body as a JSON object (--arg overrides keys)")
    inv.add_argument("--out", help="write the result here ('-' or omitted: stdout)")
    inv.add_argument(
        "--approve-when",
        choices=["declared", "destructive", "all", "never"],
        help="trust policy (default: $THINGCTX_APPROVE_WHEN or 'declared')",
    )
    inv.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="auto-approve trust-gated actions (non-interactive)",
    )
    inv.add_argument("--verbose", action="store_true", help="print startup logs to stderr")
    inv.set_defaults(func=_cmd_invoke)

    reg = sub.add_parser("registry", help="manage the per-user default TD registry")
    reg_sub = reg.add_subparsers(dest="registry_command", required=True)
    radd = reg_sub.add_parser("add", help="add a TD (dir/file copied in; URL/tdd recorded)")
    radd.add_argument("source", help=src_help)
    radd.add_argument(
        "--link", action="store_true", help="symlink a local TD instead of copying it"
    )
    radd.set_defaults(func=_cmd_registry_add)
    rls = reg_sub.add_parser("list", help="show the default registry's sources and TD files")
    rls.add_argument("--out", help="write the listing here ('-' or omitted: stdout)")
    rls.set_defaults(func=_cmd_registry_list)
    rpath = reg_sub.add_parser("path", help="print the default registry directory")
    rpath.set_defaults(func=_cmd_registry_path)

    skill = sub.add_parser("skill", help="the app-agnostic 'drive any Thing' agent skill")
    skill_sub = skill.add_subparsers(dest="skill_command", required=True)
    sinst = skill_sub.add_parser("install", help="copy the driver skill to ~/.claude/skills/")
    sinst.add_argument(
        "--dest", help="destination skill directory (default: ~/.claude/skills/thingctx)"
    )
    sinst.add_argument("--force", action="store_true", help="overwrite an existing SKILL.md")
    sinst.set_defaults(func=_cmd_skill_install)
    spath = skill_sub.add_parser("show", help="print the driver skill to stdout")
    spath.set_defaults(func=_cmd_skill_show)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return cast(int, args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
