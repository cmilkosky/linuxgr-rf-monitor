"""Diagnose HackRF DC-spike poisoning of feature extraction.

Captures 101.8 MHz (a 300 kHz LO offset from the strongest local FM bin at
101.5 MHz) and compares occupied-bandwidth estimates for:
  A. raw IQ, signal at -300 kHz from tune  (what v1 did: tuned dead-centre)
  B. mean-subtracted IQ, signal off-centre
  C. mean-subtracted IQ, signal dead-centre (for reference)

Also measures spectrogram PNG size for RGB vs 64-colour palette.
"""

import sys
from pathlib import Path

sys.path.insert(0, "/home/cmilkosk/rf-monitor")

import numpy as np
from PIL import Image

import rf_v2_capture as capture
from rf_signal_intel import extract_iq_features

OUT = Path("/tmp/rf-v2-diag")
OUT.mkdir(parents=True, exist_ok=True)
IQ = OUT / "capture.iq"

if not IQ.exists():
    print("capturing 101.8 MHz (300 kHz LO offset) ...", flush=True)
    capture.capture_hackrf(101_800_000, IQ, sample_rate_hz=2_000_000, duration_seconds=4)

iq = capture.read_iq(IQ)
sr = 2_000_000
print(f"samples={iq.size}  mean={iq.mean():.5f}  |mean|={abs(iq.mean()):.5f}")


def summarise(label, data, centre):
    f = extract_iq_features(data, sr, centre)
    print(
        f"{label:34s} bw={f['occupied_bandwidth_hz']:>9,} Hz  "
        f"peak={f['peak_frequency_hz']/1e6:.6f} MHz  "
        f"peaks={f['peak_count']}  flat={f['spectral_flatness']}  "
        f"traits={','.join(f['traits'])}"
    )
    return f


print("\n--- feature extraction variants ---")
summarise("A raw, tuned dead-centre", iq, 101_500_000)
summarise("B mean-sub, tuned +300 kHz", iq - iq.mean(), 101_800_000)
summarise("C mean-sub, dead-centre", iq - iq.mean(), 101_500_000)

print("\n--- spectrogram PNG size ---")
for tag, centre in (("off-centre", 101_800_000),):
    png = OUT / f"spec-{tag}.png"
    capture.spectrogram_png(iq, sr, centre, png)
    print(f"  RGB PNG            {png.stat().st_size:,} bytes")
    img = Image.open(png).convert("RGB").quantize(colors=64, method=Image.Quantize.MEDIANCUT)
    png_q = OUT / f"spec-{tag}-q64.png"
    img.save(png_q, optimize=True)
    print(f"  palette-64 PNG     {png_q.stat().st_size:,} bytes")
    img32 = Image.open(png).convert("RGB").quantize(colors=32, method=Image.Quantize.MEDIANCUT)
    png_32 = OUT / f"spec-{tag}-q32.png"
    img32.save(png_32, optimize=True)
    print(f"  palette-32 PNG     {png_32.stat().st_size:,} bytes")
