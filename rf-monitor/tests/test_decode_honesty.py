#!/usr/bin/env python3
"""Decode honesty: a row must not call a negative result "pending".

Regression guard for the bug where `decode_status` stayed 'captured' after every
decoder had run and found nothing, so the Signals table showed
"sample captured · decoder pending" for rows that had already been tried - and for
rows whose audio had been evicted by the artifact cap and could never be tried.

Run like the other suites: python3 tests/test_decode_honesty.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import signals_store as store  # noqa: E402
import signals_web as web  # noqa: E402
import rf_decode  # noqa: E402

_tmp = pathlib.Path(tempfile.mkdtemp())
store.SIGNALS_DIR = _tmp
store.DB_PATH = _tmp / "test-signals.db"
store.ARTIFACT_DIR = _tmp / "artifacts"
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


def captured(freq_mhz: float, status: str = "captured") -> int:
    episode_id = int(store.upsert_blob(blob(freq_mhz), 1_500_000)["id"])
    with store.connect() as conn:
        conn.execute(
            "UPDATE episodes SET capture_id=?, decode_status=? WHERE id=?",
            (f"20261028T000000Z-{int(freq_mhz * 1e6)}", status, episode_id),
        )
        conn.commit()
    return episode_id


failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(f"{label}: {detail}")


# --- an episode every decoder has already failed on ------------------------- #
tried_id = captured(31.0)
store.record_attempt(tried_id, "multimon-ng/am", "no_frame", "0 frame(s) in 0.1s")
store.record_attempt(tried_id, "multimon-ng/nfm", "no_frame", "0 frame(s) in 0.1s")
store.set_decode_status(tried_id, "no_frame")

# --- an episode nobody has tried yet ---------------------------------------- #
pending_id = captured(44.0)

# --- an episode whose audio is gone ----------------------------------------- #
gone_id = captured(46.0, status="unavailable")

rows = {int(r["id"]): r for r in store.query_episodes(limit=50)}

check("attempt_count is exposed to the page", int(rows[tried_id]["attempt_count"]) == 2,
      f"got {rows[tried_id].get('attempt_count')}")
check("attempt decoders are exposed", "multimon-ng/nfm" in (rows[tried_id]["attempt_decoders"] or ""),
      f"got {rows[tried_id].get('attempt_decoders')}")
check("untried row has no attempts", int(rows[pending_id]["attempt_count"]) == 0,
      f"got {rows[pending_id].get('attempt_count')}")

tried_text = web.decode_text(rows[tried_id])
pending_text = web.decode_text(rows[pending_id])
gone_text = web.decode_text(rows[gone_id])

check("tried row says no frame", "no frame" in tried_text and "pending" not in tried_text, tried_text)
check("untried row still says pending", "decoder pending" in pending_text, pending_text)
check("purged row is not pending", "pending" not in gone_text and "undecodable" in gone_text, gone_text)

# --- backfill must not consider an undecodable row ------------------------- #
pending_ids = {int(e["id"]) for e in rf_decode.pending_episodes()}
check("tried row is not backfill-pending", tried_id not in pending_ids, str(sorted(pending_ids)))
check("untried row is backfill-pending", pending_id in pending_ids, str(sorted(pending_ids)))
check("purged row is not backfill-pending", gone_id not in pending_ids, str(sorted(pending_ids)))

# --- a decode run that finds nothing must record no_frame, not captured ---- #
silent_id = captured(162.55)
store.set_decode_status(silent_id, "captured")
rf_decode.decode_episode({"id": silent_id, "freq_hz": 162_550_000}, artifacts=[])
with store.connect() as conn:
    status = conn.execute("SELECT decode_status FROM episodes WHERE id=?", (silent_id,)).fetchone()[0]
check("no decoders run => status untouched", status == "captured", f"got {status}")

if failures:
    print("FAIL")
    for failure in failures:
        print("  -", failure)
    raise SystemExit(1)
print("OK decode honesty")
