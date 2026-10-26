"""Pick capture gains: measure clipping + features at several gain pairs.

Captures 1 s at 101.8 MHz (300 kHz above the strongest local FM carrier at
101.5 MHz, so the station is off the DC spike), then reports clipping, RMS and
the feature estimate on DC-removed IQ.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/cmilkosk/rf-monitor")
import rf_v2_capture as capture
from rf_signal_intel import extract_iq_features

OUT = Path("/tmp/rf-v2-gain")
OUT.mkdir(parents=True, exist_ok=True)
TUNE = 101_800_000
SR = 2_000_000

print(f"{'lna/vga':>10} {'clip%':>7} {'rms_dBFS':>9} {'p99|iq|':>8} {'bw_kHz':>8} {'peak_MHz':>10} {'traits'}")
for lna, vga in [(32, 24), (24, 16), (16, 16), (8, 16), (8, 8), (0, 8)]:
    path = OUT / f"cap-{lna}-{vga}.iq"
    try:
        capture.capture_hackrf(TUNE, path, duration_seconds=1, sample_rate_hz=SR,
                               lna_gain_db=lna, vga_gain_db=vga, amp_enable=0)
    except Exception as exc:
        print(f"{lna:2d}/{vga:2d}      capture failed: {exc}")
        continue
    raw = np.fromfile(path, dtype=np.uint8)
    clip = float(np.mean((raw == 0) | (raw == 255)))
    iq = capture.read_iq(path)
    rms = 20 * np.log10(np.sqrt(np.mean(np.abs(iq.astype(np.float64)) ** 2)) + 1e-12)
    p99 = float(np.percentile(np.abs(iq), 99))
    clean = iq - iq.mean()
    feats = extract_iq_features(clean, SR, TUNE)
    print(f"{lna:2d}/{vga:2d}   {clip*100:6.2f}% {rms:9.1f} {p99:8.3f} "
          f"{feats['occupied_bandwidth_hz']/1e3:8.0f} {feats['peak_frequency_hz']/1e6:10.6f} "
          f"{','.join(feats['traits'])}")
