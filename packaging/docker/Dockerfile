# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
#
# Lean image for the thingctx gateway / MCP bridge. No media extra (av/numpy/pillow
# are heavy compiled libs; a separate -all tag can add them). Installs the SAME PyPI
# artifact everything else resolves to, so image trust inherits the signed/attested
# PyPI release. Pin the base by DIGEST before shipping (placeholder below).
FROM python:3.12-slim AS build
# TODO before release: pin by digest, e.g.
# FROM python@sha256:<digest> AS build
WORKDIR /app
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
# Install from PyPI (the signed/attested artifact), not the local tree, so the image
# is reproducible from a released version and carries nothing unpublished.
ARG THINGCTX_VERSION=0.2.0
RUN pip install --no-cache-dir "thingctx[mcp-http,http,mqtt,validate,authz]==${THINGCTX_VERSION}"

FROM python:3.12-slim
# TODO: pin by digest to match the build stage.
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
# The container serves streamable-http (the remote transport) so a hosted gateway or a
# cloud agent runtime (Azure Foundry, Vertex) can reach it by URL. Bind 0.0.0.0 inside
# the container. Override the CMD for a different mode.
ENTRYPOINT ["thingctx-mcp"]
CMD ["--http", "--host", "0.0.0.0", "--port", "8080", "/things/"]
EXPOSE 8080
