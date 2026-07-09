# Threat model

Scope: the inbound identity guard and the authorization PEP/PDP in
thingctx-identity. Assets: the devices a thingctx gateway can drive. Adversary: a
network caller who may present crafted tokens and requests, and, separately, a
compromised or malicious Thing Description.

## Trust boundaries

- NORTH (caller -> thingctx): untrusted. The caller presents a bearer JWT. The
  guard validates it; the PEP authorizes the operation.
- SOUTH (thingctx -> device): the device leg, authenticated by the core thingctx
  outbound stack (out of scope here).
- THE PDP: trusted to make the decision, but treated as fail-closed (an
  unreachable or malformed PDP denies).
- THE TD: semi-trusted (it describes the device). We defend against a TD that
  tries to widen authorization (see T7).

## Threats and mitigations

| # | Threat | Mitigation | Verified by |
|---|--------|-----------|-------------|
| T1 | Forged / tampered token | RS256 signature verified against the provider's live JWKS; never disabled | guard tests (wrong-key rejected), live JWKS test |
| T2 | Algorithm downgrade (`none`, HS256-with-pubkey) | `alg` allowlist checked before key work + `algorithms=` to jwt.decode | guard tests (alg:none refused) |
| T3 | Confused deputy (token for another audience/resource) | exact `aud` check (incl. Cloudflare aud-array), exact `iss` check; issuer_must_contain only narrows | guard tests |
| T4 | Expired / not-yet-valid token | `exp` required, `nbf`/`exp` with bounded leeway | guard tests |
| T5 | Unauthorized op by an authenticated caller | PEP authorizes (thing, affordance, op) against the PDP before any device touch | authz tests (read/write split, deny paths) |
| T6 | PEP bypass via an unwrapped method (as_tools raw invoke, media, subscribe, a future method) | default-DENY proxy: guarded methods + safe-introspection allowlist; everything else raises | authz tests (as_tools guarded, unknown-method raises, media/stream enforced) |
| T7 | Malicious/compromised TD widening authorization | grants are TD-CLOSED: a wildcard expands only over declared (form op) tuples; readOnly never yields write/observe | authz tests (vocabulary closed, default-op filtered by readable/writable) |
| T8 | Stream outliving the token (subscribe once, receive forever) | per-delivery filter re-authorizes each value, stops the stream on grant lapse | authz test (stop-on-lapse) |
| T9 | JWKS-fetch flood (random kids -> refetch amplification) | forced-refetch cooldown bounds outbound fetches | guard hardening |
| T10 | PDP unavailable -> fail open | AuthZenPDP and JWKS fetch both deny on any error | authzen test (unreachable denies) |

## Residual risks (stated honestly)

- The (identity -> grant) POLICY is the adopter's; a wrong policy authorizes wrong
  things. thingctx enforces the policy faithfully; it cannot know your intent.
- The per-delivery filter cuts a stream FORWARD on revocation; values already
  delivered before the lapse are not clawed back (correct live-feed semantics, but
  worth knowing).
- A compromised PDP (not merely unreachable) that returns permit is trusted. Run
  your PDP as you run any authorization-critical service.
- Replay of a valid unexpired token is possible within its lifetime (standard JWT
  property); use short-lived tokens and, if needed, a jti/nonce check in your PDP.
