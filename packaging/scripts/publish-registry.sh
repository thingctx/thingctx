#!/usr/bin/env bash
# Publish the MCP registry entry so thingctx appears in github.com/mcp and the
# VS Code @mcp gallery. The entry is stdio + the uvx command -> it installs the
# signed PyPI release. Publish AFTER the PyPI release exists and versions match.
#
# Prereqs: the mcp-publisher CLI, authenticated as the thingctx GitHub org.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ENTRY="$HERE/registry/server.json"
echo "dry-run against the registry..."
mcp-publisher publish --dry-run "$ENTRY"
echo "--- review the dry-run above; re-run without --dry-run to publish ---"
# mcp-publisher publish "$ENTRY"
