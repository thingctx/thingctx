# Driving a system from a Thing Description

Point thingctx at a W3C Thing Description and drive the described system: project
its actions to agent tools, route each call over the form's transport, supply
credentials at run time, and gate risky calls behind an approver.

For authorization and identity, see [SECURITY.md](SECURITY.md). Runnable examples
live in [examples/](../examples/).

## Mapping a TD to tools

Each action projects to exactly one tool in OpenAI function format.

- **Name.** `<thing>.<action>`, where `<thing>` is the final significant segment
  of the Thing's `id` with a trailing version token (`v1`, `2`) removed. Action
  `createIssue` on `urn:svc:github` projects to `github.createIssue`.
- **Parameters.** The action's `input` JSON Schema, unmodified. No `input` means
  no arguments.
- **Description.** The action's `description`. An action's `output` schema is
  appended as `Returns: <schema>`.

This action on `urn:svc:github`:

```json
"createIssue": {
  "input": { "type": "object", "properties": {
    "owner": {"type": "string"}, "repo": {"type": "string"},
    "title": {"type": "string"}, "body": {"type": "string"} } },
  "forms": [{ "href": "https://api.github.com/repos/{owner}/{repo}/issues",
              "htv:methodName": "POST" }]
}
```

projects to:

```json
{ "type": "function", "function": {
  "name": "github.createIssue",
  "description": "createIssue",
  "parameters": { "type": "object", "properties": {
    "owner": {"type": "string"}, "repo": {"type": "string"},
    "title": {"type": "string"}, "body": {"type": "string"} } } } }
```

A property projects to read and write operations, not one tool. A read is `GET`;
a write is `PUT` by default. An event, and an observable property, projects to a
subscription over the form's streaming binding: Server-Sent Events for HTTP, the
named topic for MQTT.

## Resolving a call to a request

Each `{name}` template variable in the form's `href` is replaced by the argument
of the same name, which is then removed from the remaining arguments. For HTTP:

- The method is the form's `htv:methodName` where declared.
- With no method declared, an `idempotent` action is issued as `GET` with the
  remaining arguments bound as query parameters; any other action is `POST` with
  the remaining arguments bound as a JSON body.

So `github.createIssue` with `owner` `my-org` and `repo` `api` issues
`POST https://api.github.com/repos/my-org/api/issues` with the body
`{"title": ..., "body": ...}`.

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
reg.register(MyHttpBinding())     # replace the built-in http binding

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
thingctx.HttpBinding(..., extra_auth=[HmacAuth()])  # or per-binding (wins)
```

Keep a custom scheme TD valid under W3C: declare `"scheme": "auto"` plus a namespaced
hint (`"x-thingctx-auth": "my-scheme"`) and match on
`scheme.raw["x-thingctx-auth"]`. See
[13_custom_stack.py](../examples/13_custom_stack.py).

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

**Ground a TD against the live Thing.** `verify` reads each readable property and
checks it against the declared scalar type. It only reads, so it is safe against
production:

```python
for report in await client.verify():
    if not report:                  # VerifyReport.__bool__ == all checks passed
        print("drifted:", report.as_dict())
```

See [04_trust.py](../examples/04_trust.py).
