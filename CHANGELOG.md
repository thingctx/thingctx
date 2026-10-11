# Changelog

## Unreleased

### Fixed

- The MQTT gateway's projected Thing Description carries each affordance as the source declares it (input, output, description, `synchronous`, a property's schema), not only the bus forms, so a consumer sees parameters and knows when to await a reply.
- The gateway waits for a long running action, up to 30 seconds, and replies with its state as JSON, instead of the text form of the status handle.
- The MQTT binding reads a property: the retained value when the form has `mqv:retain`, otherwise a request on the topic answered on `<topic>/reply`.

## 0.2.3 - 2026-08-30

### Fixed

- `pip install thingctx[mcp]` works again: the bridge is pinned below `mcp` 2.0, which redesigned the server API. Also fixes the Claude Desktop bundle.
- The CLI reports input errors without a traceback (#119, @lntutor).
- The default path no longer reaches the registry during packaging (#115).

### Added

- Redis transport binding: properties to keys, events to pub/sub, over `redis://` and `rediss://`, behind the `redis` extra (#130, @avishekrao).
- Per caller authorization on the MCP HTTP bridge: each call is authorized against the request's validated caller, not the bridge's own identity (#141, @KrzysiekSko).

### Changed

- Every GitHub Action is pinned by commit SHA (#127, @WAHIB-EL-KHADIRI).
- ruff moves to 0.16.0 (#125, @TalhaTahir24).
