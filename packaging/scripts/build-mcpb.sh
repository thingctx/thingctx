#!/usr/bin/env bash
# Build the Claude Desktop bundle (.mcpb) from packaging/mcpb/.
#
# The bundle is a launcher, not a vendored install: manifest.json plus a
# pyproject.toml and a tiny entry script. server.type is uv, so the host runs
# `uv run --directory <install dir> src/server.py` and uv resolves thingctx
# from the PyPI release at first launch. Keep the three files in sync:
#   manifest.json   the mcp_config template and user_config surface
#   pyproject.toml  the dependency pin the host installs
#   src/server.py   the stdio entry point
#
# Prereqs: node (npx fetches @anthropic-ai/mcpb on demand).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"      # packaging/
SRC="$HERE/mcpb"
OUT="$HERE/dist"
mkdir -p "$OUT"
echo "validating manifest..."
npx -y @anthropic-ai/mcpb validate "$SRC/manifest.json"
echo "packing..."
npx -y @anthropic-ai/mcpb pack "$SRC" "$OUT/thingctx.mcpb"
echo "built: $OUT/thingctx.mcpb"
echo "next: submit to Anthropic's directory for review (they gate the curated list)."
