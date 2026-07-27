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
  # No dry run against the registry: mcp-publisher 1.8.0 accepts --dry-run on
  # publish and sends the entry anyway. Validate locally instead, and let
  # --publish be the only path that talks to the registry.
  echo "validating thingctx==$VERSION locally (nothing is sent)..."
  python3 - "$STAMPED" <<'PY'
import json, sys, urllib.request

entry = json.load(open(sys.argv[1]))
print(f"  {entry['name']} {entry['version']}")

try:
    import jsonschema
except ImportError:
    # Presence checks alone would pass the length and pattern rules that reject
    # a real entry, so claiming the entry is valid here would be a lie.
    sys.exit("  cannot validate: pip install jsonschema")

try:
    with urllib.request.urlopen(entry["$schema"], timeout=30) as fh:
        schema = json.load(fh)
except OSError as exc:
    sys.exit(f"  cannot validate: schema unreachable ({exc})")

try:
    jsonschema.validate(entry, schema)
except jsonschema.ValidationError as exc:
    field = "/".join(str(p) for p in exc.absolute_path) or "(root)"
    sys.exit(f"  INVALID at {field}: {exc.message}")
print("  valid against the registry schema")
PY
  echo "--- validated only. re-run as '$0 --publish' to send it. ---"
fi
