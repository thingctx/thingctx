#!/usr/bin/env bash
# Publish the MCP registry entry so thingctx appears in github.com/mcp and the
# VS Code @mcp gallery. The entry is stdio + the uvx command -> it installs the
# signed PyPI release. Publish AFTER the PyPI release exists and versions match.
#
# Prereqs: the mcp-publisher CLI, authenticated as the thingctx GitHub org.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ENTRY="$HERE/registry/server.json"

# The entry carries the version twice and neither is generated, so a hand edit
# that misses one publishes a listing that installs a different release than it
# claims. Stamp both from pyproject.toml, the same source the bundle uses, and
# publish from a temp copy so the tree keeps whatever is committed.
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -n1)
if [ -z "$VERSION" ]; then
  echo "could not read the version from $ROOT/pyproject.toml" >&2
  exit 1
fi
STAMPED="$(mktemp -d)/server.json"
trap 'rm -rf "$(dirname "$STAMPED")"' EXIT
python3 - "$ENTRY" "$STAMPED" "$VERSION" <<'PY'
import json, sys
src, dst, version = sys.argv[1], sys.argv[2], sys.argv[3]
entry = json.load(open(src))
entry["version"] = version
for pkg in entry.get("packages", []):
    pkg["version"] = version
json.dump(entry, open(dst, "w"), indent=2)
print(f"stamped registry entry for thingctx=={version}")
PY

echo "dry-run against the registry..."
mcp-publisher publish --dry-run "$STAMPED"
echo "--- review the dry-run above; re-run without --dry-run to publish ---"
# mcp-publisher publish "$STAMPED"
