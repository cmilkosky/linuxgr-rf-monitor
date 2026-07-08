#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
import struct
import sys
from pathlib import Path
from typing import Any


HA_HOST = os.environ.get("HASS_HOST", "192.168.202.56")
HA_PORT = int(os.environ.get("HASS_PORT", "8123"))
RF_URL = os.environ.get("RF_MONITOR_URL", "http://LinuxGR.lan:8099")
TOKEN_PATHS = [
    Path(value)
    for value in [
        os.environ.get("HASS_TOKEN_FILE", ""),
        "/home/cmilkosk/.config/cowrie/ha.env",
        "/private/tmp/ha_token",
    ]
    if value
]


def load_token() -> str:
    for path in TOKEN_PATHS:
        if not str(path) or not path.exists():
            continue
        text = path.read_text().strip()
        if "HASS_TOKEN=" in text:
            for line in text.splitlines():
                if line.startswith("HASS_TOKEN="):
                    return line.split("=", 1)[1].strip()
        if text:
            return text
    raise RuntimeError("No Home Assistant token found")


class HAWebSocket:
    def __init__(self) -> None:
        self.sock: socket.socket | None = None
        self.next_id = 1

    def connect(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        sock = socket.create_connection((HA_HOST, HA_PORT), timeout=15)
        request = (
            "GET /api/websocket HTTP/1.1\r\n"
            f"Host: {HA_HOST}:{HA_PORT}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("No websocket handshake response")
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(response.decode(errors="replace"))
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if expected.lower() not in response.decode(errors="replace").lower():
            raise RuntimeError("Bad websocket accept header")
        self.sock = sock

    def recv_json(self) -> dict[str, Any]:
        sock = self.sock
        if sock is None:
            raise RuntimeError("not connected")
        header = sock.recv(2)
        if len(header) < 2:
            raise RuntimeError("short websocket frame")
        first, second = header
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(payload))
        if opcode == 8:
            raise RuntimeError("websocket closed")
        if opcode != 1:
            return self.recv_json()
        return json.loads(payload.decode())

    def _recv_exact(self, length: int) -> bytes:
        sock = self.sock
        if sock is None:
            raise RuntimeError("not connected")
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            chunk = sock.recv(remaining)
            if not chunk:
                raise RuntimeError("short websocket payload")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def send_json(self, value: dict[str, Any]) -> None:
        sock = self.sock
        if sock is None:
            raise RuntimeError("not connected")
        payload = json.dumps(value, separators=(",", ":")).encode()
        mask = random.randbytes(4) if hasattr(random, "randbytes") else bytes(random.getrandbits(8) for _ in range(4))
        if len(payload) < 126:
            header = struct.pack("!BB", 0x81, 0x80 | len(payload))
        elif len(payload) < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, len(payload))
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, len(payload))
        masked = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(payload))
        sock.sendall(header + mask + masked)

    def request(self, type_: str, **extra: Any) -> dict[str, Any]:
        ident = self.next_id
        self.next_id += 1
        self.send_json({"id": ident, "type": type_, **extra})
        while True:
            msg = self.recv_json()
            if msg.get("id") == ident:
                return msg

    def auth(self, token: str) -> None:
        required = self.recv_json()
        if required.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected auth greeting: {required}")
        self.send_json({"type": "auth", "access_token": token})
        ok = self.recv_json()
        if ok.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {ok}")

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None


def radio_health_view() -> dict[str, Any]:
    return {
        "title": "Radio Health",
        "path": "radio-health",
        "icon": "mdi:radio-tower",
        "cards": [
            {
                "type": "iframe",
                "url": f"{RF_URL}/ha-radio-health",
                "aspect_ratio": "115%",
            },
        ],
    }


def find_rf_dashboard(ws: HAWebSocket) -> tuple[str, dict[str, Any]]:
    candidates = ["rf-monitor", "rf-monitor-dashboard", "radio", "lovelace-rf-monitor", "lovelace"]
    last_errors: list[str] = []
    for url_path in candidates:
        result = ws.request("lovelace/config", url_path=url_path)
        if result.get("success"):
            config = result["result"]
            text = json.dumps(config).lower()
            if url_path != "lovelace" or "rf monitor" in text or "8099" in text:
                return url_path, config
        else:
            last_errors.append(f"{url_path}: {result.get('error')}")
    raise RuntimeError("Could not find RF Monitor Lovelace dashboard. Tried: " + "; ".join(last_errors))


def main() -> int:
    token = load_token()
    ws = HAWebSocket()
    ws.connect()
    try:
        ws.auth(token)
        url_path, config = find_rf_dashboard(ws)
        backup = Path(f"/tmp/ha-rf-dashboard-before-radio-health-{url_path}.json")
        backup.write_text(json.dumps(config, indent=2))
        view = radio_health_view()
        views = config.setdefault("views", [])
        index = next((idx for idx, item in enumerate(views) if item.get("path") == "radio-health" or item.get("title") == "Radio Health"), None)
        if index is None:
            views.append(view)
        else:
            views[index] = view
        saved = ws.request("lovelace/config/save", url_path=url_path, config=config)
        print(json.dumps({"url_path": url_path, "backup": str(backup), "saved": saved}, indent=2))
        return 0 if saved.get("success") else 1
    finally:
        ws.close()


if __name__ == "__main__":
    raise SystemExit(main())
