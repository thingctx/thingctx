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
ROOT="$(cd "$HERE/.." && pwd)"
mkdir -p "$OUT"

# The bundle asks for exactly the release it ships with, taken from
# pyproject.toml. A range would leave the resolver to choose, and a range like
# >=0.2,<0.3 silently excludes a prerelease, so an rc bundle cannot install the
# rc it was built for. Stamping the exact version removes the guesswork: what a
# user launches is the build this bundle was tested against.
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -n1)
if [ -z "$VERSION" ]; then
  echo "could not read the version from $ROOT/pyproject.toml" >&2
  exit 1
fi
EXTRAS="mcp,http,mqtt,media,filesystem,authz"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$SRC/." "$STAGE/"
python3 - "$STAGE" "$VERSION" "$EXTRAS" <<'PY'
import json, pathlib, re, sys
stage, version, extras = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]

pin = pathlib.Path(stage / "pyproject.toml")
text = pin.read_text()
new, count = re.subn(
    r'"thingctx\[[^\]]*\][^"]*"', f'"thingctx[{extras}]=={version}"', text
)
if count != 1:
    raise SystemExit(f"expected one thingctx pin in pyproject.toml, found {count}")
pin.write_text(new)

manifest = pathlib.Path(stage / "manifest.json")
data = json.loads(manifest.read_text())
data["version"] = version
manifest.write_text(json.dumps(data, indent=2) + "\n")
print(f"stamped bundle for thingctx=={version}")
PY
SRC="$STAGE"
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
