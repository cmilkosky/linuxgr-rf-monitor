"""Corrected capture path: int8 + DC block + LO offset. Pick the gain setting.

Target 101.5 MHz (strongest local FM carrier); tune 101.75 MHz so the station
sits 250 kHz below centre, away from the DC spike.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/cmilkosk/rf-monitor")
import rf_v2_capture as capture
from rf_signal_intel import extract_iq_features

TARGET = 101_500_000
OFFSET = 250_000
TUNE = TARGET + OFFSET
SR = 2_000_000
OUT = Path("/tmp/rf-v2-final")
OUT.mkdir(parents=True, exist_ok=True)

print(f"{'lna/vga':>8} {'p99|iq|':>7} {'rms_dBFS':>8} {'floor':>7} {'pk':>6} {'@kHz':>7} "
      f"{'sig@-250k':>9} {'snr':>5} {'bw_kHz':>7} traits")
for lna, vga in [(8, 8), (16, 16), (24, 16), (32, 24), (40, 32)]:
    path = OUT / f"cap-{lna}-{vga}.iq"
    capture.capture_hackrf(TUNE, path, duration_seconds=0.5, sample_rate_hz=SR,
                           lna_gain_db=lna, vga_gain_db=vga, amp_enable=0)
    iq = capture.remove_dc(capture.read_iq(path))
    p99 = float(np.percentile(np.abs(iq), 99))
    rms = 20 * np.log10(np.sqrt(np.mean(np.abs(iq) ** 2)) + 1e-12)

    nfft = 4096
    window = np.hanning(nfft).astype(np.float32)
    acc = np.zeros(nfft, dtype=np.float64)
    count = 0
    for start in range(0, iq.size - nfft, nfft // 2):
        acc += np.abs(np.fft.fftshift(np.fft.fft(iq[start:start + nfft] * window))) ** 2
        count += 1
    db = 10 * np.log10(np.maximum(acc / max(1, count), 1e-18))
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / SR))
    floor = float(np.percentile(db, 20))
    peak_i = int(np.argmax(db))
    sig_i = int(np.argmin(np.abs(freqs + OFFSET)))
    feats = extract_iq_features(iq, SR, TUNE)
    print(f"{lna:2d}/{vga:2d}  {p99:7.3f} {rms:8.1f} {floor:7.1f} {db[peak_i]:6.1f} "
          f"{freqs[peak_i]/1e3:+7.0f} {db[sig_i]:9.1f} {db[sig_i]-floor:5.1f} "
          f"{feats['occupied_bandwidth_hz']/1e3:7.0f} {','.join(feats['traits'])}")
