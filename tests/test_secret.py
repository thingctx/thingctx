"""Unit tests for the Secret container: masked display, explicit unwrap,
constant-time equality, blocked serialization/copy, and wiping."""

import copy
import pickle

import pytest

from thingctx.auth import Secret

PLAIN = "super-secret-value"


def test_value_never_appears_in_text_forms():
    s = Secret(PLAIN)
    for text in (repr(s), str(s), f"{s}", format(s), f"{s!r}"):
        assert PLAIN not in text
        assert "***" in text


def test_explicit_unwrap_returns_value():
    assert Secret(PLAIN).get_secret_value() == PLAIN
    assert Secret(b"\x00\x01rawbytes").get_secret_bytes() == b"\x00\x01rawbytes"
    assert Secret("ünïcödé").get_secret_value() == "ünïcödé"


def test_constant_time_equality():
    assert Secret("a") == Secret("a")
    assert Secret("a") == "a"
    assert Secret("a") == b"a"
    assert Secret("a") != Secret("b")
    assert Secret("a") != "b"
    assert Secret("a") != 123  # foreign type: not equal, no crash


def test_unhashable():
    with pytest.raises(TypeError):
        hash(Secret(PLAIN))


def test_pickle_is_blocked():
    with pytest.raises(TypeError):
        pickle.dumps(Secret(PLAIN))


def test_copy_and_deepcopy_are_blocked():
    s = Secret(PLAIN)
    with pytest.raises(TypeError):
        copy.copy(s)
    with pytest.raises(TypeError):
        copy.deepcopy(s)


def test_wipe_zeroes_and_blocks_access():
    s = Secret(PLAIN)
    assert bool(s) is True
    s.wipe()
    assert bool(s) is False
    with pytest.raises(RuntimeError):
        s.get_secret_value()
    s.wipe()  # idempotent


def test_context_manager_wipes_on_exit():
    s = Secret(PLAIN)
    with s as inner:
        assert inner.get_secret_value() == PLAIN
    with pytest.raises(RuntimeError):
        s.get_secret_value()


def test_rejects_non_string_bytes():
    with pytest.raises(TypeError):
        Secret(12345)


def test_optional_mlock_is_best_effort():
    # lock=True must never raise even where mlock is unavailable/forbidden.
    s = Secret(PLAIN, lock=True)
    assert s.get_secret_value() == PLAIN
    s.wipe()


def test_credential_secret_fields_are_coerced_and_wipeable():
    from thingctx.auth import BasicCredential, BearerToken

    b = BearerToken(token="tok")
    assert isinstance(b.token, Secret)
    assert b.token.get_secret_value() == "tok"
    assert b.scheme == "Bearer"  # non-secret field stays plain

    ba = BasicCredential(username="u", password="p")
    ba.wipe()
    with pytest.raises(RuntimeError):
        ba.password.get_secret_value()
