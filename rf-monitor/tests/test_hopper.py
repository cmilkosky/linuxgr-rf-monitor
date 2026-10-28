#!/usr/bin/env python3
"""Hopper lane tests: the unsigned-RTL trap and watchlist selection.

Run like the other suites: python3 tests/test_hopper.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import rf_hopper  # noqa: E402


def main() -> int:
    failures = 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)

        # RTL cu8 is unsigned: 0 is full-negative, 255 full-positive, 128 silence.
        # Reading it as signed (the HackRF convention) maps 128..255 to a large
        # negative block and manufactures a full-scale DC term - silence would
        # then look like a carrier. This pins the correct convention.
        path = tmp / "levels.cu8"
        path.write_bytes(bytes([0, 0] * 2048 + [255, 255] * 2048))
        iq = rf_hopper.read_rtl_iq(path)
        low, high = float(iq[:2048].real.mean()), float(iq[2048:].real.mean())
        if iq.size == 4096 and abs(low + 0.996) < 0.02 and abs(high - 0.996) < 0.02:
            print("PASS: RTL cu8 read as unsigned (0 -> -1.0, 255 -> +1.0)")
        else:
            print(f"FAIL: unsigned convention wrong (means {low:.3f} / {high:.3f})")
            failures += 1

        silence = tmp / "silence.cu8"
        silence.write_bytes(bytes([128, 128] * 4096))
        quiet = float(np.abs(rf_hopper.read_rtl_iq(silence)).mean())
        if quiet < 0.01:
            print("PASS: mid-scale bytes read as silence, not as a carrier")
        else:
            print(f"FAIL: silence measured {quiet:.3f} (fake DC)")
            failures += 1

        tiny = tmp / "tiny.cu8"
        tiny.write_bytes(bytes([128, 128] * 100))
        try:
            rf_hopper.read_rtl_iq(tiny)
            print("FAIL: a 100-sample capture was accepted")
            failures += 1
        except RuntimeError:
            print("PASS: short capture rejected instead of analysed")

    rows = [
        {"id": 1, "freq_hz": 1_090_000_000, "decoder": "adsb", "radio": "rtl-3",
         "mode": "parked", "enabled": 1},
        {"id": 2, "freq_hz": 930_000_000, "decoder": "pager", "radio": "rtl-2",
         "mode": "parked", "enabled": 1},
        {"id": 3, "freq_hz": 144_390_000, "decoder": "aprs", "radio": None,
         "mode": "parked", "enabled": 1},
        {"id": 4, "freq_hz": 162_400_000, "decoder": "noaa", "radio": "rtl-1",
         "mode": "parked", "enabled": 1},
    ]
    original = rf_hopper.rf_watchlist.list_entries
    rf_hopper.rf_watchlist.list_entries = lambda enabled_only=True: rows
    try:
        picked = [entry["id"] for entry in rf_hopper.select_entries("rtl-2")]
    finally:
        rf_hopper.rf_watchlist.list_entries = original
    # 1 is ADS-B (readsb owns it), 4 belongs to another radio, 3 has no radio yet
    # and is adopted by this lane rather than left unserved.
    if picked == [2, 3]:
        print("PASS: hopper serves its own radio plus unassigned entries, skips ADS-B")
    else:
        print(f"FAIL: selected {picked}, expected [2, 3]")
        failures += 1

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
