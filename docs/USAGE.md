# Driving a system from a Thing Description

Point thingctx at a W3C Thing Description and drive the described system: project
its actions to agent tools, route each call over the form's transport, supply
credentials at run time, and gate risky calls behind an approver.

For authorization and identity, see [SECURITY.md](SECURITY.md). Runnable examples
live in [examples/](../examples/).

## Mapping a TD to tools

Each action projects to exactly one tool in OpenAI function format: the tool name
is `<thing>__<action>` (a double underscore separator), the parameters are the
action's `input` schema, and a call resolves back to a form invocation over the
form's transport. So `createIssue` on `urn:svc:github` becomes the tool
`github__createIssue`, and calling it issues
`POST https://api.github.com/repos/{owner}/{repo}/issues`.

The full rules (tool naming, parameter and description projection, property and
event projection, call resolution to a request, security schemes, the constant
gateway projection) are the normative spec: [MAPPING.md](MAPPING.md).

## Bindings

A binding is a transport. `http`, `mqtt`, `media`, and `local` ship with thingctx. The
scheme of a form's `href` selects the binding: `https://` over HTTP, `mqtt://`
over MQTT, and an `href` with no scheme is handled locally. One Thing may combine
transports; binding is per form.

Add TDs to a live client at run time:

```python
ids = client.add_things(tds, validate=False)
```

Register a binding to add a protocol or replace one that ships:

```python
from thingctx import BindingRegistry, ThingClient

reg = BindingRegistry.default()   # http + local
reg.register(OpcUaBinding())      # add a new protocol
reg.register(MyHttpBinding())     # replace the bundled http binding

client = ThingClient(tds=[...], bindings=reg)
```

`register` makes a binding the one that serves its scheme(s). Enable the optional
transports through the same registry:

```python
BindingRegistry.default(mqtt=True, media=True)
```

Write a binding by naming a `scheme` and an async `invoke`:

```python
class OpcUaBinding:
    scheme = "opc.tcp"                      # or: schemes = ("opc.tcp", "opc.https")

    async def invoke(self, action, form, arguments):
        ...
```

Prove it with the conformance kit before shipping:

```python
from thingctx.testing import assert_binding_contract, binding_capabilities

assert_binding_contract(MyBinding())
print(binding_capabilities(MyBinding()))
```

A binding that needs credentials inherits `AuthMixin`, resolves the neutral
`Credential`, and maps it onto its wire. See
[13_custom_stack.py](../examples/13_custom_stack.py).

## Credentials

A TD only names a security scheme; supply the secret at run time, keyed by Thing
id, slug, or scheme name. Auth is decoupled from transport, so one credential
drives every protocol.

Pass secrets to the binding:

```python
thingctx.HttpBinding(credentials={"weather": "my-token"})
```

Schemes that ship:

| TD `scheme` | Supply as the secret | Resolves to |
|---|---|---|
| `bearer` | `"tok"` or `{"access_token": …}` | `BearerToken` |
| `basic` | `"user:pass"`, `(user, pass)`, or `{…}` | `BasicCredential` |
| `apikey` | the key string | `ApiKeyCredential` |
| `oauth2` (client secret) | `{"client_id", "client_secret"}` | `BearerToken` (cached) |
| `oauth2` (private key) | service account dict | `BearerToken` (needs `thingctx[cloud]`) |
| `aws-sigv4` / `auto`+hint | `{"…access_key_id", "…secret_access_key"}` | `SignatureCredential` (request is signed) |
| any | a prepared `Credential` | used verbatim (e.g. mTLS via `ClientCertificate`) |

A `form` may declare its own `security`, which overrides the Thing's default for
that interaction. With no security on the form, the Thing's default applies.

Add a scheme by registering a provider (`CredentialProvider`); inherit `BaseAuth`
for defaults that do nothing:

```python
from thingctx import BaseAuth, CredentialProvider, RequestSigner, implements

@implements(CredentialProvider)
class HmacAuth(BaseAuth):
    name = "hmac"
    def matches(self, scheme, credential):
        return (getattr(scheme, "raw", {}) or {}).get("x-thingctx-auth") == "hmac"
    async def resolve(self, ctx):
        return RequestSigner(sign=lambda r: r.headers.__setitem__("X-Sig", ...))

thingctx.register_auth(HmacAuth())                  # global
thingctx.HttpBinding(..., extra_auth=[HmacAuth()])  # or on one binding (wins)
```

Keep a custom scheme TD valid under W3C: declare `"scheme": "auto"` plus a namespaced
hint (`"x-thingctx-auth": "my-scheme"`) and match on
`scheme.raw["x-thingctx-auth"]`. See
[13_custom_stack.py](../examples/13_custom_stack.py).

## Connect a service that a user signs in to (OAuth)

For a Thing that acts for a person and needs them to sign in, the TD declares an
`oauth2` scheme with flow `code`, naming the provider's endpoints:

```json
"securityDefinitions": {
  "google": {
    "scheme": "oauth2", "flow": "code",
    "authorization": "https://accounts.google.com/o/oauth2/v2/auth",
    "token": "https://oauth2.googleapis.com/token",
    "scopes": ["https://www.googleapis.com/auth/calendar.readonly"]
  }
}
```

The TD holds no secret. You sign in once; thingctx stores a refresh token and
refreshes access tokens on its own after that.

Over the MCP bridge, sign in happens on demand: a `connect` tool lists what needs
it, and a call that lacks a token prompts you to connect first. Either way you
confirm, then approve once in a browser on the provider's page. Just ask the agent
to do the task and approve when prompted.

The OAuth client (the app's id and secret) is yours, supplied once at
`~/.config/thingctx/oauth-clients/<token-host>.json` (for Google,
`oauth2.googleapis.com.json`), never in a TD or the agent config. One file per
provider. Google's Desktop client download (`{"installed":{...}}`) drops in
unchanged. One Google file covers every Thing on that token host (Gmail and
YouTube share it). To sign in from the shell instead:

```
thingctx auth login --td service.td.json --client-secrets-file client.json
```

Tokens are keyed by Thing id, so connecting one Thing does not connect another.

### The agent never holds your token

The refresh token lives in a local store that only thingctx reads, a file only its
owner can open. The agent calls a described operation and never receives a token.
A compromised agent, or one under prompt injection, cannot take a credential it was
never given. With the authorization seam (see [SECURITY.md](SECURITY.md)) a misused
token is still bounded to the operations you granted.

This is narrower than "the secret is safe." The token store is a file like any
other credential file: an attacker who owns the host can read it, and redacting
logs is not a hardware boundary. The durable difference is that the agent is not
in the credential path. Unlike pasting an API key into an agent's config or an
MCP server's session, the token stays below the agent and is attached only when
thingctx reaches the real system.

## Trust: approval and grounding

Two primitives on `ThingClient`, both off until you opt in.

**Gate risky calls.** `ThingClient(approve=<callable>, approve_when=<policy>)`
picks when the approver is consulted:

| policy | gated calls |
|---|---|
| `declared` (default) | actions the TD marks risky |
| `destructive` | the above + any action that is not idempotent and every property write |
| `all` | every action and every property write |
| `never` | nothing (gating off) |

The approver is any callable (sync or async) receiving an `ApprovalRequest` and
returning truthy to allow. If a call is gated but no approver is configured, it
returns an error envelope instead of running. Gating is enforced inside `invoke`
and `write_property`, so it protects the LLM tool loop, direct callers, and the
MCP bridge alike.

On the CLI the same policy is `--approve-when declared|destructive|all|never`, or
`THINGCTX_APPROVE_WHEN` in the environment when you configure the bridge through
a host's JSON config. `--yes` approves unattended, which is what a script or a CI
job needs; without a terminal and without `--yes` a gated call is denied and exits
non-zero.

**Ground a TD against the live Thing.** `verify` reads each readable property and
checks it against the declared scalar type. It only reads, so it is safe against
production:

```python
for report in await client.verify():
    if not report:                  # VerifyReport.__bool__ == all checks passed
        print("drifted:", report.as_dict())
```

See [04_trust.py](../examples/04_trust.py).
