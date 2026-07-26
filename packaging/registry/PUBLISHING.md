# Publishing the MCP registry entry

The entry lists thingctx in the MCP registry, which feeds github.com/mcp and the
VS Code `@mcp` gallery. It points at the PyPI release, so publish it only after
that release exists.

## Why a GitHub login is not enough

The entry is named `com.thingctx/thingctx`. That is a domain namespace, and the
registry proves those by DNS. A GitHub login grants only `io.github.<account>/*`,
so publishing with one fails:

    403 Forbidden
    You have permission to publish: io.github.galaxygrid/*
    Attempting to publish: com.thingctx/thingctx

Keeping the `com.thingctx` name means authenticating against the domain. The
alternative is renaming the entry to `io.github.<account>/thingctx`, which
publishes with a GitHub login but ties the listing to a personal namespace
rather than the project's own.

## The TXT record

It goes on the **apex**, `thingctx.com` itself. Not on a subdomain: the registry
treats `_mcp-auth` and `_mcp-registry` as commonly mistaken selectors and will
tell you the record is in the wrong place.

    name:  thingctx.com        (or "@")
    type:  TXT
    value: v=MCPv1; k=ed25519; p=<base64 public key>

A domain can hold several TXT records, so an existing SPF record stays.

Generate the keypair with `openssl genpkey -algorithm ed25519`. The private key
is a credential: it authorizes publishing under `com.thingctx/*`. Keep it out of
the repo, and use a KMS signing provider instead if it needs to live in CI.

## Publishing

    mcp-publisher login dns --domain thingctx.com --private-key <hex>
    bash packaging/scripts/publish-registry.sh              # dry run
    bash packaging/scripts/publish-registry.sh --publish

The script stamps the version from `pyproject.toml` into a temp copy, so the
listed version always matches the release. That copy is deleted when the script
exits, which is why the publish happens in the same run rather than as a
separate step afterwards.

Verify: <https://registry.modelcontextprotocol.io/v0/servers?search=thingctx>
