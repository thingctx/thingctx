# Examples

Run from the repo root with `PYTHONPATH=src`.

| | |
|--|--|
| [01_mcp_baseline.py](01_mcp_baseline.py) | The MCP model: author + run a server. |
| [02_thingctx_baseline.py](02_thingctx_baseline.py) | Same pump, no server. Read 01/02 back to back. |
| [03_thingctx_llm.py](03_thingctx_llm.py) | Add an LLM (local Ollama or an API key). |
| [04_trust.py](04_trust.py) | Approval gating + `verify()` grounding (no model). See [docs/TRUST.md](../docs/TRUST.md). |
| [05_oauth2.py](05_oauth2.py) | A full OAuth2 client-credentials flow, offline: local token server + protected API, driven from a TD. |
| [13_custom_stack.py](13_custom_stack.py) | **Extending thingctx.** All four parts you can extend, in one programming model: a custom transport (`ProtocolBinding` + `AuthMixin`), auth scheme (`CredentialProvider`/`BaseAuth`), discovery source (`Registry`), and media engine (`MediaBackend`), each `@implements`-checked and run through its conformance kit. Offline. See [docs/BINDINGS.md](../docs/BINDINGS.md). |
| [registry/](registry/) | Standalone TDs. Point `thingctx-mcp` or `from_registry` here. |

01/02 need no model. The pump device is [_pump.py](_pump.py) (HTTP + SSE + MQTT).

## Authorization (authn vs authz)

Two seams, kept apart on purpose. Authorization (decide what a caller may do)
lives in `thingctx.authz` and runs on the dependency-free core. Authentication
(prove who the caller is) lives in `thingctx.identity` behind the `authz` extra
because it needs crypto. See [docs/AUTHZ.md](../docs/AUTHZ.md).

| | |
|--|--|
| [14_authz.py](14_authz.py) | **Authorization on core, no extra.** A `pdp` and an `identity` passed to `ThingClient`; the same caller may READ a property and is DENIED the WRITE, decided against the TD-derived vocabulary before the device is touched. The identity is a claims dict written inline (what a guard's `validate()` returns). Offline. |
| [15_authn_to_authz.py](15_authn_to_authz.py) | **The full chain: real token to enforcement.** A self-signed issuer mints a real RS256 token; `JwtGatewayGuard` validates its signature against the JWKS (and rejects a forged one); the validated claims become the `identity` that core authz enforces. Needs the `authz` extra (`pip install 'thingctx[authz]'`). Offline. |

14 is the authz seam alone; 15 adds the authn guard in front of it so you see
where the identity actually comes from.

## Media (audio/video)

The continuous-binary plane: consume frames from a source, or publish them to a
target. Needs PyAV (`pip install 'thingctx[media]'`); a few are gated on
`mediamtx`/`ffmpeg` or an LLM key.

| | |
|--|--|
| [07_media_live.py](07_media_live.py) | Consume a stream resolved from a web page (yt-dlp); optional authenticated/private path. |
| [08_media_vlm.py](08_media_vlm.py) | Feed a sampled frame to a VLM. |
| [09_rtsp_local.py](09_rtsp_local.py) | Authenticated RTSP end to end, fully local (MediaMTX + auth layer). |
| [10_media_clip.py](10_media_clip.py) | Multi-frame sampling from a clip for a VLM. |
| [11_media_video.py](11_media_video.py) | Parameterized video-understanding service: pass a clip URL at call time. |
| [12_media_publish.py](12_media_publish.py) | The publish path; encode and push frames to a file or live RTSP (round trip verified). |

The media engine is also pluggable (`MediaBackend`); [13_custom_stack.py](13_custom_stack.py) shows a custom one alongside the other parts.
