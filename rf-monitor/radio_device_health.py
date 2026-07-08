#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HASS_ENV = Path(os.environ.get("HASS_ENV", "/home/cmilkosk/.config/cowrie/ha.env"))
STATUS_PATH = Path(os.environ.get("RADIO_HEALTH_STATUS", "/home/cmilkosk/rf-monitor/radio-health.json"))
HISTORY_PATH = Path(os.environ.get("RADIO_HEALTH_HISTORY", "/home/cmilkosk/rf-monitor/radio-health-history.jsonl"))
STATE_TOPIC = os.environ.get("RADIO_HEALTH_STATE_TOPIC", "linuxgr/radio_health/state")
DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant")
HISTORY_DAYS = int(os.environ.get("RADIO_HEALTH_HISTORY_DAYS", "7"))

EXPECTED_RADIOS = [
    {
        "id": "hackrf",
        "name": "HackRF",
        "kind": "hackrf",
        "vendor": "1d50",
        "product": "6089",
        "required_service": "hackrf-influx.service",
        "icon": "mdi:access-point-network",
    },
    {
        "id": "sdrplay",
        "name": "SDRplay RSPdxR2",
        "kind": "sdrplay",
        "vendor": "1df7",
        "product": "3060",
        "required_service": "sdrplay.service",
        "icon": "mdi:radio-tower",
    },
    {
        "id": "rtl0",
        "name": "RTL-SDR RTL0",
        "kind": "rtlsdr",
        "vendor": "0bda",
        "product": "2838",
        "serial": "RTL0",
        "icon": "mdi:usb-port",
    },
    {
        "id": "rtl2",
        "name": "RTL-SDR RTL2",
        "kind": "rtlsdr",
        "vendor": "0bda",
        "product": "2838",
        "serial": "RTL2",
        "icon": "mdi:usb-port",
    },
    {
        "id": "rtl3",
        "name": "RTL-SDR RTL3",
        "kind": "rtlsdr",
        "vendor": "0bda",
        "product": "2838",
        "serial": "RTL3",
        "icon": "mdi:usb-port",
    },
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not HASS_ENV.exists():
        return env
    for line in HASS_ENV.read_text().splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        env[key] = value.strip()
    return env


def read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def usb_devices() -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for vendor_path in sorted(Path("/sys/bus/usb/devices").glob("*/idVendor")):
        base = vendor_path.parent
        vendor = read_text(base / "idVendor").lower()
        product = read_text(base / "idProduct").lower()
        if not vendor or not product:
            continue
        devices.append(
            {
                "sysfs": base.name,
                "vendor": vendor,
                "product": product,
                "manufacturer": read_text(base / "manufacturer"),
                "name": read_text(base / "product"),
                "serial": read_text(base / "serial"),
                "bus": read_text(base / "busnum"),
                "device": read_text(base / "devnum"),
                "speed": read_text(base / "speed"),
            }
        )
    return devices


def service_state(service: str) -> str:
    result = subprocess.run(["systemctl", "is-active", service], text=True, capture_output=True, timeout=5, check=False)
    return result.stdout.strip() or result.stderr.strip() or "unknown"


def match_device(expected: dict[str, Any], devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [
        item
        for item in devices
        if item["vendor"] == expected["vendor"].lower() and item["product"] == expected["product"].lower()
    ]
    expected_serial = expected.get("serial")
    if expected_serial:
        for item in matches:
            if item.get("serial") == expected_serial:
                return item
        return None
    return matches[0] if matches else None


def build_status() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    devices = usb_devices()
    radio_items: list[dict[str, Any]] = []
    missing = 0
    degraded = 0
    for expected in EXPECTED_RADIOS:
        found = match_device(expected, devices)
        service = expected.get("required_service")
        svc_state = service_state(service) if service else None
        present = found is not None
        healthy = present and (svc_state in (None, "active"))
        if not present:
            missing += 1
        elif not healthy:
            degraded += 1
        radio_items.append(
            {
                "id": expected["id"],
                "name": expected["name"],
                "kind": expected["kind"],
                "present": present,
                "healthy": healthy,
                "status": "ok" if healthy else "missing" if not present else "degraded",
                "icon": expected["icon"],
                "expected_serial": expected.get("serial"),
                "service": service,
                "service_state": svc_state,
                "usb": found,
            }
        )
    overall = "ok"
    if missing:
        overall = "missing"
    elif degraded:
        overall = "degraded"
    score = round(100 * sum(1 for item in radio_items if item["healthy"]) / max(1, len(radio_items)))
    return {
        "updated_at": now.isoformat(),
        "host": "linuxGR",
        "status": overall,
        "score": score,
        "poll_ok": overall == "ok",
        "expected_count": len(EXPECTED_RADIOS),
        "present_count": sum(1 for item in radio_items if item["present"]),
        "healthy_count": sum(1 for item in radio_items if item["healthy"]),
        "missing_count": missing,
        "degraded_count": degraded,
        "devices": radio_items,
    }


def append_history(status: dict[str, Any]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "updated_at": status["updated_at"],
        "status": status["status"],
        "score": status["score"],
        "poll_ok": status["poll_ok"],
        "missing_count": status["missing_count"],
        "degraded_count": status["degraded_count"],
        "devices": {item["id"]: item["status"] for item in status["devices"]},
    }
    with HISTORY_PATH.open("a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    prune_history()


def parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def prune_history() -> None:
    if not HISTORY_PATH.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
    kept: list[str] = []
    for line in HISTORY_PATH.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = parse_dt(str(record.get("updated_at", "")))
        if ts and ts >= cutoff:
            kept.append(json.dumps(record, separators=(",", ":")))
    HISTORY_PATH.write_text("\n".join(kept) + ("\n" if kept else ""))


def hass_post(server: str, token: str, path: str, payload: dict[str, Any]) -> None:
    req = urllib.request.Request(
        f"{server.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        response.read()


def mqtt_publish(server: str, token: str, topic: str, payload: dict[str, Any] | str, retain: bool = False) -> None:
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    hass_post(server, token, "/api/services/mqtt/publish", {"topic": topic, "payload": payload, "retain": retain, "qos": 0})


def publish_discovery(status: dict[str, Any]) -> None:
    env = load_env()
    server = env.get("HASS_SERVER")
    token = env.get("HASS_TOKEN")
    if not server or not token:
        return
    device = {
        "identifiers": ["linuxgr_radio_health"],
        "name": "LinuxGR Radio Health",
        "manufacturer": "Codex",
        "model": "SDR device health monitor",
    }
    sensors = [
        ("linuxgr_radio_health_status", "LinuxGR Radio Health Status", "{{ value_json.status }}", "mdi:radio-tower", None),
        ("linuxgr_radio_health_score", "LinuxGR Radio Health Score", "{{ value_json.score }}", "mdi:gauge", "%"),
        ("linuxgr_radio_health_present", "LinuxGR Radios Present", "{{ value_json.present_count }}", "mdi:usb-port", "devices"),
        ("linuxgr_radio_health_missing", "LinuxGR Radios Missing", "{{ value_json.missing_count }}", "mdi:alert-circle-outline", "devices"),
    ]
    for object_id, name, template, icon, unit in sensors:
        payload: dict[str, Any] = {
            "name": name,
            "state_topic": STATE_TOPIC,
            "value_template": template,
            "icon": icon,
            "device": device,
            "unique_id": object_id,
            "json_attributes_topic": STATE_TOPIC if object_id == "linuxgr_radio_health_status" else None,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        if unit:
            payload["unit_of_measurement"] = unit
            payload["state_class"] = "measurement"
        mqtt_publish(server, token, f"{DISCOVERY_PREFIX}/sensor/{object_id}/config", payload, retain=True)
    for item in status["devices"]:
        object_id = f"linuxgr_radio_{item['id']}"
        payload = {
            "name": f"LinuxGR {item['name']}",
            "state_topic": STATE_TOPIC,
            "value_template": f"{{{{ 'ON' if value_json.devices | selectattr('id','eq','{item['id']}') | map(attribute='healthy') | first else 'OFF' }}}}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "connectivity",
            "icon": item["icon"],
            "device": device,
            "unique_id": object_id,
        }
        mqtt_publish(server, token, f"{DISCOVERY_PREFIX}/binary_sensor/{object_id}/config", payload, retain=True)
    mqtt_publish(server, token, STATE_TOPIC, status, retain=True)


def main() -> int:
    status = build_status()
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2))
    append_history(status)
    publish_discovery(status)
    print(json.dumps(status, indent=2))
    return 0 if status["status"] in {"ok", "degraded", "missing"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
