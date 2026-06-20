"""The ``thingctx`` command line.

    thingctx import openapi <spec> [--out td.json] [--base-url URL] [--id ID]

``<spec>`` is a file path or http(s) URL (JSON or YAML). With ``--out`` the TD
is written there; otherwise it is printed to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys


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
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
