# Authorization claims, and the test that proves each

Every claim thingctx makes about authorization maps to a test you can run. This
document is the map. If a claim is not backed by a green test here, it is not
made. The last section lists what is deliberately NOT proven yet, and why, so the
line between "demonstrated" and "designed for" is explicit.

Run the evidence:

    pytest tests/test_authz.py tests/test_authz_mcp.py \
           tests/test_authz_agent_identity.py tests/test_authz_gateway.py \
           tests/test_authz_pdp_parity.py tests/test_guard.py tests/test_cloudflare_guard.py

## Enforcement (the seam)

| Claim | Proof |
|---|---|
| The same caller may READ a property and is DENIED the WRITE, per operation on one affordance. | `test_authz.py::test_read_write_split` |
| A grant is honored only for operations the TD's forms declare; a wildcard cannot invent an operation. | `test_authz.py::test_vocabulary_is_td_closed`, `test_explicit_op_restricts_default` |
| No identity, or an unknown claim, denies (fail-closed). | `test_authz.py::test_no_identity_denies` |
| Authorization is native to `ThingClient`: every device-reaching method authorizes before the device is touched. | `test_authz.py::test_native_constructor_blocks_every_method_before_device` |
| A future device-reaching method cannot silently become an unguarded hole (it raises, not forwards). | `test_authz.py::test_unknown_device_method_raises_not_forwards` |
| `as_tools()` returns the guarded invoke, so an LLM loop cannot get the raw one. | `test_authz.py::test_native_constructor_as_tools_invoke_is_authorized` |
| The gate holds across every transport (below the transport choice). | `test_authz.py::test_multi_transport_coverage` |
| With no PDP, behavior is unchanged (opt-in, backward compatible). | `test_authz.py::test_no_pdp_is_backward_compatible` |

## Streams (observe / event / media)

| Claim | Proof |
|---|---|
| A stream is authorized at subscribe time; an ungranted caller never subscribes. | `test_authz.py::test_subscribe_enforced_gate_and_denied` |
| A stream STOPS the moment the caller's token expires (checked per delivery against the real clock). | `test_authz.py::test_per_delivery_filter_stops_on_real_expiry`, `test_token_expired_reads_exp` |
| Media is enforced (as an invokeaction), not accidentally bricked or left open. | `test_authz.py::test_media_enforced_as_invokeaction` |

## Authentication (the guard, `thingctx.identity`)

| Claim | Proof |
|---|---|
| A valid token passes; the signature check is really on. | `test_guard.py::test_valid_token_passes`, `test_signature_verification_is_actually_on` |
| A forged (wrong-key), tampered, or unknown-kid token is rejected. | `test_guard.py::test_wrong_key_signature_rejected`, `test_tampered_payload_rejected`, `test_wrong_key_unknown_kid_rejected` |
| An `alg: none` (or HS256 downgrade) token is refused. | `test_guard.py::test_alg_none_downgrade_refused` |
| Wrong audience / wrong issuer / expired tokens are rejected. | `test_guard.py::test_wrong_audience_rejected`, `test_wrong_issuer_rejected`, `test_expired_token_rejected` |
| An app role (app-only token) is enforced. | `test_guard.py::test_app_role_enforced` |
| Cloudflare Access tokens (user and service) validate the same way. | `test_cloudflare_guard.py` (whole file) |

## Agents get their own identity, narrower than a user

| Claim | Proof |
|---|---|
| An AGENT principal (app-only token: `roles` + `appid`, no user `sub`) is authorized on its own identity, no user present. | `test_authz_agent_identity.py::test_agent_principal_authorized_with_no_user_present` |
| The agent's grant can be STRICTLY NARROWER than its user's: same device, same write, allowed for the user, denied for the agent. | `test_authz_agent_identity.py::test_agent_grant_is_strictly_narrower_than_user` |
| The decision is driven only by the claims. | `test_authz_agent_identity.py::test_decision_is_driven_only_by_the_claims` |

## The identity survives when thingctx owns the authn boundary (gateway)

| Claim | Proof |
|---|---|
| Caller token validated on the north; device driven with its OWN credential on the south; the token never leaks to the device. | `test_authz_gateway.py::test_gateway_validates_north_drives_south_with_device_credential` |
| An invalid token is rejected before the device is touched. | `test_authz_gateway.py::test_gateway_invalid_token_never_touches_device` |

## Bring your own PDP (AuthZEN), no lock-in

| Claim | Proof |
|---|---|
| The local PDP and an external AuthZEN PDP return the SAME decision for the same case. | `test_authz_pdp_parity.py::test_local_and_external_pdp_agree_on_every_case` |
| The caller's full claims reach the external PDP intact (identity survives the hop). | `test_authz_pdp_parity.py::test_identity_survives_the_authzen_hop` |
| A `ThingClient` wired to the external PDP enforces identically. | `test_authz_pdp_parity.py::test_external_pdp_drives_the_client_the_same_as_local` |
| The AuthZEN mapping is fail-closed (anything not an explicit permit denies). | `test_authz.py::test_authzen_mapping_and_fail_closed` |

## Authorization holds over the MCP bridge, and its one limit

| Claim | Proof |
|---|---|
| The authz gate FIRES over the MCP bridge: a granted operation flows, an ungranted one is refused before the device. | `test_authz_mcp.py::test_mcp_bridge_enforces_authz_allow`, `test_mcp_bridge_enforces_authz_deny_before_device` |
| The identity the gate sees over MCP is the bridged client's own (server-level), because MCP carries the client session, not a per-call caller identity. | `test_authz_mcp.py::test_mcp_bridge_authz_uses_server_level_identity_not_per_call` |

## NOT proven yet (the honest line)

These are limits of the ecosystem, not of thingctx, and are marked in the suite so
a reader sees the gap instead of a silent absence.

| Not yet true | Why | Marker |
|---|---|---|
| A per-CALL caller identity delivered by MCP reaches the gate, so a granted caller and an ungranted caller hitting the same MCP server get different decisions. | MCP's transport carries the client session, not a per-tool-call caller claim. There is no channel to present a distinct validated caller per call. | `test_authz_mcp.py::test_mcp_per_call_caller_identity_reaches_the_gate` is `xfail(strict=True)`: it will fail (and this row flips to proven) the day an MCP identity-propagation extension ships. |

The design is ready for that: the identity is a claims dict into the PDP, so the
day MCP delivers a per-call caller, thingctx enforces on it with no code change.
But "ready for" is not "demonstrated", and this table keeps the two apart. See
[AUTHZ_VS_MCP.md](AUTHZ_VS_MCP.md) for why the model, not the wiring, is the
difference.
