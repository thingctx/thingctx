#!/usr/bin/env bash
# Publish the MCP registry entry so thingctx appears in github.com/mcp and the
# VS Code @mcp gallery. The entry is stdio + the uvx command -> it installs the
# signed PyPI release. Publish AFTER the PyPI release exists and versions match.
#
# Prereqs: the mcp-publisher CLI, authenticated as an owner of the thingctx
# GitHub org (the registry checks org ownership of the com.thingctx namespace,
# so repo admin rights alone are not enough): mcp-publisher login github
#
# Dry run by default. Pass --publish to send it for real.
set -euo pipefail
PUBLISH=0
case "${1:-}" in
  --publish) PUBLISH=1 ;;
  "") ;;
  *) echo "usage: $0 [--publish]" >&2; exit 2 ;;
esac
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

# The publisher resolves a bare server.json against the working directory, so
# run it from the stamped copy's own directory rather than passing a path. The
# stamped copy is deleted when this script exits, so the publish has to happen
# in the same run that produced it.
if [ "$PUBLISH" -eq 1 ]; then
  echo "publishing thingctx==$VERSION to the registry..."
  (cd "$(dirname "$STAMPED")" && mcp-publisher publish server.json)
  echo "published. verify: https://registry.modelcontextprotocol.io/v0/servers?search=thingctx"
else
  echo "dry run for thingctx==$VERSION (nothing is sent)..."
  (cd "$(dirname "$STAMPED")" && mcp-publisher publish --dry-run server.json)
  echo "--- dry run only. re-run as '$0 --publish' to send it. ---"
fi
