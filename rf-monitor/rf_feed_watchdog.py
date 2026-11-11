#!/usr/bin/env python3
"""LinuxGR RF feed watchdog: alarm when the sweep goes dead.

A dead feed and a healthy feed look identical on a waterfall once the sweep
keeps running: on 2026-11-07 every rf_sweep bin sat at the receiver floor from
08:30 to 13:44 the next day (29 hours) and nothing alerted, because "no signal"
is the normal state of most bins.

The discriminator is spectral spread, not level: a live feed always has some
narrow peaks standing out of its own noise floor (weather, pagers, cell
downlink). When max-minus-median across the whole band collapses below a few
dB, the antenna or the coax has come off.

Publishes binary_sensor.linuxgr_rf_feed to Home Assistant ("on" = dead) and
writes STATE_PATH for the local dashboards.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENV_PATH = Path(os.environ.get("RF_MONITOR_ENV", "/home/cmilkosk/.config/hackrf-influx.env"))
HASS_ENV = Path(os.environ.get("HASS_ENV", "/home/cmilkosk/.config/cowrie/ha.env"))
STATE_PATH = Path(os.environ.get("RF_FEED_STATE", "/home/cmilkosk/rf-monitor/rf-feed-health.json"))
ENTITY_ID = os.environ.get("RF_FEED_ENTITY_ID", "binary_sensor.linuxgr_rf_feed")
WINDOW_MIN = int(os.environ.get("RF_FEED_WINDOW_MIN", "10"))
MIN_SPREAD_DB = float(os.environ.get("RF_FEED_MIN_SPREAD_DB", "6"))
MIN_BINS = int(os.environ.get("RF_FEED_MIN_BINS", "100"))
HASS_TIMEOUT = float(os.environ.get("RF_FEED_HASS_TIMEOUT", "8"))


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("'\"")
    return out


def query(env: dict[str, str], flux: str) -> list[dict[str, str]]:
    """Run Flux against the Influx HTTP API and parse the annotated CSV."""
    url = env["INFLUX_URL"].rstrip("/") + "/api/v2/query?" + urllib.parse.urlencode(
        {"org": env["INFLUX_ORG"], "type": "flux"}
    )
    req = urllib.request.Request(
        url,
        data=flux.encode(),
        headers={
            "Authorization": f"Token {env['INFLUX_TOKEN']}",
            "Accept": "application/csv",
            # Influx wants the raw-Flux media type; text/plain is parsed as JSON.
            "Content-Type": "application/vnd.flux",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # A 400 from Influx is usually a Flux parse error; surface the body or the
        # watchdog just reports "could not read the sweep" with no way to debug it.
        raise RuntimeError(f"Influx {exc.code}: {exc.read().decode('utf-8', 'replace')[:400]}") from exc
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        cells = line.split(",")
        if header is None:
            header = cells
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def measure(env: dict[str, str]) -> dict[str, object]:
    bucket = env["INFLUX_BUCKET"]
    per_bin = query(
        env,
        f'''from(bucket: "{bucket}")
  |> range(start: -{WINDOW_MIN}m)
  |> filter(fn: (r) => r._measurement == "rf_sweep" and r._field == "rssi_db")
  |> group(columns: ["frequency_hz"])
  |> median()
''',
    )
    levels = []
    for row in per_bin:
        try:
            levels.append(float(row["_value"]))
        except (KeyError, ValueError):
            continue
    samples = query(
        env,
        f'''from(bucket: "{bucket}")
  |> range(start: -{WINDOW_MIN}m)
  |> filter(fn: (r) => r._measurement == "rf_sweep")
  |> last()
  |> keep(columns: ["_time"])
''',
    )
    last_time = samples[0]["_time"] if samples else ""
    return {"bins": levels, "last_sample": last_time}


def evaluate(levels: list[float], last_sample: str, now: datetime) -> dict[str, object]:
    if len(levels) < MIN_BINS:
        return {"dead": True, "reason": f"only {len(levels)} bins in {WINDOW_MIN} min (feed not sweeping)",
                "spread_db": None, "bins": len(levels)}
    ordered = sorted(levels)
    median = ordered[len(ordered) // 2]
    peak = ordered[-1]
    spread = peak - median
    if spread < MIN_SPREAD_DB:
        return {"dead": True, "reason": f"band is flat: peak-median {spread:.1f} dB < {MIN_SPREAD_DB:.0f} dB",
                "spread_db": round(spread, 1), "bins": len(levels)}
    return {"dead": False, "reason": f"live: peak-median {spread:.1f} dB over {len(levels)} bins",
            "spread_db": round(spread, 1), "bins": len(levels)}


def publish_hass(state: dict[str, object]) -> str:
    hass = load_env(HASS_ENV)
    server = hass.get("HASS_SERVER", "").rstrip("/")
    token = hass.get("HASS_TOKEN", "")
    if not server or not token:
        return "hass credentials missing - state only"
    payload = {
        "state": "on" if state["dead"] else "off",
        "attributes": {
            "friendly_name": "LinuxGR RF feed",
            "device_class": "problem",
            "reason": state["reason"],
            "spread_db": state["spread_db"],
            "bins": state["bins"],
            "window_minutes": WINDOW_MIN,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }
    req = urllib.request.Request(
        f"{server}/api/states/{ENTITY_ID}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HASS_TIMEOUT) as resp:
        resp.read()
    return "published"


def main() -> int:
    env = load_env(ENV_PATH)
    missing = [k for k in ("INFLUX_URL", "INFLUX_ORG", "INFLUX_TOKEN", "INFLUX_BUCKET") if not env.get(k)]
    if missing:
        print(json.dumps({"error": f"missing {', '.join(missing)} in {ENV_PATH}"}))
        return 2
    now = datetime.now(timezone.utc)
    raw: dict[str, object] = {"bins": [], "last_sample": ""}
    try:
        raw = measure(env)
        state = evaluate(raw["bins"], raw["last_sample"], now)  # type: ignore[arg-type]
    except Exception as exc:  # a watchdog must never crash into silence
        state = {"dead": True, "reason": f"watchdog could not read the sweep: {exc}"[:300],
                 "spread_db": None, "bins": 0}
    state["last_sample"] = raw.get("last_sample", "")
    state["checked_at"] = now.isoformat(timespec="seconds")
    try:
        state["hass"] = publish_hass(state)
    except Exception as exc:
        state["hass"] = f"publish failed: {exc}"[:200]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))
    print(json.dumps(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
