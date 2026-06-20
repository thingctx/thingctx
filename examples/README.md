# Examples

Run from the repo root with `PYTHONPATH=src`.

| | |
|--|--|
| [01_mcp_baseline.py](01_mcp_baseline.py) | The MCP model: author + run a server. |
| [02_thingctx_baseline.py](02_thingctx_baseline.py) | Same pump, no server. Read 01/02 back to back. |
| [03_thingctx_llm.py](03_thingctx_llm.py) | Add an LLM (local Ollama or an API key). |
| [04_trust.py](04_trust.py) | Approval gating + `verify()` grounding (no model). See [docs/TRUST.md](../docs/TRUST.md). |
| [05_oauth2.py](05_oauth2.py) | A full OAuth2 client-credentials flow, offline: local token server + protected API, driven from a TD. |
| [06_custom_auth.py](06_custom_auth.py) | Extensible auth: register a new security scheme, and use the built-in AWS SigV4 signer. Offline. |
| [registry/](registry/) | Standalone TDs. Point `thingctx-mcp` or `from_registry` here. |

01/02 need no model. The pump device is [_pump.py](_pump.py) (HTTP + SSE + MQTT).
