"""Calibrate the scout's detection threshold against the live spectrum.

Prints the distribution of the two statistics the scout uses, how many blobs
each threshold would produce, and sanity-checks the time axis / duty cycle.
"""

import sys

import numpy as np

sys.path.insert(0, "/home/cmilkosk/rf-monitor")
import rf_signal_scout as scout

env = scout.load_env()
floor = scout.fetch_floor(env)
recent = scout.fetch_recent(env)
print(f"floor bins={len(floor)}  recent rows={len(recent)}")

freqs = np.array(sorted({f for _t, f, _v in recent}), dtype=np.int64)
times = sorted({t for t, _f, _v in recent})
print(f"distinct freqs={freqs.size}  distinct timestamps={len(times)}")
print(f"first 3 timestamps: {times[:3]}")
print(f"rows per timestamp: {len(recent)/max(1,len(times)):.0f}")

f_index = {int(f): i for i, f in enumerate(freqs)}
t_index = {t: i for i, t in enumerate(times)}
matrix = np.full((freqs.size, len(times)), np.nan, dtype=np.float32)
for stamp, freq, value in recent:
    matrix[f_index[freq], t_index[stamp]] = value
print(f"matrix shape={matrix.shape}")

floor_arr = np.array([floor.get(int(f), np.nan) for f in freqs], dtype=np.float32)
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore", RuntimeWarning)
    level = np.nanpercentile(matrix, 75, axis=1) - floor_arr
    peak = np.nanmax(matrix, axis=1) - floor_arr
    coverage = np.sum(np.isfinite(matrix), axis=1)

for name, arr in (("level_snr(p75-median)", level), ("peak_snr(max-median)", peak)):
    a = arr[np.isfinite(arr)]
    print(f"{name}: p50={np.percentile(a,50):.2f} p75={np.percentile(a,75):.2f} "
          f"p90={np.percentile(a,90):.2f} p95={np.percentile(a,95):.2f} "
          f"p99={np.percentile(a,99):.2f} max={a.max():.2f}")
print(f"bins with full coverage ({len(times)} samples): {int(np.sum(coverage == len(times)))}")

print("\nblobs per threshold (level only):")
saved_burst = scout.BURST_SNR_MIN
scout.BURST_SNR_MIN = 999.0
for threshold in (6, 8, 9, 10, 11, 12, 14, 16):
    blobs = scout.detect_blobs(floor, recent, snr_min=threshold)
    top = ", ".join(f"{b['freq_hz']/1e6:.0f}({b['peak_snr_db']:.0f})" for b in blobs[:5])
    print(f"  level>={threshold:2d} dB -> {len(blobs):3d} blobs   top: {top}")

print("\nblobs from burst rule only (peak>=X, level<9):")
for threshold in (12, 15, 18, 20, 22):
    scout.BURST_SNR_MIN = threshold
    blobs = scout.detect_blobs(floor, recent, snr_min=99.0)
    print(f"  peak>={threshold:2d} dB -> {len(blobs):3d} blobs")
scout.BURST_SNR_MIN = saved_burst
