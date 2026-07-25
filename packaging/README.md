# thingctx packaging: one bridge, every channel

Each artifact here delivers the SAME thingctx to a different install surface. All
resolve to the signed PyPI release (`thingctx` on PyPI), so trust flows from one anchor.

| Dir            | Channel                          | Build / publish                       |
| skill/         | Agent Skills (40+ apps, OpenCLAW)| copy the folder / submit to ClawHub   |
| registry/      | github.com/mcp + VS Code @mcp    | scripts/publish-registry.sh           |
| mcpb/          | Claude Desktop one-click         | scripts/build-mcpb.sh -> submit       |
| docker/        | gateway + cloud/remote (http)    | .github/workflows/release-image.yml   |
| k8s/           | org gateway on Kubernetes (example)| kubectl apply (references the image)|

Local (no packaging): `uvx --from thingctx[mcp] thingctx-mcp <tds>` in any MCP client;
`pip install thingctx` for the library and CLI.

Trust: PyPI ships with trusted-publishing + attestations; the Docker image is cosign
keyless-signed with an SBOM and provenance (see the release-image workflow).
