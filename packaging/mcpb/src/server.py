# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""Bundle entry point: run the thingctx MCP bridge over stdio."""

from thingctx.integrations.mcp import main

if __name__ == "__main__":
    main()
