"""Compare uint8 (v1 convention) vs int8 (hackrf_transfer's real format) IQ reading."""

import sys

import numpy as np

sys.path.insert(0, "/home/cmilkosk/rf-monitor")
from rf_signal_intel import extract_iq_features

SR = 2_000_000
TUNE = 101_800_000


def spectrum_stats(iq):
    nfft = 4096
    window = np.hanning(nfft).astype(np.float32)
    spec = np.abs(np.fft.fftshift(np.fft.fft(iq[:nfft] * window))) ** 2
    db = 10 * np.log10(spec + 1e-18)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / SR))
    return freqs, db


for name in ("cap-32-24", "cap-16-16", "cap-8-8"):
    raw = np.fromfile(f"/tmp/rf-v2-gain/{name}.iq", dtype=np.uint8)

    as_uint = ((raw[0::2].astype(np.float32) - 127.5)
               + 1j * (raw[1::2].astype(np.float32) - 127.5)) / 128.0
    signed = raw.view(np.int8)
    as_int = (signed[0::2].astype(np.float32) + 1j * signed[1::2].astype(np.float32)) / 128.0

    for tag, iq in (("uint8", as_uint), ("int8 ", as_int)):
        rms = 20 * np.log10(np.sqrt(np.mean(np.abs(iq) ** 2)) + 1e-12)
        freqs, db = spectrum_stats(iq)
        feats = extract_iq_features(iq, SR, TUNE)
        peak_khz = freqs[int(np.argmax(db))] / 1e3
        print(f"{name:9s} {tag}  rms={rms:6.1f} dBFS  DC={abs(iq.mean()):5.3f}  "
              f"peak@{peak_khz:+7.0f} kHz  floor={np.percentile(db, 20):6.1f}  pk={db.max():6.1f}  "
              f"bw={feats['occupied_bandwidth_hz']/1e3:7.0f} kHz  traits={','.join(feats['traits'])}")
    print()
