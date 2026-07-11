# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""09, authenticated RTSP, end to end and fully local: an RTSP server that
requires a username and password, consumed with thingctx.

This is how a real IP camera works. The TD declares ``basic`` security; the secret
is handed to the client at runtime, and the MediaBinding resolves it into the RTSP
URL userinfo via the same ``resolve_credentials`` primitive HTTP and MQTT use. The
TD carries no secret.

    MediaMTX (RTSP :8554, reading requires auth)
        ^ ffmpeg publishes a test pattern  ->  rtsp://127.0.0.1:8554/cam
    thingctx reads with basic credentials  ->  decoded frames

Needs two binaries on PATH::  brew install mediamtx ffmpeg
Run::  python examples/09_rtsp_local.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from thingctx.bindings import HttpBinding
from thingctx.bindings.builtin.media import MediaBinding
from thingctx.runtime import ThingClient

RTSP_URL = "rtsp://127.0.0.1:8554/cam"
# Demo credentials for the local throwaway server. Read from the environment so
# no password literal is committed; the fallback keeps the example runnable with
# no setup. The '@' and '!' in the fallback prove the userinfo is percent encoded
# by the auth layer so they cannot break the URL.
USERNAME = os.environ.get("THINGCTX_DEMO_RTSP_USER", "cam")
PASSWORD = os.environ.get("THINGCTX_DEMO_RTSP_PASSWORD", "p@ssw0rd!")

# A camera described by a TD: the form points at the rtsp:// stream (no secret),
# and `basic` security says "this stream needs a login". The secret is supplied
# to the client at runtime, not written here.
CAMERA_TD = {
    "@context": "https://www.w3.org/2022/wot/td/v1.1",
    "id": "urn:thingctx:cam:local",
    "title": "Local RTSP camera",
    "securityDefinitions": {"cam_auth": {"scheme": "basic"}},
    "security": "cam_auth",
    "actions": {
        "watch": {
            "description": "Open the RTSP stream and decode frames.",
            "forms": [{"href": RTSP_URL, "x-thingctx-media": {"container": "rtsp"}}],
        }
    },
}

# MediaMTX: anyone may publish; reading the stream requires the cam login.
MEDIAMTX_CONFIG = f"""\
authInternalUsers:
  - user: any
    permissions:
      - action: publish
  - user: {USERNAME}
    pass: "{PASSWORD}"
    permissions:
      - action: read
      - action: playback
paths:
  all_others:
"""


def _wait_port(host: str, port: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _publish_test_pattern() -> subprocess.Popen:
    """ffmpeg generates a test pattern and publishes it to the RTSP server."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=640x480:rate=15",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        RTSP_URL,
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def _first_frames(client: ThingClient, name: str, n: int, timeout: float = 20.0) -> list:
    """Pull up to n frames, bounded by a timeout (a refused read shouldn't hang)."""
    out: list = []

    async def pull() -> None:
        async for fr in await client.frames(name, track="video"):
            out.append(fr)
            if len(out) >= n:
                break

    await asyncio.wait_for(pull(), timeout=timeout)
    return out


async def main() -> None:
    if not shutil.which("mediamtx") or not shutil.which("ffmpeg"):
        print("Need mediamtx and ffmpeg on PATH. Install: brew install mediamtx ffmpeg")
        return

    # 0o600: the config carries the demo credential, so keep it owner-only.
    cfg = Path(tempfile.mkdtemp()) / "mediamtx.yml"
    fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(MEDIAMTX_CONFIG)

    server = subprocess.Popen(
        ["mediamtx", str(cfg)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    publisher: subprocess.Popen | None = None
    try:
        if not _wait_port("127.0.0.1", 8554):
            print("RTSP server did not come up on :8554")
            return
        publisher = _publish_test_pattern()
        await asyncio.sleep(2.0)  # let the publisher register the path

        print(f"RTSP server on :8554 (reading requires a login), publishing to {RTSP_URL}\n")

        # Wrong credentials are refused; proof the stream really is protected.
        wrong = ThingClient(
            tds=[CAMERA_TD],
            bindings=[MediaBinding(credentials={"local": (USERNAME, "not-the-password")})],
        )
        try:
            await _first_frames(wrong, "local.watch", 1, timeout=10.0)
            print("  unexpected: read succeeded with the wrong password")
        except Exception as exc:  # noqa: BLE001 - any failure means it was refused
            # The error is a MediaError with the password scrubbed from the URL.
            print(f"  wrong password  -> refused: {exc}")
        finally:
            await wrong.aclose()

        # Correct credentials, supplied to the client (never in the TD): the
        # MediaBinding resolves `basic` -> RTSP userinfo and the read succeeds.
        client = ThingClient(
            tds=[CAMERA_TD],
            bindings=[HttpBinding(), MediaBinding(credentials={"local": (USERNAME, PASSWORD)})],
        )
        print(f"  correct login   -> consuming {client.list_media()[0]} ...\n")
        frames = await _first_frames(client, "local.watch", 10)
        for i, fr in enumerate(frames, 1):
            print(f"    frame {i}: {fr.width}x{fr.height} {fr.encoding} pts={fr.pts}")
        await client.aclose()
        print("\nOK: thingctx authenticated to a protected RTSP stream via the auth layer.")
    finally:
        for proc in (publisher, server):
            if proc is None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
