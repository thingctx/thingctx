# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The ``thingctx`` command line.

    thingctx import openapi <spec> [--out td.json] [--base-url URL] [--id ID]
    thingctx lint <td>

``<spec>`` is a file path or http(s) URL (JSON or YAML). With ``--out`` the TD
is written there; otherwise it is printed to stdout. ``lint`` reads a TD and
reports whether an agent can use it; it exits 1 on any error-severity finding.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def _load_td(source: str) -> dict:
    """Read one TD from a file path or an http(s) URL."""
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source) as resp:  # noqa: S310 (scheme checked above)
            return json.loads(resp.read().decode("utf-8"))
    with open(source, encoding="utf-8") as fh:
        return json.loads(fh.read())


def _cmd_lint(args: argparse.Namespace) -> int:
    from .lint import lint_td

    findings = lint_td(_load_td(args.td))
    for f in findings:
        print(f"{f.severity:6} {f.target}  [{f.rule}] {f.message}", file=sys.stderr)
    errors = sum(1 for f in findings if f.severity == "error")
    if not findings:
        print("ok: no lint findings", file=sys.stderr)
    return 1 if errors else 0


def _cmd_import_openapi(args: argparse.Namespace) -> int:
    from .openapi import from_openapi, load_spec

    spec = load_spec(args.spec)
    td = from_openapi(spec, base_url=args.base_url, id=args.id, title=args.title)
    out = json.dumps(td, indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"wrote {args.out} ({len(td.get('actions', {}))} actions)", file=sys.stderr)
    else:
        sys.stdout.write(out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="thingctx", description="WoT Thing Description tooling.")
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
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
