# Connect a user-authorized service (OAuth)

`example.td.json` is a minimal Thing with an `oauth2` / `code` scheme, so the
connect flow has something to run. It uses `provider.example` placeholder
endpoints. Point it at a real provider by editing `authorization`, `token`, and
`scopes`, or point the bridge at a curated TD from
[td.thingctx.com](https://td.thingctx.com). The TD holds no secret.

## Run it

```
thingctx-mcp examples/oauth_connect/
```

The agent sees a `connect` tool alongside the Thing's tools. Ask it to do the
task; the bridge asks you to confirm, opens a browser once to approve on the
provider's page, and stores a refresh token. Later calls refresh on their own.
The agent never receives the token.

## The OAuth client (one time, yours)

Register an OAuth client with your provider (for desktop consent, a public client
with a loopback redirect), then drop its client-secrets JSON at:

```
~/.config/thingctx/oauth-clients/<token-host>.json
```

Keyed by the provider's token host, so one file serves every Thing on that
provider. For Google, drop the Desktop client JSON Google gives you at
`~/.config/thingctx/oauth-clients/oauth2.googleapis.com.json` (the nested
`installed` shape is fine unchanged). To sign in from the shell instead of the
bridge:

```
thingctx auth login --td example.td.json --client-secrets-file client.json
```

See [docs/USAGE.md](../../docs/USAGE.md) for the full flow and the security note.
