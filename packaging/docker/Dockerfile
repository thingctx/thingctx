# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
#
# Lean image for the thingctx gateway / MCP bridge. No media extra (av/numpy/pillow
# are heavy compiled libs; a separate -all tag can add them). Installs the SAME PyPI
# artifact everything else resolves to, so image trust inherits the signed/attested
# PyPI release.
#
# The base is pinned by digest so a build is reproducible and cannot pick up a
# silently republished tag. Dependabot bumps this (see .github/dependabot.yml), which
# is what keeps a pin from rotting into an unpatched base. One arg feeds both stages:
# naming it twice invites pinning one and leaving the runtime base out of step with
# what the venv was built against. The digest is the multi-arch index, so arm64 and
# amd64 both still resolve.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
FROM ${PYTHON_IMAGE} AS build
WORKDIR /app
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
# Install from PyPI (the signed/attested artifact), not the local tree, so the image
# is reproducible from a released version and carries nothing unpublished.
ARG THINGCTX_VERSION=0.2.0
RUN pip install --no-cache-dir "thingctx[mcp-http,http,mqtt,filesystem,validate,authz]==${THINGCTX_VERSION}"

FROM ${PYTHON_IMAGE}
# Run as a non-root user; the gateway needs no privileges.
RUN useradd --system --uid 10001 --home /home/thingctx thingctx
COPY --from=build /venv /venv
ENV PATH="/venv/bin:$PATH"
USER 10001
# Default: serve a fleet over the gateway. Override the command for the stdio bridge:
#   docker run ... thingctx-mcp <tds>
# Mount TDs at /things and secrets/credentials via env or a mounted file (NEVER baked
# into the image) so the image stays a stateless enforcer in front of the secret store.
ENV THINGCTX_APPROVE_WHEN=destructive
# The HTTP endpoint performs no inbound authentication. Front the published port with
# an authenticating reverse proxy or gateway guard; never expose it directly to an
# untrusted network. Default the operation policy to read-only so a bare deploy is
# least privilege; set THINGCTX_POLICY=full (behind auth) to allow writes.
ENV THINGCTX_POLICY=read-only
# The container serves streamable-http (the remote transport) so a hosted gateway or a
# cloud agent runtime (Azure Foundry, Vertex) can reach it by URL. Bind 0.0.0.0 inside
# the container. Override the CMD for a different mode.
ENTRYPOINT ["thingctx-mcp"]
CMD ["--http", "--host", "0.0.0.0", "--port", "8080", "/things/"]
EXPOSE 8080
