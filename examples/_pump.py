# Copyright 2026 The thingctx Authors
# SPDX-License-Identifier: Apache-2.0
"""The simulated pump the demos drive. Reachable in-process (local forms),
over a real HTTP server with an SSE event stream (http forms), and over a
real MQTT broker (the set_coolant action).
"""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class PumpDevice:
    """The pump's logic, behind every transport."""

    def __init__(self) -> None:
        self.rpm = 0
        self.target_rpm = 0
        self.temp = 60
        self.stopped = False
        self.fault = False  # when set, the status endpoint returns 503
        self.coolant_open = False
        self._sensors = {"temp-1": 72, "vibration": 3}
        self._listeners: list = []  # local bindings subscribed for pushes
        self._sse_queues: list = []  # HTTP/SSE client queues (over the wire)
        self._jobs: dict = {}  # long-running action jobs, keyed by id

    # actions
    def set_speed(self, rpm: int) -> dict:
        self.rpm = rpm
        return {"ok": True, "rpm": rpm}

    def status(self) -> dict:
        return {"rpm": self.rpm, "temp": self.temp, "healthy": not self.stopped}

    def estop(self, reason: str = "") -> dict:
        self.stopped = True
        self.rpm = 0
        return {"stopped": True, "reason": reason}

    def read_sensor(self, id: str) -> dict:  # uriVariable: {id}
        return {"id": id, "value": self._sensors.get(id, 0)}

    def set_coolant(self, open: bool) -> dict:  # the mqtt action
        self.coolant_open = open
        return {"ok": True, "coolant_open": open}

    def start_calibrate(self, target: int = 0) -> str:
        """Run calibration as a background job; returns its id. Status and
        cancellation map to the WoT queryaction / cancelaction ops."""
        import time
        import uuid

        job_id = uuid.uuid4().hex[:8]
        job = {"status": "running", "output": None, "error": None, "cancelled": False}
        self._jobs[job_id] = job

        def _run():
            for _ in range(5):
                if job["cancelled"]:
                    job["status"] = "cancelled"
                    return
                time.sleep(0.1)
            if not job["cancelled"]:
                self.target_rpm = target
                job["status"] = "completed"
                job["output"] = {"calibrated_to": target}

        threading.Thread(target=_run, daemon=True).start()
        return job_id

    def calibrate_status(self, job_id: str) -> dict | None:
        return self._jobs.get(job_id)

    def cancel_calibrate(self, job_id: str) -> dict | None:
        job = self._jobs.get(job_id)
        if job is not None:
            job["cancelled"] = True
        return job

    # camera: render the current state to a PNG, like an on-board camera.
    # The warning light is red when temp is over the limit.
    def get_camera(self) -> bytes:
        import io

        from PIL import Image, ImageDraw

        over = self.temp > 80
        img = Image.new("RGB", (160, 120), (20, 20, 24))
        d = ImageDraw.Draw(img)
        light = (220, 40, 40) if over else (40, 200, 80)
        d.ellipse((110, 18, 150, 58), fill=light)
        d.text((12, 16), f"RPM {self.rpm}", fill=(230, 230, 230))
        d.text((12, 40), f"TEMP {self.temp}", fill=(230, 230, 230))
        d.text((12, 64), "OVER LIMIT" if over else "OK", fill=light)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    # diagnose has no method: its template is in the TD (tc:template)
    # and the prompts extension expands it client-side.

    # property read/write
    def get_rpm(self) -> int:
        return self.rpm

    def get_target_rpm(self) -> int:
        return self.target_rpm

    def set_target_rpm(self, value: int) -> dict:
        self.target_rpm = value
        return {"ok": True, "target_rpm": value}

    # telemetry: the device pushes events to its subscribers
    def attach(self, binding) -> None:
        self._listeners.append(binding)

    def add_sse_queue(self, q) -> None:
        self._sse_queues.append(q)

    def overheat(self, temp: int) -> None:
        """Emit an overheat event to local subscribers and SSE clients."""
        self.temp = temp
        evt = {"temp": temp, "limit": 80}
        for inv in self._listeners:
            inv.emit("overheat", evt)
        for q in list(self._sse_queues):
            q.put(evt)

    def start_telemetry(self, temps=(92, 94, 96, 98, 99, 101), period: float = 0.2):
        """Emit an overheat reading every `period` seconds, looping over
        `temps`. Returns the task; cancel to stop."""
        import asyncio

        async def _emit_loop():
            while True:
                for t in temps:
                    await asyncio.sleep(period)
                    self.overheat(t)

        return asyncio.ensure_future(_emit_loop())


# Demo bearer token the TD's bearer_sc expects; read from the environment so no
# secret literal is committed. The fallback keeps the example self-contained.
DEVICE_TOKEN = os.environ.get("THINGCTX_DEMO_DEVICE_TOKEN", "demo-bearer-token")


def start_http_server(device: PumpDevice):
    """Start the device's real HTTP server. GET for property reads +
    sensors + the SSE stream; POST for actions. Requires the bearer token
    the TD declares (`security: bearer_sc`). Returns (base_url, server)."""

    def _authed(handler) -> bool:
        return handler.headers.get("Authorization") == f"Bearer {DEVICE_TOKEN}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send_json(self, obj, code=200, allow_gzip=False):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            # The status form declares contentCoding=gzip; gzip the body when the
            # client requests it (Accept-Encoding), so the coding is real, not inert.
            if allow_gzip and "gzip" in self.headers.get("Accept-Encoding", ""):
                import gzip

                payload = gzip.compress(payload)
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            from urllib.parse import parse_qs, urlsplit

            query = parse_qs(urlsplit(self.path).query)
            path = self.path.split("?")[0]
            if path.endswith("/events/overheat"):
                if not _authed(self):
                    self._send_json({"error": "unauthorized"}, 401)
                    return
                # subscribe-time filter (the event's `subscription` schema):
                # only forward readings at or above the requested threshold.
                threshold = float(query["threshold"][0]) if "threshold" in query else None
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q: queue.Queue = queue.Queue()
                device.add_sse_queue(q)
                try:
                    while True:
                        evt = q.get()
                        if threshold is not None and evt.get("temp", 0) < threshold:
                            continue
                        self.wfile.write(f"data: {json.dumps(evt)}\n\n".encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if not _authed(self):
                self._send_json({"error": "unauthorized"}, 401)
                return
            if path.endswith("/status"):
                if device.fault:
                    # the status form declares this 503 error response shape
                    self._send_json({"error": "overloaded"}, 503)
                    return
                self._send_json(device.status(), allow_gzip=True)
            elif path.endswith("/all/properties"):
                # Thing-level bulk read; an optional ?props=a,b selects a subset
                # (readmultipleproperties). camera is binary, so it is omitted.
                values = {"rpm": device.rpm, "target_rpm": device.target_rpm}
                want = query["props"][0].split(",") if "props" in query else None
                if want:
                    values = {k: v for k, v in values.items() if k in want}
                self._send_json(values)
            elif "/actions/calibrate/" in path:
                # query a long-running action's status (the queryaction op)
                job = device.calibrate_status(path.rsplit("/", 1)[1])
                if job is None:
                    self.send_error(404)
                    return
                self._send_json(
                    {"status": job["status"], "output": job["output"], "error": job["error"]}
                )
            elif "/sensors/" in path:
                sensor_id = path.rsplit("/sensors/", 1)[1]
                self._send_json(device.read_sensor(sensor_id))
            else:
                self.send_error(404)

        def do_DELETE(self):
            if not _authed(self):
                self._send_json({"error": "unauthorized"}, 401)
                return
            path = self.path.split("?")[0]
            if "/actions/calibrate/" in path:
                # cancel a long-running action (the cancelaction op)
                device.cancel_calibrate(path.rsplit("/", 1)[1])
                self._send_json({"status": "cancelled"})
            else:
                self.send_error(404)

        def do_PUT(self):
            if not _authed(self):
                self._send_json({"error": "unauthorized"}, 401)
                return
            length = int(self.headers.get("Content-Length", 0))
            values = json.loads(self.rfile.read(length) or b"{}") if length else {}
            path = self.path.split("?")[0]
            if path.endswith("/all/properties"):
                # Thing-level bulk write; rpm is read-only and is ignored.
                written = {}
                if "target_rpm" in values:
                    device.set_target_rpm(values["target_rpm"])
                    written["target_rpm"] = device.target_rpm
                self._send_json({"ok": True, "written": written})
            else:
                self.send_error(404)

        def do_POST(self):
            if not _authed(self):
                self._send_json({"error": "unauthorized"}, 401)
                return
            length = int(self.headers.get("Content-Length", 0))
            args = json.loads(self.rfile.read(length) or b"{}") if length else {}
            path = self.path.split("?")[0]
            if path.endswith("/set_speed"):
                self._send_json(device.set_speed(args["rpm"]))
            elif path.endswith("/status"):
                self._send_json(device.status())
            elif path.endswith("/actions/calibrate"):
                # start a long-running action; reply 201 with a status resource
                # href the client polls (the WoT async-action lifecycle).
                job_id = device.start_calibrate(args.get("target", 0))
                href = f"http://{self.headers.get('Host')}/actions/calibrate/{job_id}"
                self._send_json({"status": "running", "href": href}, 201)
            else:
                self.send_error(404)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", server


def start_device():
    """Bring the external device fully online: a PumpDevice, its HTTP
    server (bearer-secured, SSE stream), AND a real MQTT broker bridging
    the set_coolant topic. Returns ``(pump, td, stop)``, hand ``pump``
    to a LocalBinding, ``td`` to thingctx, and call ``stop()`` when done.

    The 'external world' the demos consume; a demo body only contains
    what you write against thingctx."""
    pump = PumpDevice()
    url, server = start_http_server(pump)
    broker = start_mqtt_broker(pump)  # real mosquitto, or None
    mqtt_addr = f"{broker[0]}:{broker[1]}" if broker else "broker"
    td = pump_td(url, mqtt_addr)

    def stop():
        server.shutdown()
        if broker:
            broker[2]()

    return pump, td, stop


def start_mqtt_broker(device: PumpDevice):
    """Start a real mosquitto broker + a device-side subscriber that
    bridges the `pump/set_coolant` topic to the device. Returns
    (host, port, stop), call stop() when done. No stand-in: this is an
    actual MQTT broker the MqttBinding publishes to over the wire.

    Skips (returns None) if mosquitto or paho aren't available."""
    import shutil
    import subprocess
    import tempfile
    import time

    if shutil.which("mosquitto") is None:
        return None
    try:
        import paho.mqtt.client as mqtt
    except Exception:
        return None

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    conf = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    conf.write(f"listener {port} 127.0.0.1\nallow_anonymous true\n")
    conf.close()
    proc = subprocess.Popen(
        ["mosquitto", "-c", conf.name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.4)  # let the broker come up

    # device-side subscriber: receive set_coolant, run it, reply.
    sub = mqtt.Client()

    def _on_message(_c, _u, msg):
        args = json.loads(msg.payload.decode() or "{}")
        result = device.set_coolant(args.get("open", False))
        sub.publish("pump/set_coolant/reply", json.dumps(result))

    sub.on_message = _on_message
    sub.connect("127.0.0.1", port)
    sub.subscribe("pump/set_coolant")
    sub.loop_start()

    def stop():
        sub.loop_stop()
        sub.disconnect()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

    return "127.0.0.1", port, stop


def pump_td(http_url: str, mqtt_broker: str = "broker") -> dict:
    """Load the device's Thing Description from ``pump.td.json`` and fill
    in the runtime endpoints, the device's HTTP base and MQTT broker.

    In production the device publishes this document (you'd
    ``thingctx.from_url(...)``); here it's a file next to the demos so you
    can read the real WoT TD. ``{BASE_URL}`` / ``{MQTT_BROKER}`` are the
    only runtime bits (the broker host:port is dynamic)."""
    from pathlib import Path

    raw = (Path(__file__).parent / "pump.td.json").read_text()
    return json.loads(raw.replace("{BASE_URL}", http_url).replace("{MQTT_BROKER}", mqtt_broker))


#  pick a model for each modality, or None if none is reachable


def _ollama_models() -> list[str]:
    import urllib.request

    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return []


def pick_llm_model() -> str | None:
    """A text LLM litellm can drive: a local Ollama Qwen, else OpenRouter,
    else None."""
    import os

    names = [n for n in _ollama_models() if "vl" not in n]
    for want in ("qwen2.5:7b", "qwen3:8b"):
        if want in names:
            return f"ollama/{want}"
    if names:
        return f"ollama/{names[0]}"
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter/google/gemini-2.5-flash"
    return None


_VLM_HINTS = ("vl", "vision", "llava", "moondream", "minicpm-v", "bakllava")


def pick_vlm_model() -> str | None:
    """A vision LLM litellm can drive: an explicit override, else the smallest
    local Ollama vision model, else OpenRouter, else None."""
    import os
    import re

    if os.environ.get("THINGCTX_VLM_MODEL"):
        return os.environ["THINGCTX_VLM_MODEL"]

    def _params_b(name: str) -> float:
        m = re.search(r"(\d+(?:\.\d+)?)b", name.lower())
        return float(m.group(1)) if m else 999.0

    vision = sorted(
        (n for n in _ollama_models() if any(h in n.lower() for h in _VLM_HINTS)),
        key=_params_b,
    )
    if vision:
        return f"ollama_chat/{vision[0]}"
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter/google/gemini-2.5-flash"
    return None
