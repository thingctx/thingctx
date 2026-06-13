"""The simulated pump the demos drive. Reachable in-process (local forms),
over a real HTTP server with an SSE event stream (http forms), and over a
real MQTT broker (the set_coolant action).
"""

from __future__ import annotations

import json
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
        self.coolant_open = False
        self._sensors = {"temp-1": 72, "vibration": 3}
        self._listeners: list = []  # local invokers subscribed for pushes
        self._sse_queues: list = []  # HTTP/SSE client queues (over the wire)

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
    def attach(self, invoker) -> None:
        self._listeners.append(invoker)

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


DEVICE_TOKEN = "demo-bearer-token"  # the secret the TD's bearer_sc expects


def start_http_server(device: PumpDevice):
    """Start the device's real HTTP server. GET for property reads +
    sensors + the SSE stream; POST for actions. Requires the bearer token
    the TD declares (`security: bearer_sc`). Returns (base_url, server)."""

    def _authed(handler) -> bool:
        return handler.headers.get("Authorization") == f"Bearer {DEVICE_TOKEN}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send_json(self, obj, code=200):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path.endswith("/events/overheat"):
                if not _authed(self):
                    self._send_json({"error": "unauthorized"}, 401)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q: queue.Queue = queue.Queue()
                device.add_sse_queue(q)
                try:
                    while True:
                        evt = q.get()
                        self.wfile.write(f"data: {json.dumps(evt)}\n\n".encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if not _authed(self):
                self._send_json({"error": "unauthorized"}, 401)
                return
            if path.endswith("/status"):
                self._send_json(device.status())
            elif "/sensors/" in path:
                sensor_id = path.rsplit("/sensors/", 1)[1]
                self._send_json(device.read_sensor(sensor_id))
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
    to a LocalInvoker, ``td`` to thingctx, and call ``stop()`` when done.

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
    actual MQTT broker the MqttInvoker publishes to over the wire.

    Skips (returns None) if mosquitto or paho aren't available."""
    import shutil
    import subprocess
    import tempfile
    import time

    if shutil.which("mosquitto") is None:
        return None
    try:
        import paho.mqtt.client as mqtt
    except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
        return []


def pick_llm_model() -> str | None:
    """A text LLM litellm can drive: a local Ollama Qwen, else an API key,
    else None."""
    import os

    names = [n for n in _ollama_models() if "vl" not in n]
    for want in ("qwen2.5:7b", "qwen3:8b"):
        if want in names:
            return f"ollama/{want}"
    if names:
        return f"ollama/{names[0]}"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic/claude-sonnet-4-6"
    if os.environ.get("OPENAI_API_KEY"):
        return "gpt-4o"
    return None
