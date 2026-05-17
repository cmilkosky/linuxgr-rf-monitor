#!/usr/bin/env python3
"""Collect HackRF sweep bins and write them to InfluxDB line protocol."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable

import requests


LOG = logging.getLogger("hackrf_influx_collector")
STOP = False


@dataclass(frozen=True)
class SweepBin:
    timestamp_ns: int
    frequency_hz: int
    start_hz: int
    stop_hz: int
    bin_width_hz: int
    samples: int
    rssi_db: float


def handle_signal(signum: int, _frame: object) -> None:
    global STOP
    LOG.info("received signal %s, stopping after current pass", signum)
    STOP = True


def parse_timestamp(date_s: str, time_s: str) -> int:
    parsed = dt.datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S.%f")
    return int(parsed.timestamp() * 1_000_000_000)


def parse_sweep_line(line: str) -> list[SweepBin]:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 7:
        raise ValueError("not enough CSV fields")

    timestamp_ns = parse_timestamp(parts[0], parts[1])
    start_hz = int(parts[2])
    stop_hz = int(parts[3])
    bin_width_hz = int(float(parts[4]))
    samples = int(parts[5])
    rssi_values = [float(value) for value in parts[6:] if value]

    if not rssi_values:
        raise ValueError("row has no RSSI values")

    expected_by_row_span = max(1, round((stop_hz - start_hz) / bin_width_hz))
    if expected_by_row_span != len(rssi_values):
        LOG.warning(
            "row span/bin width implies %s bins but row contains %s RSSI values: %s",
            expected_by_row_span,
            len(rssi_values),
            line[:240],
        )

    bins: list[SweepBin] = []
    for index, rssi_db in enumerate(rssi_values):
        frequency_hz = int(start_hz + (index * bin_width_hz) + (bin_width_hz / 2))
        bins.append(
            SweepBin(
                timestamp_ns=timestamp_ns,
                frequency_hz=frequency_hz,
                start_hz=start_hz,
                stop_hz=stop_hz,
                bin_width_hz=bin_width_hz,
                samples=samples,
                rssi_db=rssi_db,
            )
        )
    return bins


def escape_tag(value: object) -> str:
    return str(value).replace(" ", r"\ ").replace(",", r"\,").replace("=", r"\=")


def to_line_protocol(measurement: str, source: str, sweep_bin: SweepBin) -> str:
    tags = {
        "source": source,
        "frequency_hz": sweep_bin.frequency_hz,
        "bin_width_hz": sweep_bin.bin_width_hz,
    }
    tag_s = ",".join(f"{key}={escape_tag(value)}" for key, value in tags.items())
    fields = (
        f"rssi_db={sweep_bin.rssi_db},"
        f"start_hz={sweep_bin.start_hz}i,"
        f"stop_hz={sweep_bin.stop_hz}i,"
        f"samples={sweep_bin.samples}i"
    )
    return f"{escape_tag(measurement)},{tag_s} {fields} {sweep_bin.timestamp_ns}"


def run_sweep(command: list[str]) -> Iterable[SweepBin]:
    LOG.info("running: %s", shlex.join(command))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None

    raw_logged = 0
    rows = 0
    bins = 0
    try:
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            if raw_logged < 5:
                LOG.info("hackrf_sweep: %s", line[:500])
                raw_logged += 1
            if not line[:4].isdigit():
                continue
            try:
                parsed_bins = parse_sweep_line(line)
            except Exception as exc:
                LOG.warning("could not parse sweep row: %s; row=%s", exc, line[:500])
                continue
            rows += 1
            bins += len(parsed_bins)
            yield from parsed_bins
    finally:
        return_code = process.wait()
        LOG.info("sweep finished rc=%s rows=%s bins=%s", return_code, rows, bins)
        if return_code not in (0, 124):
            raise RuntimeError(f"hackrf_sweep exited with {return_code}")


def write_influx_v2(lines: list[str], timeout: float) -> None:
    url = os.environ["INFLUX_URL"].rstrip("/")
    org = os.environ["INFLUX_ORG"]
    bucket = os.environ["INFLUX_BUCKET"]
    token = os.environ["INFLUX_TOKEN"]
    endpoint = f"{url}/api/v2/write"
    response = requests.post(
        endpoint,
        params={"org": org, "bucket": bucket, "precision": "ns"},
        headers={"Authorization": f"Token {token}", "Content-Type": "text/plain"},
        data="\n".join(lines),
        timeout=timeout,
    )
    response.raise_for_status()


def write_influx_v1(lines: list[str], timeout: float) -> None:
    url = os.environ["INFLUX_URL"].rstrip("/")
    db = os.environ["INFLUX_DB"]
    auth = None
    if os.environ.get("INFLUX_USER"):
        auth = (os.environ["INFLUX_USER"], os.environ.get("INFLUX_PASSWORD", ""))
    response = requests.post(
        f"{url}/write",
        params={"db": db, "precision": "ns"},
        auth=auth,
        data="\n".join(lines),
        timeout=timeout,
    )
    response.raise_for_status()


def flush(lines: list[str], dry_run: bool, timeout: float) -> None:
    if not lines:
        return
    if dry_run:
        LOG.info("dry-run flush: %s points", len(lines))
        for line in lines[:5]:
            LOG.info("line: %s", line)
        return
    if os.environ.get("INFLUX_BUCKET"):
        write_influx_v2(lines, timeout)
    elif os.environ.get("INFLUX_DB"):
        write_influx_v1(lines, timeout)
    else:
        raise RuntimeError("set either INFLUX_BUCKET/ORG/TOKEN or INFLUX_DB")
    LOG.info("wrote %s points to influx", len(lines))


def build_command(args: argparse.Namespace) -> list[str]:
    command = [args.hackrf_sweep]
    command.extend(shlex.split(args.sweep_args))
    if "-1" not in command:
        command.append("-1")
    return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hackrf-sweep", default=os.environ.get("HACKRF_SWEEP", "hackrf_sweep"))
    parser.add_argument(
        "--sweep-args",
        default=os.environ.get("HACKRF_SWEEP_ARGS", "-f 400:6000 -w 1000000"),
        help="arguments passed to hackrf_sweep; -1 is added automatically",
    )
    parser.add_argument("--interval", type=float, default=float(os.environ.get("SWEEP_INTERVAL", "60")))
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("INFLUX_BATCH_SIZE", "5000")))
    parser.add_argument("--measurement", default=os.environ.get("INFLUX_MEASUREMENT", "rf_sweep"))
    parser.add_argument("--source", default=os.environ.get("RF_SOURCE", "hackrf_linuxgr"))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("INFLUX_TIMEOUT", "10")))
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN") == "1")
    parser.add_argument("--once", action="store_true", help="run one sweep pass and exit")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    command = build_command(args)
    while not STOP:
        started = time.monotonic()
        batch: list[str] = []
        for sweep_bin in run_sweep(command):
            batch.append(to_line_protocol(args.measurement, args.source, sweep_bin))
            if len(batch) >= args.batch_size:
                flush(batch, args.dry_run, args.timeout)
                batch.clear()
        flush(batch, args.dry_run, args.timeout)
        if args.once:
            break
        sleep_for = max(0.0, args.interval - (time.monotonic() - started))
        LOG.info("sleeping %.1fs", sleep_for)
        time.sleep(sleep_for)
    return 0


if __name__ == "__main__":
    sys.exit(main())
