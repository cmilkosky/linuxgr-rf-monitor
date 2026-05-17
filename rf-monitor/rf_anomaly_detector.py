#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from rf_signal_intel import classify_signal


CONFIG_PATH = Path(os.environ.get("RF_MONITOR_ENV", "/home/cmilkosk/.config/hackrf-influx.env"))
STATUS_PATH = Path(os.environ.get("RF_MONITOR_STATUS", "/home/cmilkosk/rf-monitor/status.json"))
INTERVAL_SECONDS = int(os.environ.get("RF_ANOMALY_INTERVAL", "60"))
BASELINE_HOURS = float(os.environ.get("RF_BASELINE_HOURS", "6"))
RECENT_MINUTES = float(os.environ.get("RF_RECENT_MINUTES", "3"))
DELTA_DB = float(os.environ.get("RF_ANOMALY_DELTA_DB", "18"))
MIN_RSSI_DB = float(os.environ.get("RF_ANOMALY_MIN_RSSI_DB", "-55"))
LIMIT = int(os.environ.get("RF_ANOMALY_LIMIT", "50"))


def load_env(path: Path = CONFIG_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value.strip().strip('"').strip("'")
    return env


ENV = load_env()
INFLUX_URL = ENV["INFLUX_URL"].rstrip("/")
INFLUX_ORG = ENV["INFLUX_ORG"]
INFLUX_BUCKET = ENV["INFLUX_BUCKET"]
INFLUX_TOKEN = ENV["INFLUX_TOKEN"]


def flux_query(query: str) -> list[dict[str, str]]:
    response = requests.post(
        f"{INFLUX_URL}/api/v2/query",
        params={"org": INFLUX_ORG},
        headers={
            "Authorization": f"Token {INFLUX_TOKEN}",
            "Accept": "application/csv",
            "Content-Type": "application/vnd.flux",
        },
        data=query,
        timeout=45,
    )
    response.raise_for_status()
    return [
        row
        for row in csv.DictReader(io.StringIO(response.text))
        if row.get("result") == "_result" and row.get("_value") not in (None, "")
    ]


def line_escape(value: object) -> str:
    return str(value).replace(" ", r"\ ").replace(",", r"\,").replace("=", r"\=")


def write_lines(lines: list[str]) -> None:
    if not lines:
        return
    response = requests.post(
        f"{INFLUX_URL}/api/v2/write",
        params={"org": INFLUX_ORG, "bucket": INFLUX_BUCKET, "precision": "ns"},
        headers={"Authorization": f"Token {INFLUX_TOKEN}", "Content-Type": "text/plain"},
        data="\n".join(lines),
        timeout=15,
    )
    response.raise_for_status()


def median(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.median(values)


def flux_duration(value: float, unit: str) -> str:
    if float(value).is_integer():
        return f"{int(value)}{unit}"
    seconds_per_unit = {"h": 3600, "m": 60}[unit]
    seconds = max(1, round(value * seconds_per_unit))
    return f"{seconds}s"


def detect_once() -> dict[str, Any]:
    baseline_duration = flux_duration(BASELINE_HOURS, "h")
    recent_duration = flux_duration(RECENT_MINUTES, "m")
    query = f'''
data = from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{baseline_duration})
  |> filter(fn: (r) => r._measurement == "rf_sweep" and r._field == "rssi_db")
  |> aggregateWindow(every: 1m, fn: max, createEmpty: false)
  |> keep(columns: ["_time", "_value", "frequency_hz"])

baseline = data
  |> range(start: -{baseline_duration}, stop: -{recent_duration})
  |> group(columns: ["frequency_hz"])
  |> median(column: "_value")
  |> rename(columns: {{_value: "baseline_rssi_db"}})

recent = data
  |> range(start: -{recent_duration})
  |> group(columns: ["frequency_hz"])
  |> max(column: "_value")
  |> rename(columns: {{_value: "recent_rssi_db"}})

join(tables: {{baseline: baseline, recent: recent}}, on: ["frequency_hz"])
  |> map(fn: (r) => ({{ r with delta_db: r.recent_rssi_db - r.baseline_rssi_db }}))
  |> filter(fn: (r) => r.delta_db >= {DELTA_DB} and r.recent_rssi_db >= {MIN_RSSI_DB})
  |> group()
  |> sort(columns: ["delta_db"], desc: true)
  |> limit(n: {LIMIT})
  |> keep(columns: ["frequency_hz", "baseline_rssi_db", "recent_rssi_db", "delta_db", "_time"])
'''
    rows = flux_query(query)
    now = datetime.now(timezone.utc)
    now_ns = int(now.timestamp() * 1_000_000_000)
    anomalies = []
    lines = []
    for row in rows:
        freq = int(row["frequency_hz"])
        anomaly = {
            "frequency_hz": freq,
            "frequency_mhz": freq / 1_000_000,
            "baseline_rssi_db": float(row["baseline_rssi_db"]),
            "rssi_db": float(row["recent_rssi_db"]),
            "delta_db": float(row["delta_db"]),
            "detected_at": now.isoformat(),
        }
        anomaly["signal_intel"] = classify_signal(freq, {"anomaly": anomaly})
        anomalies.append(anomaly)
        tags = f"source={line_escape(ENV.get('RF_SOURCE', 'hackrf_linuxgr'))},frequency_hz={freq}"
        fields = (
            f"rssi_db={anomaly['rssi_db']},"
            f"baseline_rssi_db={anomaly['baseline_rssi_db']},"
            f"delta_db={anomaly['delta_db']}"
        )
        lines.append(f"rf_anomaly,{tags} {fields} {now_ns}")
    write_lines(lines)
    status = {
        "status": "ok",
        "updated_at": now.isoformat(),
        "baseline_hours": BASELINE_HOURS,
        "recent_minutes": RECENT_MINUTES,
        "delta_threshold_db": DELTA_DB,
        "min_rssi_db": MIN_RSSI_DB,
        "anomalies_active": len(anomalies),
        "latest_anomalies": anomalies,
    }
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2) + "\n")
    tmp.replace(STATUS_PATH)
    return status


def main() -> int:
    while True:
        try:
            status = detect_once()
            print(
                f"{status['updated_at']} anomalies={status['anomalies_active']} "
                f"threshold={DELTA_DB}dB min={MIN_RSSI_DB}dB",
                flush=True,
            )
        except Exception as exc:
            now = datetime.now(timezone.utc).isoformat()
            STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATUS_PATH.write_text(json.dumps({"status": "error", "updated_at": now, "error": str(exc)}, indent=2) + "\n")
            print(f"{now} error={exc}", flush=True)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
