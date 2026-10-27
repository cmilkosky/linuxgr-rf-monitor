#!/usr/bin/env python3
"""Roll raw rf_sweep data up into a compact max-hold measurement.

Why this exists: the 7-day heatmap cannot be served from raw sweep data. A 168 h
range is 59.9M points / 3.8 GB, and every Flux-side reduction measured slower
than the raw transfer on this hardware:

    raw 168h                    88.2 s   3.79 GB
    aggregateWindow 168h       161.5 s   63 MB
    decimate 10min + 5 MHz     252.8 s   75 MB

So the long-range view reads a precomputed rollup instead: one point per
(frequency bin, window) holding the max RSSI seen in that window. 7 days at
5 MHz / 10 min is 1.2M points, which reads in seconds.

Usage:
    rf_rollup.py                     # roll up the most recent window
    rf_rollup.py --backfill-hours 168
    rf_rollup.py --dry-run           # report what would be written
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import sys
import time

import requests

URL = os.environ["INFLUX_URL"].rstrip("/")
ORG = os.environ["INFLUX_ORG"]
TOKEN = os.environ["INFLUX_TOKEN"]
BUCKET = os.environ["INFLUX_BUCKET"]
MEASUREMENT = os.environ.get("RF_ROLLUP_MEASUREMENT", "rf_rollup")
BIN_HZ = int(os.environ.get("RF_ROLLUP_BIN_HZ", "5000000"))

HEADERS = {"Authorization": f"Token {TOKEN}"}


def fetch_window(start: dt.datetime, stop: dt.datetime) -> list[dict[str, str]]:
    query = f'''from(bucket: "{BUCKET}")
  |> range(start: {start.strftime("%Y-%m-%dT%H:%M:%SZ")}, stop: {stop.strftime("%Y-%m-%dT%H:%M:%SZ")})
  |> filter(fn: (r) => r._measurement == "rf_sweep" and r._field == "rssi_db")
  |> keep(columns: ["_time", "_value", "frequency_hz"])'''
    response = requests.post(
        f"{URL}/api/v2/query",
        params={"org": ORG},
        headers={**HEADERS, "Accept": "application/csv", "Content-Type": "application/vnd.flux"},
        data=query,
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"query failed {response.status_code}: {response.text[:200]}")
    return list(csv.DictReader(io.StringIO(response.text)))


def rollup_window(start: dt.datetime, stop: dt.datetime) -> list[str]:
    rows = fetch_window(start, stop)
    best: dict[int, float] = {}
    for row in rows:
        try:
            bin_hz = int(row["frequency_hz"]) // BIN_HZ * BIN_HZ
            value = float(row["_value"])
        except (KeyError, TypeError, ValueError):
            continue
        if bin_hz not in best or value > best[bin_hz]:
            best[bin_hz] = value
    stamp_ns = int(start.timestamp()) * 1_000_000_000
    return [
        f"{MEASUREMENT},freq_bin={bin_hz} rssi_db={value:.1f} {stamp_ns}"
        for bin_hz, value in best.items()
    ]


def write_lines(lines: list[str]) -> None:
    if not lines:
        return
    response = requests.post(
        f"{URL}/api/v2/write",
        params={"org": ORG, "bucket": BUCKET, "precision": "ns"},
        headers={**HEADERS, "Content-Type": "text/plain; charset=utf-8"},
        data="\n".join(lines),
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"write failed {response.status_code}: {response.text[:300]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-minutes", type=int, default=10)
    parser.add_argument("--backfill-hours", type=float, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    window = args.window_minutes * 60
    now = dt.datetime.now(dt.timezone.utc)
    aligned_now = dt.datetime.fromtimestamp(now.timestamp() // window * window, dt.timezone.utc)

    if args.backfill_hours:
        start = aligned_now - dt.timedelta(hours=args.backfill_hours)
    else:
        start = aligned_now - dt.timedelta(seconds=window)

    written = 0
    windows = 0
    cursor = start
    while cursor < aligned_now:
        stop = min(cursor + dt.timedelta(seconds=window), aligned_now)
        try:
            lines = rollup_window(cursor, stop)
        except Exception as exc:  # keep the loop alive; report at the end
            print(f"window {cursor:%Y-%m-%dT%H:%MZ} failed: {exc}", file=sys.stderr)
            cursor = stop
            continue
        windows += 1
        if lines and not args.dry_run:
            write_lines(lines)
        written += len(lines)
        if windows % 24 == 0:
            print(f"  {windows} windows, {written} points, at {cursor:%Y-%m-%dT%H:%MZ}", flush=True)
        cursor = stop

    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {written} points across {windows} window(s) at {BIN_HZ/1e6:.0f} MHz bins", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
