#!/usr/bin/env bash
# Build the Claude Desktop bundle (.mcpb) from packaging/mcpb/manifest.json.
# server.type is uv, so nothing is vendored: the bundle is just the manifest and
# resolves thingctx from the signed PyPI release at run time. Tiny and tamper-light.
#
# Prereqs: npm i -g @anthropic-ai/mcpb   (the official CLI, formerly dxt)
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"      # packaging/
SRC="$HERE/mcpb"
OUT="$HERE/dist"
mkdir -p "$OUT"
echo "validating manifest..."
mcpb validate "$SRC/manifest.json"
echo "packing..."
mcpb pack "$SRC" "$OUT/thingctx.mcpb"
echo "built: $OUT/thingctx.mcpb"
echo "next: submit to Anthropic's directory for review (they gate the curated list)."
