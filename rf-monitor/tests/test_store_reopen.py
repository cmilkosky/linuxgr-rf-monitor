#!/usr/bin/env python3
"""Episode identity tests: one row per emitter, not one per 15-minute window.

Run like the other suites: python3 tests/test_store_reopen.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import signals_store as store  # noqa: E402

store.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "test-signals.db"
store.init_db()


def blob(freq_mhz: float, snr: float = 12.0, width_hz: int = 100_000) -> dict:
    freq = int(freq_mhz * 1e6)
    return {
        "freq_hz": freq, "low_hz": freq - width_hz // 2, "high_hz": freq + width_hz // 2,
        "bandwidth_hz": width_hz, "n_bins": 2, "peak_snr_db": snr, "mean_snr_db": snr - 3,
        "delta_db": 0.0, "peak_rssi_db": snr, "floor_db": 0.0, "duty": 1.0, "score": 50.0,
        "band": None, "likely": None, "confidence": None, "class": "unexplained",
        "known_emitter": None, "features_json": None,
    }


def main() -> int:
    failures = 0

    # 1. First sighting creates a row.
    first = store.upsert_blob(blob(144.390), 1_500_000, 15)
    if first["created"]:
        print("PASS: first sighting creates one episode")
    else:
        print("FAIL: first sighting did not create an episode")
        failures += 1

    # 2. After the stale close, the same emitter must REOPEN that row, not mint a new one.
    store.close_stale(0)
    reopened = store.upsert_blob(blob(144.390), 1_500_000, 15)
    if not reopened["created"] and reopened["id"] == first["id"]:
        print("PASS: a closed episode is reopened, not duplicated")
    else:
        print(f"FAIL: reopen gave created={reopened['created']} id={reopened['id']} vs {first['id']}")
        failures += 1

    # 3. Reopening must flip the row back to open and count the sighting.
    row = store.get_episode(first["id"])
    if row and row["state"] == "open" and row["sightings"] >= 2:
        print("PASS: reopened episode is open again with sightings incremented")
    else:
        print(f"FAIL: reopened row state={row and row['state']} sightings={row and row['sightings']}")
        failures += 1

    # 4. A genuinely different emitter keeps its own row.
    other = store.upsert_blob(blob(162.550), 1_500_000, 15)
    if other["created"] and other["id"] != first["id"]:
        print("PASS: a different emitter gets its own episode")
    else:
        print(f"FAIL: distinct emitter created={other['created']} id={other['id']}")
        failures += 1

    # 5. Outside the reopen window the row is left alone and a new episode is made.
    with store.connect() as conn:
        conn.execute("UPDATE episodes SET last_seen='2020-01-01T00:00:00+00:00', state='closed'"
                     " WHERE id=?", (int(first["id"]),))
        conn.commit()
    late = store.upsert_blob(blob(144.390), 1_500_000, 15)
    if late["created"] and late["id"] != first["id"]:
        print("PASS: beyond the reopen window a fresh episode is created")
    else:
        print(f"FAIL: old episode gave created={late['created']} id={late['id']}")
        failures += 1

    # 6. The stored span follows the current blob, so a row can shrink again.
    wide = store.upsert_blob(blob(50.0, width_hz=4_000_000), 1_500_000, 15)
    narrower = store.upsert_blob(blob(50.0, width_hz=3_000_000), 1_500_000, 15)
    row = store.get_episode(wide["id"])
    if not narrower["created"] and row and row["bandwidth_hz"] == 3_000_000:
        print("PASS: span tracks the current blob instead of only ever growing")
    else:
        print(f"FAIL: shrink gave created={narrower['created']} bw={row and row['bandwidth_hz']}")
        failures += 1

    # 7. A narrow signal inside a wide band is a different emitter, not part of the band.
    band = store.upsert_blob(blob(50.0, width_hz=30_000_000), 1_500_000, 15)
    narrow = store.upsert_blob(blob(50.0, width_hz=1_000_000), 1_500_000, 15)
    if narrow["created"] and narrow["id"] != band["id"]:
        print("PASS: a 1 MHz signal inside a 30 MHz band is not absorbed into it")
    else:
        print(f"FAIL: narrow blob created={narrow['created']} id={narrow['id']} vs band {band['id']}")
        failures += 1

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
