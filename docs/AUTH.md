# Authentication

thingctx drives Things that name a security scheme in their TD. The TD only
*names* the scheme; the secret is supplied at runtime, keyed by Thing id, Thing
slug, or scheme name, and never lives in the document.

## A transport-neutral layer

Authentication is fully decoupled from transport. The seam between them is a
small set of **neutral credential types** -- nothing in the auth layer knows
about headers, topics, httpx or paho:

```
  secret + TD scheme
        |
        v
  Provider.resolve(ctx) -> Credential        # auth layer (transport-agnostic)
        |
        |  BearerToken | BasicCredential | ApiKeyCredential
        |  SignatureCredential | ClientCertificate | EnhancedAuth | RequestSigner
        v
  apply_http / apply_mqtt / ...               # one applier per transport
        |
        v
  the invoker just runs the plan              # no auth logic in the transport
```

- **Providers** resolve a scheme + secret into `Credential` material. Token
  minting (OAuth2, JWT-bearer) lives here and may call its IdP over HTTPS -- that
  is the auth layer talking to an identity provider, not the transport leaking in.
- **Appliers** map that material onto one protocol. `apply_http` produces headers,
  query params, a client `cert`, and request signers; `apply_mqtt` produces a
  username/password, a TLS cert, and (v5) enhanced-auth data. Kinds a transport
  cannot express are ignored (HTTP ignores `EnhancedAuth`; MQTT ignores AWS
  signing).
- **`resolve_credentials`** is the single primitive every invoker shares to walk
  a Thing's active schemes and produce `Credential`s. `HttpInvoker` and
  `MqttInvoker` use the identical call; a future OPC-UA / RTSP / GigE invoker
  adds only an applier, never new auth logic.

The payoff: one credential drives every transport. A `basic` Thing becomes an
HTTP `Authorization` header *or* an MQTT username/password from the same
declaration; a `ClientCertificate` is reused verbatim for HTTPS client auth and
MQTT `tls_set`.

## Built-in providers and the material they return

| Scheme (TD `scheme`) | Provider | Credential you supply | Resolves to |
|---|---|---|---|
| `bearer` | StaticBearerAuth | `"tok"` or `{"access_token": "tok"}` | `BearerToken` |
| `basic` | BasicAuth | `"user:pass"`, `(user, pass)`, or `{"username","password"}` | `BasicCredential` |
| `apikey` | ApiKeyAuth | the key string | `ApiKeyCredential` (header or query per the TD's `in`/`name`) |
| `oauth2` (+ client secret) | OAuth2ClientCredentialsAuth | `{"client_id","client_secret"}` | `BearerToken` (Basic-first, body-post fallback; caches) |
| `oauth2` (+ private key) | OAuth2JwtBearerAuth | a service-account dict (`client_email`,`private_key`,`token_uri`,`scopes`) | `BearerToken` (RFC 7523 RS256; needs `thingctx[cloud]`) |
| `aws-sigv4` / `auto`+hint | AwsSigV4Auth | `{"aws_access_key_id","aws_secret_access_key"[, "aws_session_token"]}` | `SignatureCredential(algorithm="aws-sigv4")` (HTTP applier signs the request) |
| any | DirectCredentialAuth | a ready-made `Credential` (e.g. `ClientCertificate(...)`) | that credential, verbatim |

`DirectCredentialAuth` is the seam for transport-level material no TD scheme
names -- notably mutual TLS: pass a `ClientCertificate` as the secret and it is
used for whatever scheme the Thing declares, on whatever transport.

## How each transport applies a credential

| Credential | HTTP (`apply_http`) | MQTT (`apply_mqtt`) |
|---|---|---|
| `BearerToken` | `Authorization: Bearer <tok>` | password (token-as-password) |
| `BasicCredential` | `Authorization: Basic ...` | username + password |
| `ApiKeyCredential` | header or query param | password |
| `ClientCertificate` | client-level `cert=` (mTLS) | `tls_set(...)` |
| `SignatureCredential` | sign the assembled request (signer chosen by `algorithm`) | (ignored) |
| `EnhancedAuth` | (ignored) | MQTT v5 enhanced auth -- Tier 2 |
| `RequestSigner` | run on the assembled request | (ignored) |

## Driving the big clouds

"Any service" across the three big clouds comes down to which provider applies:

- **Azure** -- Entra ID (Azure AD) service principals use OAuth2
  `client_credentials` with a `.default` scope; Azure AI uses `apikey`. Both are
  built in:

  ```json
  {"scheme": "oauth2", "flow": "client_credentials",
   "token": "https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token",
   "scopes": ["https://management.azure.com/.default"]}
  ```

- **Google Cloud** -- service accounts sign a JWT with their private key and
  exchange it for a token (JWT-bearer). Pass the `service_account.json` contents
  as the credential; the default token endpoint is `oauth2.googleapis.com/token`.

- **AWS** -- every request is signed with SigV4 (no bearer token). SigV4 is not a
  standard W3C scheme, so declare it conformantly as `auto` (a valid scheme) plus
  a namespaced hint; supply the access key/secret as the credential:

  ```json
  {"scheme": "auto", "x-thingctx-auth": "aws-sigv4",
   "region": "us-east-1", "service": "sts"}
  ```

  This passes strict W3C TD 1.1 validation (the schema permits extra members on a
  security scheme). thingctx's SigV4 provider matches on the `x-thingctx-auth`
  hint. A bare `{"scheme": "aws-sigv4", ...}` also works but will fail a strict
  validator, so prefer the `auto` form. The SigV4 core is also exposed as the pure
  function `thingctx.sigv4_sign(...)`.

## MQTT brokers

The same credentials drive Mosquitto, EMQX, and Azure IoT Operations over MQTT:

- **username/password** (`basic`) and **token-as-password** (`bearer`/`oauth2`):
  mapped onto the CONNECT by `apply_mqtt`. Proven live against a
  password-protected Mosquitto in `tests/test_mqtt_live.py`.
- **mutual TLS**: supply a `ClientCertificate`; `apply_mqtt` calls `tls_set`.
- **MQTT v5 enhanced auth** (Azure IoT Operations SAT, EMQX SCRAM): modeled as
  `EnhancedAuth(method, data)`. The invoker switches to an MQTT v5 client and
  sends `AuthenticationMethod` + `AuthenticationData` on the CONNECT. Supply the
  material directly (e.g. `credentials={id: EnhancedAuth("K8S-SAT", token)}`).
  Single-step token mechanisms (AIO SAT) work as-is; multi-step challenge/response
  (full SCRAM re-auth round-trips) is not yet driven end-to-end and needs a
  compatible broker to validate live.

## Extending it

Register your own provider to override a built-in or teach thingctx a scheme it
has never seen -- without forking the SDK. A provider implements `matches` and
`resolve`, returning a built-in `Credential` (works on every transport) or a
`RequestSigner` (for transport-specific signing):

```python
import thingctx
from thingctx import RequestSigner
from thingctx.auth import _BaseAuth

class HmacAuth(_BaseAuth):
    name = "hmac"
    def matches(self, scheme, credential):
        return (getattr(scheme, "raw", {}) or {}).get("x-thingctx-auth") == "hmac"
    async def resolve(self, ctx):
        secret = str(ctx.credential)
        def sign(request):
            request.headers["X-Signature"] = hmac_header(secret, request)
        return RequestSigner(sign=sign)

# Globally:
thingctx.register_auth(HmacAuth())
# ...or per-invoker (takes precedence over the built-ins):
thingctx.HttpInvoker(credentials={...}, extra_auth=[HmacAuth()])
```

Providers registered with `register_auth` / `extra_auth` are tried before the
built-ins, so they can override how an existing scheme is handled. A provider
that returns a built-in `Credential` (e.g. `BearerToken`) automatically works on
every transport. See [`examples/06_custom_auth.py`](../examples/06_custom_auth.py).

Request-signing schemes are neutral, not vendor-specific: a provider returns a
`SignatureCredential(algorithm=...)` carrying the key material (each secret field
wrapped in a `Secret`), and the HTTP applier picks the signer registered for that
`algorithm`. AWS SigV4 is the one built-in (`"aws-sigv4"`); add another cloud's
signing scheme with `register_signer("my-alg", factory)` — reusing the same
credential type and the same `Secret` handling, with no change to the providers
or other transports.

To keep a TD with a custom scheme **W3C-valid**, declare `"scheme": "auto"` plus
a namespaced hint (e.g. `"x-thingctx-auth": "my-scheme"`) and match on
`scheme.raw.get("x-thingctx-auth")`, rather than inventing a new value for
`scheme` (which a strict validator rejects).

## Handling secrets

Every secret a credential holds lives in a `Secret` (`thingctx.auth.secret`),
a zero-dependency container that hardens two distinct axes.

**Axis 1 — accidental exposure** (where almost all real leaks happen):

- It never prints its value: `repr`, `str`, f-strings, `format`, tracebacks and
  log lines all show `Secret(***)`. Credentials inherit a redacting repr too, so
  `BearerToken(...)` shows `token=***`.
- Reading requires an explicit `get_secret_value()` / `get_secret_bytes()`, so a
  secret can never be unwrapped by accident ("syntactic salt").
- Pickling and copying are blocked, so a secret can't be serialized into a queue,
  cache, or log, nor silently duplicated.
- Equality is constant-time (`hmac.compare_digest`); `Secret` is unhashable.

**Axis 2 — memory lifetime** (pushed as far as CPython allows):

- The value is stored in a `bytearray` (mutable, so it *can* be overwritten,
  unlike `str`/`bytes`). `wipe()` zeroes it; the credential's `wipe()` zeroes all
  of its secret fields at once. It is also wiped on garbage-collection and on
  context-manager exit.
- Optional, best-effort memory locking keeps the page out of swap. Off by
  default; enable per value with `Secret(value, lock=True)` or globally with
  `THINGCTX_MLOCK_SECRETS=1`. It is pure stdlib (`ctypes` → `mlock`) and a silent
  no-op where the OS forbids it — a hardening bonus, never a requirement.

```python
from thingctx import Secret

# Hold a secret only for the window you need it, then zero it:
with Secret(raw_token) as tok:
    call_api(tok.get_secret_value())
# tok is wiped here

cred.wipe()  # zero every secret field of a resolved credential
```

### The ceiling

`Secret` shrinks the window and blast radius; it does **not** make leakage
impossible, because CPython itself caps this:

- Secrets usually *arrive* as `str` (env vars, config, JSON) and have already
  left immutable, unwipeable copies on the heap before any wrapper sees them.
- `get_secret_value()` and every downstream consumer (an HTTP header, a TLS
  handshake, `jwt.encode`) create more immutable copies you can't track or wipe.
- The GC does not zero freed memory, and `os.fork()` copies the whole heap.
- Deliberate introspection (`vars`, `dataclasses.asdict`, a debugger,
  `/proc/<pid>/mem`) can still reach a live value.

So treat in-process hardening as defense-in-depth. The strongest controls are
**architectural**, and thingctx is built for them: it never persists secrets,
resolves them at call time, and mints short-TTL tokens from a root credential on
demand. Push further by sourcing the root secret from a manager / OS keychain /
workload identity (IMDS, SPIFFE) / HSM or TPM that signs without revealing the
key, and by preferring short-lived tokens over long-lived secrets — so the
material that ever reaches the Python process is both minimal and short-lived.
