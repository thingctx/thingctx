"""AuthZEN interop: speak the OpenID Authorization API 1.0 Final shape.

thingctx ships a lean, zero-dependency local PDP as its default (see
:class:`~thingctx.authz.pdp.PolicyDecisionPoint`). This module makes the same
decision boundary speak the AuthZEN standard, so an adopter can point thingctx at
ANY AuthZEN-conformant Policy Decision Point (their OPA, a Cedar service, an
enterprise PDP) without changing thingctx.

Two directions, both here:

* OUTBOUND (thingctx is the PEP calling an external PDP):
  :class:`AuthZenPDP` maps a thingctx :class:`AccessRequest` to an AuthZEN
  Access Evaluation request, POSTs it to the configured endpoint, and reads the
  ``decision`` back. Drop it in wherever a :class:`PolicyDecisionPoint` is used;
  the PEP is unchanged.

* MAPPING (shared by both directions): :func:`to_authzen_request` and
  :func:`from_authzen_response` are the exact 1.0 field mapping, so the same
  translation is used whether thingctx calls out or (future) serves in.

The 1.0 Final shape (openid.github.io/authzen):
    POST /access/v1/evaluation
    request  {subject{type,id,properties?}, action{name,properties?},
              resource{type,id,properties?}, context?}
    response {decision: bool, context?}

The mapping thingctx uses:
    subject  = the validated identity (type "identity", id from sub/oid/appid)
    action   = the WoT operation (name = the op: readproperty/writeproperty/
               invokeaction/subscribeevent)
    resource = the affordance (type = affordance kind if known else "affordance",
               id = "<thing_id>/<affordance>")
    context  = the raw claims + the form scheme, for the external PDP's own rules
"""

from __future__ import annotations

from typing import Any

from thingctx.authz.pdp import AccessRequest, Decision

# The standard endpoint path (a base URL + this path). A deployment may override.
AUTHZEN_EVALUATION_PATH = "/access/v1/evaluation"


def _subject_id(identity: Any) -> str:
    """A stable subject id from the validated claims. Prefer the standard subject
    claims in order; fall back to a string form. Never invents an id."""
    if isinstance(identity, dict):
        for k in ("sub", "oid", "appid", "azp", "client_id", "common_name"):
            v = identity.get(k)
            if v:
                return str(v)
        return "unknown"
    return str(identity) if identity is not None else "anonymous"


def to_authzen_request(identity: Any, request: AccessRequest) -> dict:
    """Map a thingctx (identity, AccessRequest) to an AuthZEN 1.0 evaluation
    request body. The op becomes the action name; the (thing, affordance) becomes
    the resource; the claims + form scheme become context for the external PDP."""
    subject = {"type": "identity", "id": _subject_id(identity)}
    if isinstance(identity, dict):
        # Pass the claims as subject properties so a policy can read roles/scp/etc.
        subject["properties"] = dict(identity)
    action = {"name": request.op}
    resource = {
        "type": "affordance",
        "id": f"{request.thing_id}/{request.affordance}",
        "properties": {"thing": request.thing_id, "affordance": request.affordance},
    }
    context: dict[str, Any] = {}
    if request.form_scheme is not None:
        context["form_scheme"] = request.form_scheme
    body: dict[str, Any] = {"subject": subject, "action": action, "resource": resource}
    if context:
        body["context"] = context
    return body


def from_authzen_response(payload: Any, request: AccessRequest) -> Decision:
    """Read an AuthZEN 1.0 evaluation response into a thingctx Decision.

    Fail closed: anything that is not an explicit ``{"decision": true}`` denies.
    A missing/malformed response, a non-bool decision, or ``false`` all deny."""
    if not isinstance(payload, dict):
        return Decision(permit=False, reason="AuthZEN PDP returned a non-object response")
    decision = payload.get("decision")
    if decision is True:
        return Decision(permit=True)
    # Surface the PDP's reason if it provided one in context, without trusting it
    # to change the deny.
    ctx = payload.get("context")
    reason = ""
    if isinstance(ctx, dict):
        reason = str(ctx.get("reason") or ctx.get("id") or "")
    return Decision(
        permit=False,
        reason=reason
        or f"AuthZEN PDP denied {request.op} on {request.thing_id}/{request.affordance}",
    )


class AuthZenPDP:
    """A PDP that delegates the decision to an external AuthZEN endpoint.

    Drop-in for :class:`PolicyDecisionPoint`: it exposes the same
    ``async decide(identity, request) -> Decision``, so the PEP does not change.
    The difference is WHERE the decision is made, an adopter's OPA, Cedar, or any
    AuthZEN-conformant PDP, reached over HTTPS.

    Fail closed on every error: a network failure, a non-2xx, or a malformed body
    all deny. Authorization must never fall open because the PDP was unreachable.

    Args:
        base_url: the PDP base URL; the evaluation path is appended.
        path: the evaluation path (default the 1.0 standard path).
        headers: extra headers (e.g. an auth token for the PDP itself).
        timeout: request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        *,
        path: str = AUTHZEN_EVALUATION_PATH,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self._url = base_url.rstrip("/") + path
        self._headers = {"Content-Type": "application/json", **(headers or {})}
        self._timeout = timeout

    async def decide(self, identity: Any, request: AccessRequest) -> Decision:
        body = to_authzen_request(identity, request)
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url, json=body, headers=self._headers)
                resp.raise_for_status()
                payload = resp.json()
        except Exception:  # noqa: BLE001 - any failure denies (fail closed)
            return Decision(
                permit=False,
                reason="AuthZEN PDP was unreachable or returned an error; denied fail-closed",
            )
        return from_authzen_response(payload, request)
