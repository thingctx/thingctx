# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The identity survives end to end when thingctx owns the authn boundary.

This is the gateway (north-south) shape. A caller presents a real Entra-shaped
access token on the NORTH side; the guard
validates it (signature against the JWKS, issuer, audience, expiry, scope); only
if it passes does thingctx drive the DEVICE on the SOUTH side, using the device's
OWN native credential, never the caller's token. The negative case proves the
device is never touched when the inbound token is invalid.

The claim this validates: when thingctx (not MCP) owns the authentication
boundary, the caller identity survives all the way to the authorization decision,
and the two credentials (caller vs device) stay separate.
"""

from __future__ import annotations

import base64

import pytest

from thingctx import HttpBinding, ThingClient
from thingctx.identity import AuthorizationError, EntraGatewayGuard

AUDIENCE = "api://thingctx-gateway"
TENANT = "11111111-2222-3333-4444-555555555555"

DEVICE_USER = "pump-controller"
DEVICE_PASS = "s3cr3t-device-password"

DEVICE_TD = {
    "@context": "https://www.w3.org/2019/wot/td/v1.1",
    "id": "urn:thingctx:water-pump:v1",
    "title": "Water Pump Controller",
    "securityDefinitions": {"device_basic": {"scheme": "basic", "in": "header"}},
    "security": ["device_basic"],
    "actions": {
        "setSpeed": {
            "input": {"type": "object", "properties": {"rpm": {"type": "integer"}}},
            "forms": [{"href": "https://pump.local/api/set-speed", "htv:methodName": "POST"}],
        }
    },
}


def _device_stub(capture):
    """An in-process httpx transport that plays the device: it records the
    Authorization header it received and returns a result."""
    import httpx

    real_send = httpx.AsyncClient.send

    async def stub_send(self, request, **kwargs):
        if "pump.local" in str(request.url):
            capture["device_auth"] = request.headers.get("Authorization")
            capture["device_body"] = request.content.decode() or ""
            return httpx.Response(200, json={"ok": True, "rpm": 1200}, request=request)
        return await real_send(self, request, **kwargs)

    return real_send, stub_send


@pytest.mark.asyncio
async def test_gateway_validates_north_drives_south_with_device_credential(keypair, monkeypatch):
    """Positive: token validated on the north, device driven with its OWN Basic
    auth on the south, and the caller's token never leaks to the device."""
    import httpx

    guard = EntraGatewayGuard(
        tenant_id=TENANT,
        audience=AUDIENCE,
        required_scopes=["Things.Invoke"],
        jwks=keypair.jwks(),  # self-signed test key; no network
    )
    capture: dict = {}
    real_send, stub_send = _device_stub(capture)
    monkeypatch.setattr(httpx.AsyncClient, "send", stub_send)

    device = HttpBinding(
        credentials={"water-pump": {"username": DEVICE_USER, "password": DEVICE_PASS}}
    )
    client = ThingClient(tds=[DEVICE_TD], bindings=[device])

    token = keypair.mint(scp="Things.Invoke")
    claims, result = await guard.authorize_and_invoke(
        token, client, "water-pump.setSpeed", {"rpm": 1200}
    )

    # North: the caller identity survived validation into claims.
    assert claims["aud"] == AUDIENCE
    assert claims["scp"] == "Things.Invoke"
    # South: the device was driven with ITS OWN Basic credential, not the token.
    expected_basic = "Basic " + base64.b64encode(f"{DEVICE_USER}:{DEVICE_PASS}".encode()).decode()
    assert capture["device_auth"] == expected_basic
    assert "Bearer" not in (capture["device_auth"] or ""), "caller token leaked to the device"
    assert result == {"ok": True, "rpm": 1200}
    await client.aclose()


@pytest.mark.asyncio
async def test_gateway_invalid_token_never_touches_device(keypair, monkeypatch):
    """Negative: a wrong-audience token is rejected on the north side, and the
    south-side device call never happens."""
    import httpx

    guard = EntraGatewayGuard(
        tenant_id=TENANT,
        audience=AUDIENCE,
        required_scopes=["Things.Invoke"],
        jwks=keypair.jwks(),
    )
    capture: dict = {}
    real_send, stub_send = _device_stub(capture)
    monkeypatch.setattr(httpx.AsyncClient, "send", stub_send)

    device = HttpBinding(
        credentials={"water-pump": {"username": DEVICE_USER, "password": DEVICE_PASS}}
    )
    client = ThingClient(tds=[DEVICE_TD], bindings=[device])

    bad_token = keypair.mint(aud="api://attacker-app")  # wrong audience
    with pytest.raises(AuthorizationError):
        await guard.authorize_and_invoke(bad_token, client, "water-pump.setSpeed", {"rpm": 9999})

    assert "device_auth" not in capture, "the device was touched despite an invalid token"
    await client.aclose()
