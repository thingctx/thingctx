# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""MQTT applier: map neutral credential material onto an MQTT connection.

MQTT authenticates at the connection (CONNECT packet / TLS), not per message, so
this produces a connect-time plan: username/password, a TLS client certificate,
and, for MQTT v5, enhanced-authentication method/data. The binding feeds the
plan to paho before ``connect()``; it holds no auth logic. Kinds with no MQTT
meaning (``SignatureCredential``, ``RequestSigner``) are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from thingctx.auth.credentials import (
    ApiKeyCredential,
    BasicCredential,
    BearerToken,
    ClientCertificate,
    Credential,
    EnhancedAuth,
)

__all__ = ["MqttAuthPlan", "apply_mqtt"]


@dataclass(repr=False)
class MqttAuthPlan:
    """How to authenticate an MQTT connection. Holds plaintext (username/
    password) only at the point of use; its ``repr`` masks credentials."""

    username: str | None = None
    password: str | None = None
    tls: ClientCertificate | None = None
    enhanced: EnhancedAuth | None = None
    properties: dict = field(default_factory=dict)

    @property
    def has_credentials(self) -> bool:
        return any((self.username is not None, self.password is not None, self.tls, self.enhanced))

    def __repr__(self) -> str:
        return (
            f"MqttAuthPlan(username={'***' if self.username is not None else None}, "
            f"password={'***' if self.password is not None else None}, "
            f"tls={self.tls!r}, enhanced={self.enhanced!r}, "
            f"properties={sorted(self.properties)!r})"
        )


def apply_mqtt(creds: list[Credential]) -> MqttAuthPlan:
    """Build an :class:`MqttAuthPlan` from neutral credential material."""
    plan = MqttAuthPlan()
    for c in creds:
        if isinstance(c, BasicCredential):
            plan.username = c.username.get_secret_value()
            plan.password = c.password.get_secret_value()
        elif isinstance(c, BearerToken):
            plan.password = c.token.get_secret_value()  # token-as-password
        elif isinstance(c, ApiKeyCredential):
            plan.password = c.value.get_secret_value()
        elif isinstance(c, ClientCertificate):
            plan.tls = c
        elif isinstance(c, EnhancedAuth):
            plan.enhanced = c
        # SignatureCredential / RequestSigner have no MQTT mapping -> ignored.
    return plan
