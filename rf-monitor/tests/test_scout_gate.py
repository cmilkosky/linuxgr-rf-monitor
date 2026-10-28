#!/usr/bin/env python3
"""Scout persistence gate tests.

Run like the other suites: python3 tests/test_scout_gate.py
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import rf_signal_scout as scout  # noqa: E402


def blob(freq_mhz: float, snr: float = 10.0) -> dict:
    freq = int(freq_mhz * 1e6)
    return {"freq_hz": freq, "low_hz": freq - 50_000, "high_hz": freq + 50_000,
            "peak_snr_db": snr, "mean_snr_db": snr - 3, "peak_rssi_db": snr,
            "floor_db": 0.0, "duty": 1.0, "n_bins": 2, "bandwidth_hz": 100_000,
            "score": 50.0}


def main() -> int:
    failures = 0
    now = 1_700_000_000.0

    # 1. A single sweep is not evidence: the first sighting is held, not emitted.
    confirmed, pending = scout.confirm_blobs([blob(144.390)], [], now=now)
    if not confirmed and len(pending) == 1:
        print("PASS: first sighting is held, not turned into an episode")
    else:
        print(f"FAIL: single sweep emitted {len(confirmed)} episode(s)")
        failures += 1

    # 2. Seen again next sweep -> confirmed.
    confirmed, pending = scout.confirm_blobs([blob(144.390)], pending, now=now + 60)
    if len(confirmed) == 1 and not pending:
        print("PASS: second consecutive sweep confirms the blob")
    else:
        print(f"FAIL: repeat sighting gave confirmed={len(confirmed)} pending={len(pending)}")
        failures += 1

    # 3. A strong burst needs only one sweep - strength is its own evidence.
    confirmed, pending = scout.confirm_blobs([blob(162.550, snr=30.0)], [], now=now)
    if len(confirmed) == 1 and not pending:
        print("PASS: 30 dB burst emits immediately, bypassing the persistence window")
    else:
        print(f"FAIL: burst gave confirmed={len(confirmed)} pending={len(pending)}")
        failures += 1

    # 4. A stale pending entry must not confirm a much later sighting.
    stale = [{"freq_hz": int(144.390e6), "low_hz": 0, "high_hz": 0, "peak_snr_db": 10.0,
              "sweeps": 1, "last_seen_epoch": now - scout.PENDING_MAX_AGE_S - 60}]
    confirmed, pending = scout.confirm_blobs([blob(144.390)], stale, now=now)
    if not confirmed and len(pending) == 1:
        print("PASS: expired pending entry does not confirm a later sighting")
    else:
        print(f"FAIL: stale entry gave confirmed={len(confirmed)}")
        failures += 1

    # 5. A different frequency in the previous sweep must not confirm this one.
    other = [{"freq_hz": int(100.0e6), "low_hz": 0, "high_hz": 0, "peak_snr_db": 10.0,
              "sweeps": 1, "last_seen_epoch": now}]
    confirmed, pending = scout.confirm_blobs([blob(144.390)], other, now=now)
    if not confirmed:
        print("PASS: a different frequency does not confirm this one")
    else:
        print("FAIL: unrelated frequency confirmed the blob")
        failures += 1

    # 6. Nearby blobs must not be double-counted, and an unmatched pending entry expires.
    two = [blob(144.390), blob(144.400)]
    confirmed, pending = scout.confirm_blobs(two, [], now=now)
    confirmed2, pending2 = scout.confirm_blobs([blob(144.395)], pending, now=now + 60)
    if len(confirmed2) == 1 and not pending2:
        print("PASS: pending entries are consumed once; unmatched ones expire")
    else:
        print(f"FAIL: consumption gave confirmed={len(confirmed2)} pending={len(pending2)}")
        failures += 1

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
