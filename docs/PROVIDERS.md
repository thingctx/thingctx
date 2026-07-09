# The gateway guard is provider-neutral and pluggable

The inbound gateway guard validates an incoming bearer JWT and authorizes the
caller, so thingctx can sit north of devices that do not speak the identity
provider and drive them south with their own native auth. The validation is real
(RS256 signature against the provider's live JWKS, iss/aud/exp checks, a
non-leaking error), and it is identical for every provider. Only three things are
provider-specific. Everything else lives in the base.

## What is provider-neutral (the base)

`JwtGatewayGuard` (in `thingctx.identity/jwt_guard.py`) holds all the generic JWT
work, and no provider may weaken any of it:

- fetch the provider's signing keys (JWKS) and cache them for an hour;
- select the key by the token header `kid`; only fall back to a lone key when the
  JWKS has exactly one, so a forged `kid` against a multi-key set is a real
  failure;
- verify the RS256 signature against that key (never disabled);
- reject a forbidden algorithm (`alg: none`, HS downgrade) before any crypto
  runs;
- verify `iss` (against the accepted issuers), `aud`, `exp`/`nbf` (with leeway),
  and require `exp`/`iss`/`aud` to be present;
- an optional belt-and-braces check that a pinned substring (a tenant or team id)
  is actually in the decoded `iss`;
- enforce the configured authorization grants;
- the north-south bridge `authorize_and_invoke`: validate first, invoke the
  device only if authorized.

Authorization itself is generic too. A `Grant` names the claim that carries a
permission and the values required in it, with two knobs: `space_delimited` (a
string claim split on whitespace, the shape of Entra's `scp`) and `require_any`
(ANY vs ALL). A provider expresses its permission model as one or more `Grant`s.

## What each provider needs

A provider is a thin subclass that supplies exactly three things:

1. the accepted issuers (a tenant / team fixes these);
2. the JWKS source (a live URL to fetch, or a static set for offline / test);
3. the authorization grants (which claim carries the grant, and the values).

Adding a new IdP is one provider class plus one entry point, zero core change.
This mirrors how thingctx itself is extended: transport bindings register under
the `thingctx.bindings` entry-point group (`discover_bindings`), outbound
credential providers under `thingctx.auth` (`discover_auth`), and inbound guards
under `thingctx.guards` (`discover_guards`, in `thingctx.identity/registry.py`).

```python
# in a third-party thingctx-cognito package, pyproject.toml
[project.entry-points."thingctx.guards"]
cognito = "thingctx_cognito:make_cognito_guard"

# then, in a gateway
from thingctx.identity import discover_guards
guards = discover_guards(register=True)   # {"entra", "cloudflare", "cognito", ...}
Guard = guards["cognito"]
guard = Guard(user_pool_id=..., audience=...)
```

One difference from `discover_auth`: an outbound credential provider is
ready-to-use, so its entry point returns an instance. A guard needs
per-deployment config (a tenant, a team domain, an audience), so its entry point
returns the guard CLASS, keyed by the class's `provider` name; the gateway
constructs it with its own config.

## The two reference providers

### Entra (`thingctx.identity/providers/entra.py`, `provider = "entra"`)

- issuers: `https://login.microsoftonline.com/{tenant}/v2.0` (v2 access tokens)
  and `https://sts.windows.net/{tenant}/` (v1 access tokens, which real app-only
  tokens are);
- JWKS: `https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys`;
- grants: delegated scopes in `scp` (space-delimited string) and/or app roles in
  `roles` (a list). Configure with `required_scopes=` / `required_roles=`.

### Cloudflare Access (`thingctx.identity/providers/cloudflare.py`, `provider = "cloudflare"`)

- issuer: `https://{team_domain}.cloudflareaccess.com`;
- JWKS: `https://{team_domain}.cloudflareaccess.com/cdn-cgi/access/certs` (two
  keys, current + rotated, so key selection is by `kid`);
- audience: the Access application's AUD tag, which Cloudflare puts in the `aud`
  ARRAY; the base requires the configured tag to be a member;
- identity: a USER token carries `email`/`sub`; a SERVICE TOKEN (the agent
  equivalent) carries `common_name`.

## The honest comparison: Cloudflare service tokens vs Entra app-only

Cloudflare service tokens are SIMPLER than Entra's app-only path, on the setup
side. A service token is a client-id/secret pair Cloudflare validates at its
edge; the minted token carries a `common_name`. There is no roles-vs-scp saga (an
Entra app-only token carries NO `scp`, only `roles`, which is the single sharpest
Azure gotcha), no app-role definition, no `appRoleAssignment` on a service
principal, no admin-consent, no `.default` scope rule. If you can reach the Access
application, you are in.

But that simplicity has an honest cost in AUTHORIZATION GRANULARITY, and this is
where Cloudflare is LESS clean than Entra app roles, not more:

- Entra's app role IS in the token. `roles: ["Thing1.Write"]` names the exact
  permission the caller holds; the guard reads it straight from the JWT. The
  token is self-describing.
- Cloudflare Access does authorization at its EDGE via Access policies. If the
  policy lets the caller reach the app, a token is minted at all; the token does
  NOT carry an arbitrary per-action permission list. Cloudflare will not put a
  `Thing1.Write`-style grant in a service-token JWT by itself.

So the FLOOR of Cloudflare authorization is coarser than Entra: "holding a valid
token for this AUD means the Access policy let you in." A single Access
application is effectively one permission. To get a per-device / per-action grant
(the `Thing1.Write`-equivalent) into the decision, a real Cloudflare deployment
has two paths, and `CloudflareAccessGuard` supports both:

1. SERVICE-TOKEN MAPPING (`service_token_permissions=` + `required_permissions=`):
   the gateway is configured with a `{common_name: [permissions]}` map and
   requires named permissions. The guard DERIVES the caller's permissions from
   the token's `common_name` against that map, then checks them. The mapping
   lives in the GATEWAY config, not in the token, because Cloudflare will not
   embed it. This is the honest difference: Entra's grant is in the token;
   Cloudflare's is resolved by the gateway from the token's identity.

2. CUSTOM-CLAIM MAPPING (`permission_claim=` + `required_permissions=`): if the
   deployment configures an upstream IdP / Access policy to stamp a custom claim
   (e.g. `groups` or a bespoke `permissions` claim) into the token, the guard
   reads it directly, exactly like Entra's `roles`. This is the closest
   Cloudflare gets to Entra app roles, and it requires the upstream IdP to emit
   the claim; Access's own service tokens do not.

If neither is configured, the guard is authentication-only: a valid token for the
AUD passes, and the Access policy at the edge was the authorization. That is a
legitimate, common Cloudflare posture. It is just coarser than a per-action role,
and the guard refuses (raises `ValueError`) if you ask for a `required_permission`
without giving it a source, rather than silently authorizing everything.

Net: Cloudflare wins on setup (no G1/G7 roles-vs-scp saga), Entra wins on
in-token grant granularity. For a real per-device fleet on Cloudflare, prefer the
service-token mapping (one service token per agent, mapped to the devices/actions
it may drive) or a custom claim, and keep one Access application per trust
boundary rather than expecting the token alone to encode fine-grained rights.

## Cloudflare-specific gotcha

`aud` is an ARRAY, not a string. Entra's `aud` is a scalar; Cloudflare's is a
list of AUD tags (usually one hex string). PyJWT's audience check accepts either,
so the base needed no special case, but a hand-rolled verifier that assumes a
string `aud` will silently mishandle a Cloudflare token. The AUD tag also never
changes unless the Access application is deleted and recreated, so pin it as
config, not something you rotate.

## Cloudflare live-integration status (2026-07-08)

Verified LIVE against a real Cloudflare Zero Trust account (team sherif-page):
the CloudflareAccessGuard fetched Cloudflare's real signing keys from the live
JWKS endpoint, was configured with a real Access-app AUD tag, and REJECTED a
forged token (real Cloudflare kid, attacker key) by validating against the live
keys. So the guard genuinely integrates with live Cloudflare infrastructure.

NOT yet verified: validating a token Cloudflare ITSELF minted (the positive
edge-issued-JWT round trip), which requires a routed origin behind Cloudflare's
edge for a service token to authenticate through. The offline tests cover the
positive path with a correctly-signed token; the live test covers the real-JWKS
integration and the negative (forgery) path. Net: Cloudflare is
integration-live-verified, one step below Entra's full live round trip.
