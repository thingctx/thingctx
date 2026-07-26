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
# Pinned: an unpinned npx fetches whatever is latest at build time, so the same
# git tag could produce a different bundle, and an upstream break or compromise
# would land straight in a published artifact. Bump deliberately.
MCPB_VERSION="${MCPB_VERSION:-2.1.2}"
echo "validating manifest (mcpb $MCPB_VERSION)..."
npx -y "@anthropic-ai/mcpb@$MCPB_VERSION" validate "$SRC/manifest.json"
echo "packing..."
npx -y "@anthropic-ai/mcpb@$MCPB_VERSION" pack "$SRC" "$OUT/thingctx.mcpb"
echo "built: $OUT/thingctx.mcpb"
echo "next: submit to Anthropic's directory for review (they gate the curated list)."
