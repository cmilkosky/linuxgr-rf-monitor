#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path


HASS_ENV = Path(os.environ.get("HASS_ENV", "/home/cmilkosk/.config/cowrie/ha.env"))
STATUS_PATH = Path(os.environ.get("RF_MONITOR_STATUS", "/home/cmilkosk/rf-monitor/status.json"))
STATE_TOPIC = os.environ.get("RF_MONITOR_STATE_TOPIC", "linuxgr/rf_monitor/state")
DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in HASS_ENV.read_text().splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        env[key] = value.strip()
    return env


ENV = load_env()
HASS_SERVER = ENV["HASS_SERVER"].rstrip("/")
HASS_TOKEN = ENV["HASS_TOKEN"]


def hass_post(path: str, payload: dict) -> None:
    req = urllib.request.Request(
        f"{HASS_SERVER}{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {HASS_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        response.read()


def mqtt_publish(topic: str, payload: dict | str, retain: bool = False) -> None:
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    hass_post("/api/services/mqtt/publish", {"topic": topic, "payload": payload, "retain": retain, "qos": 0})


def sensor(object_id: str, name: str, template: str, icon: str, unit: str | None = None) -> tuple:
    return object_id, name, template, icon, unit


SENSORS = [
    sensor("linuxgr_rf_monitor_status", "LinuxGR RF Monitor Status", "{{ value_json.status }}", "mdi:access-point", None),
    sensor("linuxgr_rf_monitor_anomalies", "LinuxGR RF Active Anomalies", "{{ value_json.anomalies_active }}", "mdi:alert-decagram-outline", "events"),
    sensor("linuxgr_rf_monitor_last_update", "LinuxGR RF Last Update", "{{ value_json.updated_at }}", "mdi:clock-outline", None),
    sensor("linuxgr_rf_monitor_threshold", "LinuxGR RF Delta Threshold", "{{ value_json.delta_threshold_db }}", "mdi:delta", "dB"),
    sensor("linuxgr_rf_monitor_min_rssi", "LinuxGR RF Min RSSI", "{{ value_json.min_rssi_db }}", "mdi:signal", "dB"),
    sensor("linuxgr_rf_monitor_overview", "LinuxGR RF Overview", "{{ value_json.anomalies_active }}", "mdi:radio-tower", "events"),
]


def publish_discovery() -> None:
    device = {
        "identifiers": ["linuxgr_rf_monitor"],
        "name": "LinuxGR RF Monitor",
        "manufacturer": "Codex",
        "model": "HackRF sweep monitor",
    }
    for object_id, name, template, icon, unit in SENSORS:
        payload = {
            "name": name,
            "state_topic": STATE_TOPIC,
            "value_template": template,
            "icon": icon,
            "device": device,
            "unique_id": object_id,
        }
        if unit:
            payload["unit_of_measurement"] = unit
            payload["state_class"] = "measurement"
        if object_id == "linuxgr_rf_monitor_overview":
            payload["json_attributes_topic"] = STATE_TOPIC
        mqtt_publish(f"{DISCOVERY_PREFIX}/sensor/{object_id}/config", payload, retain=True)


def main() -> int:
    status = json.loads(STATUS_PATH.read_text()) if STATUS_PATH.exists() else {"status": "unknown", "anomalies_active": 0}
    publish_discovery()
    mqtt_publish(STATE_TOPIC, status, retain=True)
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
