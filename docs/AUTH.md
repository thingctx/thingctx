# Authentication

A Thing's TD only *names* a security scheme; the secret is supplied at runtime
(keyed by Thing id, slug, or scheme name) and never lives in the document. Auth
is decoupled from transport, so one credential drives every protocol.

## How it works

Three steps, each replaceable:

1. **Provider** — `resolve(scheme, secret)` returns a neutral `Credential`
   (`BearerToken`, `BasicCredential`, `ApiKeyCredential`, `SignatureCredential`,
   `ClientCertificate`, …). Token minting (OAuth2, JWT-bearer) happens here.
2. **Applier** — `apply_http` / `apply_mqtt` map a `Credential` onto one
   transport (a header, query param, username/password, TLS cert, or request
   signer). Material a transport can't express is ignored.
3. **`resolve_credentials`** — the one primitive every invoker shares to turn a
   Thing's active schemes into `Credential`s. A new transport adds an applier,
   never new auth logic.

## Built-in schemes

| TD `scheme` | Supply as the secret | Resolves to |
|---|---|---|
| `bearer` | `"tok"` or `{"access_token": …}` | `BearerToken` |
| `basic` | `"user:pass"`, `(user, pass)`, or `{…}` | `BasicCredential` |
| `apikey` | the key string | `ApiKeyCredential` |
| `oauth2` (client secret) | `{"client_id", "client_secret"}` | `BearerToken` (cached) |
| `oauth2` (private key) | service-account dict | `BearerToken` (needs `thingctx[cloud]`) |
| `aws-sigv4` / `auto`+hint | `{"…access_key_id", "…secret_access_key"}` | `SignatureCredential` (request is signed) |
| any | a ready-made `Credential` | used verbatim (e.g. mTLS via `ClientCertificate`) |

## Using it

Pass secrets to the invoker, keyed by Thing id/slug or scheme name:

```python
thingctx.HttpInvoker(credentials={"weather": "my-token"})
```

## Extending it

Register a provider to override a built-in or add a new scheme — no fork. A
provider implements `matches` and `resolve`, returning a `Credential` (works on
every transport) or a `RequestSigner` (transport-specific signing):

```python
class HmacAuth(_BaseAuth):
    name = "hmac"
    def matches(self, scheme, credential):
        return (getattr(scheme, "raw", {}) or {}).get("x-thingctx-auth") == "hmac"
    async def resolve(self, ctx):
        return RequestSigner(sign=lambda r: r.headers.__setitem__("X-Sig", ...))

thingctx.register_auth(HmacAuth())                  # global
thingctx.HttpInvoker(..., extra_auth=[HmacAuth()])  # or per-invoker (wins)
```

Providers registered this way are tried before the built-ins. For a new signing
algorithm, return `SignatureCredential(algorithm="my-alg")` and register the
signer with `register_signer("my-alg", factory)`.

Keep custom-scheme TDs W3C-valid: declare `"scheme": "auto"` plus a namespaced
hint (`"x-thingctx-auth": "my-scheme"`) and match on
`scheme.raw["x-thingctx-auth"]` rather than inventing a new `scheme` value.
See [`examples/06_custom_auth.py`](../examples/06_custom_auth.py).

## Secrets

Every secret a credential holds is wrapped in `Secret`: it redacts in
`repr`/`str`/logs (`Secret(***)`), requires an explicit `get_secret_value()` to
read, blocks pickling/copying, and stores the value in a wipeable `bytearray`.

```python
with Secret(raw_token) as tok:
    call_api(tok.get_secret_value())   # tok is wiped on exit

cred.wipe()   # zero every secret field of a resolved credential
```

In-process hardening is defense-in-depth, not a guarantee (Python keeps
immutable copies of anything that arrived as a `str`). The durable controls are
architectural: thingctx never persists secrets, resolves them at call time, and
prefers short-lived tokens.
