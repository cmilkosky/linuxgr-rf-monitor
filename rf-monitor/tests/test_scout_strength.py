#!/usr/bin/env python3
"""Strength metric tests for the sweep scout.

Strength used to be the single best bin of a blob. A max over n noise bins sits ~ln(n)
above the blob's mean, so that rule handed free SNR to whichever blob spanned more
spectrum - measured on live rows 2026-10-28, wide blobs read 34.9 dB by max vs 27.0 dB by
mean while 1-2 bin signals moved 0.1 dB. Strength is now the blob's in-band average.

Run like the other suites: python3 tests/test_scout_strength.py
"""

from __future__ import annotations

import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import rf_signal_scout as scout  # noqa: E402

STAMP = "2026-10-28T18:00:00Z"
STEP = 500_000          # the sweep's bin spacing
FLOOR = -60.0           # per-bin 6 h median, and the spectrum-wide median
LEVEL = 20.0            # true in-band level above the floor, in dB


def sweep(base_hz: int, levels):
    freqs = [base_hz + i * STEP for i in range(len(levels))]
    recent = [(STAMP, f, float(v)) for f, v in zip(freqs, levels)]
    floor = {f: FLOOR for f in freqs}
    return recent, floor


def reported(levels, base_hz: int, strength_bins: int):
    """Run detect_blobs with a given rule; return peak_snr_db of the single blob."""
    recent, floor = sweep(base_hz, levels)
    saved = scout.STRENGTH_BINS
    scout.STRENGTH_BINS = strength_bins
    try:
        blobs = scout.detect_blobs(floor, recent)
    finally:
        scout.STRENGTH_BINS = saved
    return blobs[0] if blobs else None


def noise_levels(rng, n: int):
    """n bins of exponential power around LEVEL: mean in-band power = LEVEL dB."""
    return FLOOR + LEVEL + 10.0 * np.log10(rng.exponential(1.0, n))


def main() -> int:
    failures = 0
    rng = np.random.default_rng(7)
    trials = 200

    # 1. The reported level is the in-band average, so it does not depend on how many bins
    #    the blob spans: 48-bin and 4-bin blobs of the same true level agree.
    wide = np.mean([reported(noise_levels(rng, 48), int(300e6), 0)["peak_snr_db"]
                    for _ in range(trials)])
    narrow_band = np.mean([reported(noise_levels(rng, 4), int(200e6), 0)["peak_snr_db"]
                           for _ in range(trials)])
    if abs(wide - narrow_band) < 1.5:
        print(f"PASS: 48-bin and 4-bin blobs of equal level read {wide:.1f} vs "
              f"{narrow_band:.1f} dB (width-independent)")
    else:
        print(f"FAIL: width still buys strength - 48-bin {wide:.1f} vs 4-bin {narrow_band:.1f}")
        failures += 1

    # 2. The old max-bin rule overstated a wide blob by several dB; the new one does not.
    wide_max = np.mean([reported(noise_levels(rng, 48), int(300e6), 1)["peak_snr_db"]
                        for _ in range(trials)])
    if wide_max - wide >= 3.0 and abs(wide - LEVEL) < 2.0:
        print(f"PASS: 48-bin blob reads {wide:.1f} dB by average vs {wide_max:.1f} dB by max "
              f"(true in-band level {LEVEL:.0f} dB above floor)")
    else:
        print(f"FAIL: 48-bin average {wide:.1f} dB, max {wide_max:.1f} dB, true {LEVEL:.0f} dB")
        failures += 1

    # 3. A single-bin signal is unchanged - the fix must not mute narrow carriers.
    one = FLOOR + 34.0
    got = reported(np.array([one]), int(144e6), 0)
    if got and abs(got["peak_snr_db"] - 34.0) < 0.01 and got["n_bins"] == 1:
        print(f"PASS: 1-bin signal still reads {got['peak_snr_db']:.1f} dB")
    else:
        print(f"FAIL: 1-bin signal read {got and got['peak_snr_db']}")
        failures += 1

    # 4. The detail page recovers the spectrum floor as peak_rssi_db - peak_snr_db, so the
    #    pair must stay exactly consistent or that sentence states a wrong baseline.
    got = reported(noise_levels(rng, 27), int(48e6), 0)
    if got and abs((got["peak_rssi_db"] - got["peak_snr_db"]) - FLOOR) < 0.05:
        print("PASS: peak_rssi_db - peak_snr_db still recovers the spectrum floor")
    else:
        print(f"FAIL: derived floor {got and got['peak_rssi_db'] - got['peak_snr_db']:.2f} != {FLOOR}")
        failures += 1

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
