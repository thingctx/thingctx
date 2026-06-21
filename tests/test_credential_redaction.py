"""Credentials must never render their secrets. These tests guard the redacting
repr on Credential and catch any future subclass that forgets to inherit it."""

import dataclasses

from thingctx.auth import credentials as C

SENTINEL = "LEAKSENTINEL_DO_NOT_PRINT"

CASES = [
    C.BearerToken(token=SENTINEL),
    C.BasicCredential(username=SENTINEL, password=SENTINEL),
    C.ApiKeyCredential(name="X-Key", value=SENTINEL),
    C.SignatureCredential(
        algorithm="aws-sigv4", key_id=SENTINEL, secret_key=SENTINEL, token=SENTINEL
    ),
    C.ClientCertificate(certfile="/path/cert.pem", password=SENTINEL),
    C.EnhancedAuth(method="MECH", data=SENTINEL.encode()),
]


def test_secret_never_appears_in_text_forms():
    for cred in CASES:
        for text in (repr(cred), str(cred), f"{cred}", format(cred)):
            assert SENTINEL not in text, type(cred).__name__


def test_every_secret_tagged_field_is_masked():
    for cred in CASES:
        for f in dataclasses.fields(cred):
            if f.metadata.get("secret"):
                value = getattr(cred, f.name)
                if value not in (None, "", b""):
                    assert repr(value) not in repr(cred), (type(cred).__name__, f.name)


def test_subclasses_share_the_redacting_base_repr():
    # A subclass that forgets @dataclass(repr=False) would regenerate a leaking
    # repr; every Credential must resolve to the masked base implementation.
    for sub in C.Credential.__subclasses__():
        assert sub.__repr__ is C.Credential.__repr__, sub.__name__


def test_equality_still_uses_all_fields():
    assert C.BearerToken(token="a") == C.BearerToken(token="a")
    assert C.BearerToken(token="a") != C.BearerToken(token="b")
