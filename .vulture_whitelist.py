# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
#
# Vulture whitelist: names that ARE invoked, but only through indirection static
# analysis cannot follow. Every entry names the concrete dynamic caller. This is
# NOT a place to silence a finding; a flagged symbol with no named dynamic caller
# is dead code to DELETE (PRINCIPLES.md #2), never to whitelist.
#
# Run: vulture (config in pyproject [tool.vulture]).

# --- Entry-point factories -------------------------------------------------- #
# Each is advertised in pyproject [project.entry-points.*] and loaded by dotted
# string at discovery time: discover_auth()/discover_guards()/discover_gateway_
# bindings()/the local-handler loader call ep.load()(), so there is no in-tree
# caller for static analysis to see.
make_provider  # thingctx.auth EP -> loaded by discover_auth via ep.load()
make_entra_guard  # thingctx.guards EP -> loaded by discover_guards
make_cloudflare_guard  # thingctx.guards EP -> loaded by discover_guards
make_filesystem_handler  # thingctx.local_handlers EP -> loaded by the handler registry
make_time_handler  # thingctx.local_handlers EP -> loaded by the handler registry

# --- Console-script mains --------------------------------------------------- #
# pyproject [project.scripts] generates a wrapper that imports the module and
# calls main(); nothing in the tree calls it.
main  # thingctx (cli:main) and thingctx-mcp (integrations.mcp:main) console scripts
