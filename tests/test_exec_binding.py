"""The exec binding: an exec:// form runs a local program as an argv list (no
shell), filling {var} tokens from the action arguments and reporting exit code
and output."""

from __future__ import annotations

import sys

import pytest

from thingctx import ExecBinding, ThingClient
from thingctx.testing import assert_binding_contract


def _td(command: list) -> dict:
    return {
        "@context": "https://www.w3.org/2022/wot/td/v1.1",
        "id": "urn:demo:host:v1",
        "title": "Host",
        "securityDefinitions": {"nosec_sc": {"scheme": "nosec"}},
        "security": ["nosec_sc"],
        "actions": {
            "run": {
                "input": {"type": "object", "properties": {"msg": {"type": "string"}}},
                "forms": [{"href": "exec://run", "x-thingctx-exec": {"command": command}}],
            }
        },
    }


def _client(command: list, **kw) -> ThingClient:
    kw.setdefault("allow_any", True)  # tests drive trusted commands directly
    return ThingClient(tds=[_td(command)], bindings=[ExecBinding(**kw)], approve_when="never")


def test_conforms_to_binding_contract():
    assert_binding_contract(ExecBinding())


@pytest.mark.asyncio
async def test_runs_command_and_captures_output():
    client = _client([sys.executable, "-c", "print('hello')"])
    res = await client.invoke("host.run")
    assert res["exit_code"] == 0
    assert res["stdout"] == "hello"
    assert "error" not in res


@pytest.mark.asyncio
async def test_argument_fills_one_argv_token_no_shell_split():
    # A value with spaces and shell metacharacters stays a single argv token:
    # it is echoed verbatim, never split or interpreted by a shell.
    client = _client([sys.executable, "-c", "import sys; print(sys.argv[1])", "{msg}"])
    payload = "a; rm -rf /  &&  echo $HOME"
    res = await client.invoke("host.run", {"msg": payload})
    assert res["stdout"] == payload
    assert res["exit_code"] == 0


@pytest.mark.asyncio
async def test_nonzero_exit_is_flagged_as_error():
    client = _client([sys.executable, "-c", "import sys; sys.exit(3)"])
    res = await client.invoke("host.run")
    assert res["exit_code"] == 3
    assert "error" in res


@pytest.mark.asyncio
async def test_missing_argument_is_reported():
    client = _client([sys.executable, "-c", "print('x')", "{msg}"])
    res = await client.invoke("host.run", {})
    assert "missing argument" in res["error"]


@pytest.mark.asyncio
async def test_allowlist_blocks_unlisted_program():
    client = _client([sys.executable, "-c", "print(1)"], allow=["systemctl"])
    res = await client.invoke("host.run")
    assert "not allowed" in res["error"]


@pytest.mark.asyncio
async def test_program_not_found():
    client = _client(["this-program-does-not-exist-xyz"])
    res = await client.invoke("host.run")
    assert "not found" in res["error"]


@pytest.mark.asyncio
async def test_timeout_kills_a_slow_command():
    client = _client([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)
    res = await client.invoke("host.run")
    assert res.get("timeout") is True
    assert "timed out" in res["error"]


@pytest.mark.asyncio
async def test_no_allowlist_refuses_by_default():
    # Without an allow list and without allow_any, the binding runs nothing.
    client = ThingClient(
        tds=[_td([sys.executable, "-c", "print(1)"])],
        bindings=[ExecBinding()],
        approve_when="never",
    )
    res = await client.invoke("host.run")
    assert "no allow list" in res["error"]


@pytest.mark.asyncio
async def test_allowlisted_interpreter_inline_code_is_refused():
    # Allowlisting the interpreter must not let a TD run arbitrary inline code.
    base = __import__("os").path.basename(sys.executable)
    client = _client([sys.executable, "-c", "print('pwned')"], allow=[base], allow_any=False)
    res = await client.invoke("host.run")
    assert "bypasses the allow list" in res["error"]


@pytest.mark.asyncio
async def test_child_env_is_scrubbed_of_secrets(monkeypatch):
    monkeypatch.setenv("THINGCTX_FAKE_SECRET", "s3cr3t")
    client = _client(
        [sys.executable, "-c", "import os; print(os.environ.get('THINGCTX_FAKE_SECRET', 'ABSENT'))"]
    )
    res = await client.invoke("host.run")
    assert res["stdout"] == "ABSENT"
