# thingctx packaging: one bridge, every channel

Each artifact here delivers the SAME thingctx to a different install surface. All
resolve to the signed PyPI release (`thingctx` on PyPI), so trust flows from one anchor.

| Dir            | Channel                          | Build / publish                       |
| skill/         | Agent Skills (40+ apps, OpenCLAW)| copy the folder / submit to ClawHub   |
| registry/      | github.com/mcp + VS Code @mcp    | scripts/publish-registry.sh           |
| mcpb/          | Claude Desktop one-click         | scripts/build-mcpb.sh -> submit       |
| docker/        | gateway + cloud/remote (http)    | .github/workflows/release-image.yml   |
| k8s/           | org gateway on Kubernetes (example)| kubectl apply (references the image)|

Local (no packaging): `uvx --from 'thingctx[mcp,http,media]' thingctx-mcp <tds>` in any
MCP client; `pip install thingctx` for the library and CLI. Add `mqtt` / `filesystem` when
those Things are in the served set.

## OAuth client (Gmail, YouTube, other Google Things)

User-authorized Things need your OAuth client on disk once. Drop the JSON Google
downloads (Desktop / installed app) at:

```
~/.config/thingctx/oauth-clients/oauth2.googleapis.com.json
```

The nested `{"installed":{...}}` shape is accepted as-is. One file covers every
Thing on that token host (Gmail and YouTube share it). Then ask the agent to
connect, or run `thingctx auth login --td <td> --client-secrets-file client.json`.
See `docs/USAGE.md` and `examples/oauth_connect/`.

## Filesystem sandbox

Serving `urn:thingctx:filesystem` binds the built-in handler. Set
`THINGCTX_FS_ROOT` to a directory you are willing to expose; with it unset every
filesystem call is refused. Optional: `THINGCTX_FS_MAX_BYTES` (default 10 MiB).

Trust: PyPI ships with trusted-publishing + attestations; the Docker image is cosign
keyless-signed with an SBOM and provenance (see the release-image workflow).
