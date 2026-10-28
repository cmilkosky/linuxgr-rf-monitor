#!/usr/bin/env python3
"""RF Monitor v2 - watchlist: the list of frequencies worth listening to 24/7.

The loop this exists to close:

    sweep finds a signal -> capture lane decodes it -> operator looks at the row
    -> "yes, I want this regularly" -> promote -> a radio is assigned to it

Without a promote action the third tier never fills up, and every frequency worth
watching is a manual edit somewhere. So this module is deliberately small: it
stores intent, decides which radio can serve it, and says plainly when nothing
can.

Radio model (LinuxGR, 2026-10):

    hackrf-sweep      always scanning, never parked (its 10 ms/bin blind time is
                      why it is a scout and not a listener)
    hackrf-portapack  capture / wideband work
    rtl-3             parked on ADS-B 1090
    rtl-2             parked slot, currently free

A parked entry needs a radio with duty='parked'. When none is free the entry is
still recorded - intent is worth keeping - but `radio` stays NULL and the row
says why, rather than pretending a lane exists.

    python3 rf_watchlist.py --list
    python3 rf_watchlist.py --promote 1234
    python3 rf_watchlist.py --radios
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

import signals_store as store

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    freq_hz       INTEGER NOT NULL,
    label         TEXT,
    decoder       TEXT,
    mode          TEXT NOT NULL DEFAULT 'parked',
    bandwidth_hz  INTEGER NOT NULL DEFAULT 2000000,
    radio         TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    origin        TEXT,
    episode_id    INTEGER,
    notes         TEXT,
    created_at    TEXT NOT NULL,
    last_checked  TEXT,
    UNIQUE(freq_hz)
);
"""

# Duty is what a radio can actually be asked to do, not what it is called.
RADIOS: tuple[dict[str, Any], ...] = (
    {"name": "hackrf-sweep", "kind": "hackrf", "duty": "scan", "bandwidth_hz": 16_000_000},
    {"name": "hackrf-portapack", "kind": "hackrf", "duty": "capture", "bandwidth_hz": 16_000_000},
    {"name": "rtl-3", "kind": "rtl", "duty": "parked", "bandwidth_hz": 2_000_000},
    {"name": "rtl-2", "kind": "rtl", "duty": "parked", "bandwidth_hz": 2_000_000},
)

DECODER_BANDS: tuple[tuple[int, int, str, str], ...] = (
    (1_080_000_000, 1_100_000_000, "adsb", "ADS-B"),
    (144_000_000, 148_000_000, "aprs", "APRS / packet"),
    (162_000_000, 174_000_000, "weather", "weather / EAS voice"),
    (929_000_000, 932_000_000, "pager", "paging"),
    (315_000_000, 316_000_000, "ism", "315 MHz ISM"),
    (433_000_000, 435_000_000, "ism", "433.92 MHz ISM"),
    (902_000_000, 928_000_000, "ism", "915 MHz ISM"),
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema() -> None:
    with store.connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def guess_decoder(freq_hz: int) -> tuple[str, str]:
    """(decoder, human label) for a frequency - a guess the operator can override."""
    for low, high, decoder, label in DECODER_BANDS:
        if low <= int(freq_hz) <= high:
            return decoder, label
    return "auto", "unknown - classify on capture"


def _parked_radios() -> list[dict[str, Any]]:
    return [r for r in RADIOS if r["duty"] == "parked"]


def radio_status() -> list[dict[str, Any]]:
    """Every radio with whatever watchlist entry currently owns it."""
    ensure_schema()
    by_radio: dict[str, dict[str, Any]] = {}
    with store.connect() as conn:
        for row in conn.execute(
            "SELECT radio, id, freq_hz, label, decoder FROM watchlist WHERE radio IS NOT NULL AND enabled=1"
        ):
            by_radio[row["radio"]] = dict(row)
    status = []
    for radio in RADIOS:
        assigned = by_radio.get(radio["name"])
        status.append({**radio, "assigned": assigned, "free": assigned is None and radio["duty"] == "parked"})
    return status


def first_free_parked_radio() -> str | None:
    status = radio_status()
    for entry in status:
        if entry["duty"] == "parked" and entry["free"]:
            return entry["name"]
    return None


def entry_for_freq(freq_hz: int, tolerance_hz: int = 25_000) -> dict[str, Any] | None:
    """The watchlist entry covering this frequency, if any."""
    ensure_schema()
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM watchlist WHERE ABS(freq_hz - ?) <= ? ORDER BY ABS(freq_hz - ?) LIMIT 1",
            (int(freq_hz), int(tolerance_hz), int(freq_hz)),
        ).fetchone()
    return dict(row) if row else None


def get_entry(entry_id: int) -> dict[str, Any] | None:
    ensure_schema()
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM watchlist WHERE id=?", (int(entry_id),)).fetchone()
    return dict(row) if row else None


def toggle_entry(entry_id: int) -> dict[str, Any] | None:
    entry = get_entry(entry_id)
    if entry is None:
        return None
    set_enabled(entry_id, not bool(entry["enabled"]))
    return get_entry(entry_id)


def list_entries(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    ensure_schema()
    query = "SELECT * FROM watchlist"
    if enabled_only:
        query += " WHERE enabled=1"
    query += " ORDER BY enabled DESC, freq_hz"
    with store.connect() as conn:
        return [dict(row) for row in conn.execute(query)]


def add_entry(freq_hz: int, *, label: str | None = None, decoder: str | None = None,
              mode: str = "parked", bandwidth_hz: int = 2_000_000, radio: str | None = None,
              origin: str = "manual", episode_id: int | None = None,
              notes: str | None = None) -> dict[str, Any]:
    """Record a frequency worth watching. Idempotent on frequency."""
    ensure_schema()
    guess, guess_label = guess_decoder(int(freq_hz))
    decoder = decoder or guess
    label = label or f"{int(freq_hz) / 1e6:.3f} MHz {guess_label}"
    if mode == "parked" and radio is None:
        radio = first_free_parked_radio()
        if radio is None:
            notes = ((notes + " · ") if notes else "") + "no free parked radio - recorded, not yet served"
    now = utcnow()
    with store.connect() as conn:
        try:
            conn.execute(
                "INSERT INTO watchlist (freq_hz, label, decoder, mode, bandwidth_hz, radio, enabled,"
                " origin, episode_id, notes, created_at) VALUES (?,?,?,?,?,?,1,?,?,?,?)",
                (int(freq_hz), label, decoder, mode, int(bandwidth_hz), radio, origin,
                 int(episode_id) if episode_id else None, notes, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            row = conn.execute("SELECT * FROM watchlist WHERE freq_hz=?", (int(freq_hz),)).fetchone()
            return dict(row) if row else {}
        row = conn.execute("SELECT * FROM watchlist WHERE freq_hz=?", (int(freq_hz),)).fetchone()
    return dict(row) if row else {}


def promote_episode(episode_id: int) -> dict[str, Any]:
    """Promote an episode row into the watchlist (the operator's 'yes, watch this')."""
    with store.connect() as conn:
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (int(episode_id),)).fetchone()
    if episode is None:
        raise LookupError(f"no episode {episode_id}")
    episode = dict(episode)
    return add_entry(
        int(episode["freq_hz"]),
        decoder=None,
        episode_id=int(episode_id),
        origin=f"promoted from episode {episode_id}",
        notes=(episode.get("decoded_summary") or episode.get("likely") or None),
    )


def set_enabled(entry_id: int, enabled: bool) -> None:
    ensure_schema()
    with store.connect() as conn:
        conn.execute("UPDATE watchlist SET enabled=? WHERE id=?", (1 if enabled else 0, int(entry_id)))
        conn.commit()


def remove_entry(entry_id: int) -> None:
    ensure_schema()
    with store.connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE id=?", (int(entry_id),))
        conn.commit()


def seed_defaults() -> None:
    """ADS-B is a real running lane, so it belongs in the list from the start."""
    ensure_schema()
    with store.connect() as conn:
        exists = conn.execute("SELECT 1 FROM watchlist WHERE freq_hz=?", (1_090_000_000,)).fetchone()
    if not exists:
        add_entry(1_090_000_000, label="ADS-B 1090 MHz", decoder="adsb", radio="rtl-3",
                  origin="seeded: rf-adsb-lane", notes="served by rf-adsb-dump1090 + rf-adsb-lane")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RF Monitor watchlist")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--radios", action="store_true")
    parser.add_argument("--promote", type=int, metavar="EPISODE_ID")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args(argv)

    store.init_db()
    if args.seed:
        seed_defaults()
        print("seeded")
        return 0
    if args.promote is not None:
        print(json.dumps(promote_episode(args.promote), indent=2))
        return 0
    if args.radios:
        print(json.dumps(radio_status(), indent=2))
        return 0
    entries = list_entries()
    if not entries:
        print("watchlist is empty")
        return 0
    for e in entries:
        print("%3d  %10.3f MHz  %-10s %-8s radio=%-15s enabled=%d  %s" % (
            e["id"], e["freq_hz"] / 1e6, e["decoder"], e["mode"], e["radio"] or "-", e["enabled"], e["label"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
