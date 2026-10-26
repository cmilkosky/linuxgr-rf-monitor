"""Offline look at the stored 101.8 MHz capture: is the HackRF front end clipping?"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/cmilkosk/rf-monitor")
import rf_v2_capture as capture

IQ = Path("/tmp/rf-v2-diag/capture.iq")
raw = np.fromfile(IQ, dtype=np.uint8)
print(f"bytes={raw.size}  min={raw.min()}  max={raw.max()}")
print(f"fraction at 0   = {np.mean(raw == 0):.5f}")
print(f"fraction at 255 = {np.mean(raw == 255):.5f}")
print(f"fraction <=2    = {np.mean(raw <= 2):.5f}")
print(f"fraction >=253  = {np.mean(raw >= 253):.5f}")

iq = capture.read_iq(IQ)
print(f"|iq| p50={np.percentile(np.abs(iq),50):.3f} p99={np.percentile(np.abs(iq),99):.3f} max={np.abs(iq).max():.3f}")
print(f"mean={iq.mean():.4f}  |mean|/p50 = {abs(iq.mean())/np.percentile(np.abs(iq),50):.1f}")

clean = iq - iq.mean()
sr = 2_000_000
nfft = 4096
chunk = clean[:nfft * 400]
win = np.hanning(nfft).astype(np.float32)
starts = np.arange(0, chunk.size - nfft, nfft // 2)
acc = np.zeros(nfft, dtype=np.float64)
for s in starts:
    acc += np.abs(np.fft.fftshift(np.fft.fft(chunk[s:s+nfft] * win))) ** 2
power = np.maximum(acc / max(1, len(starts)), 1e-18)
db = 10 * np.log10(power)
freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / sr))

print(f"\nspectrum: floor(p20)={np.percentile(db,20):.1f} dB  peak={db.max():.1f} dB "
      f"at {freqs[int(np.argmax(db))]/1e3:+.0f} kHz  span={db.max()-np.percentile(db,20):.1f} dB")
for offset_khz in (-400, -300, -200, -100, 0, 100, 200, 300, 400):
    idx = int(np.argmin(np.abs(freqs - offset_khz * 1e3)))
    print(f"  {offset_khz:+5d} kHz : {db[idx]:6.1f} dB")
above = db >= np.percentile(db, 20) + 6
idx = np.where(above)[0]
if idx.size:
    print(f"occupied (>floor+6dB): {(freqs[idx[-1]]-freqs[idx[0]])/1e3:.0f} kHz")
