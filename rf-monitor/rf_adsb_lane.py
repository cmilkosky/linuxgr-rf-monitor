#!/usr/bin/env python3
"""RF Monitor v2 - ADS-B lane (1090 MHz).

dump1090 decodes the Mode-S/ADS-B downlink from one dedicated RTL-SDR; this
service turns what it hears into episode rows the Signals dashboard already
knows how to show (`decode_type='aircraft'`), so aircraft stop being invisible
to a spectrum-first monitor.

Design:

* One row per aircraft *sighting*, keyed on the ICAO hex. An aircraft still
  being heard inside `REVISIT_MINUTES` updates its row (last_seen, sightings,
  summary); after that gap the same hex starts a new row, because a plane that
  comes back 40 minutes later is a different observation.
* Rows are inserted with `score=0` and `decode_status='decoded'`, so the
  capture queue (`decode_status='none' AND score >= 45`) never tries to capture
  an aircraft: there is no IQ worth keeping for a packet that already decoded.
* Retention: rows from this lane older than `--retention-days` are pruned each
  pass, so a busy sky does not grow the DB without bound.

    python3 rf_adsb_lane.py --once
    python3 rf_adsb_lane.py --loop --interval 5
    python3 rf_adsb_lane.py --stats
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import signals_store as store

LOG_PREFIX = "rf_adsb_lane"

FREQ_HZ = 1_090_000_000
BAND = "1090 MHz ADS-B"
SOURCE = "adsb-lane"
REVISIT_MINUTES = 10
RETENTION_DAYS = 7

DEFAULT_JSON = Path("/home/cmilkosk/rf-monitor/signals/adsb/aircraft.json")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarise(aircraft: dict[str, Any]) -> str:
    """Operator-facing one-liner: who, how high, how fast, where heading."""
    ident = (aircraft.get("flight") or "").strip() or aircraft.get("hex") or "unknown"
    hex_id = aircraft.get("hex") or "?"
    parts = [f"{ident} ({hex_id})"]
    altitude = aircraft.get("altitude", aircraft.get("alt_baro"))
    if isinstance(altitude, (int, float)):
        parts.append(f"{int(altitude):,} ft")
    speed = aircraft.get("speed", aircraft.get("gs"))
    if isinstance(speed, (int, float)):
        parts.append(f"{int(speed)} kt")
    track = aircraft.get("track", aircraft.get("heading"))
    if isinstance(track, (int, float)):
        parts.append(f"trk {int(track):03d}°")
    if aircraft.get("squawk"):
        parts.append(f"sqwk {aircraft['squawk']}")
    lat, lon = aircraft.get("lat"), aircraft.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        parts.append(f"{lat:.3f},{lon:.3f}")
    return " · ".join(parts)


def load_aircraft(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    aircraft = payload.get("aircraft")
    return aircraft if isinstance(aircraft, list) else []


def record_sighting(aircraft: dict[str, Any], now: str, revisit_minutes: int = REVISIT_MINUTES) -> str:
    """Insert or refresh one aircraft row. Returns 'new' or 'update'."""
    hex_id = str(aircraft.get("hex") or "").strip()
    if not hex_id:
        return "skip"
    summary = summarise(aircraft)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=revisit_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    features = json.dumps(
        {k: aircraft.get(k) for k in ("hex", "flight", "altitude", "alt_baro", "speed", "gs",
                                      "track", "heading", "squawk", "category", "rssi",
                                      "lat", "lon", "messages", "seen") if k in aircraft},
        sort_keys=True,
    )[:2000]
    rssi = aircraft.get("rssi")
    peak = float(rssi) if isinstance(rssi, (int, float)) else 0.0

    with store.connect() as conn:
        row = conn.execute(
            "SELECT id FROM episodes WHERE source=? AND known_emitter=? AND last_seen >= ?"
            " ORDER BY last_seen DESC LIMIT 1",
            (SOURCE, hex_id, cutoff),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE episodes SET last_seen=?, sightings=sightings+1, decoded_summary=?,"
                " features_json=?, peak_snr_db=MAX(peak_snr_db, ?) WHERE id=?",
                (now, summary, features, peak, int(row["id"])),
            )
            conn.commit()
            return "update"
        conn.execute(
            "INSERT INTO episodes (freq_hz, low_hz, high_hz, bandwidth_hz, n_bins, peak_snr_db,"
            " mean_snr_db, first_seen, last_seen, sightings, state, class, band, likely,"
            " confidence, score, is_new, known_emitter, decode_type, decode_status,"
            " decoded_summary, features_json, source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (FREQ_HZ, FREQ_HZ - 1_000_000, FREQ_HZ + 1_000_000, 2_000_000, 1, peak, peak,
             now, now, 1, "open", "known", BAND, "ADS-B Mode-S transponder", "high",
             0.0, 1, hex_id, "aircraft", "decoded", summary, features, SOURCE, now),
        )
        conn.commit()
        return "new"


def prune(retention_days: int = RETENTION_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with store.connect() as conn:
        cur = conn.execute(
            "DELETE FROM episodes WHERE source=? AND last_seen < ?", (SOURCE, cutoff)
        )
        conn.commit()
        return cur.rowcount or 0


def pass_once(json_path: Path, *, revisit_minutes: int = REVISIT_MINUTES,
              retention_days: int = RETENTION_DAYS, prune_every: int = 60) -> dict[str, Any]:
    now = utcnow()
    aircraft = load_aircraft(json_path)
    counts = {"new": 0, "update": 0, "skip": 0}
    for entry in aircraft:
        if not isinstance(entry, dict):
            continue
        counts[record_sighting(entry, now, revisit_minutes)] += 1
    pruned = 0
    if prune_every and int(time.time()) % max(prune_every, 1) < 10:
        pruned = prune(retention_days)
    return {"utc": now, "heard": len(aircraft), **counts, "pruned": pruned}


def stats() -> dict[str, Any]:
    with store.connect() as conn:
        total = conn.execute("SELECT count(*) FROM episodes WHERE source=?", (SOURCE,)).fetchone()[0]
        recent = conn.execute(
            "SELECT count(*) FROM episodes WHERE source=? AND last_seen >= ?",
            (SOURCE, (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ).fetchone()[0]
        latest = conn.execute(
            "SELECT decoded_summary, last_seen FROM episodes WHERE source=? ORDER BY last_seen DESC LIMIT 1",
            (SOURCE,),
        ).fetchone()
    return {"rows": total, "last_hour": recent,
            "latest": dict(latest) if latest else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RF Monitor ADS-B lane")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON, help="dump1090 aircraft.json path")
    parser.add_argument("--once", action="store_true", help="one pass then exit")
    parser.add_argument("--loop", action="store_true", help="poll forever")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between passes in --loop")
    parser.add_argument("--revisit-minutes", type=int, default=REVISIT_MINUTES)
    parser.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--json-out", action="store_true", help="print each pass as JSON")
    args = parser.parse_args(argv)

    store.init_db()

    if args.stats:
        print(json.dumps(stats(), indent=2))
        return 0

    if args.loop:
        print(f"{LOG_PREFIX}: polling {args.json} every {args.interval:.0f}s", flush=True)
        while True:
            result = pass_once(args.json, revisit_minutes=args.revisit_minutes,
                               retention_days=args.retention_days)
            if args.json_out or result["new"] or result["update"]:
                print(f"{LOG_PREFIX}: {json.dumps(result)}", flush=True)
            time.sleep(max(1.0, args.interval))
        return 0

    result = pass_once(args.json, revisit_minutes=args.revisit_minutes,
                       retention_days=args.retention_days)
    print(json.dumps(result, indent=2) if args.json_out else
          f"{LOG_PREFIX}: heard={result['heard']} new={result['new']} "
          f"update={result['update']} pruned={result['pruned']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
