#!/usr/bin/env python3
"""Wideband lane tests: does a channel buried in a 5 MS/s capture still decode?

The narrowband decode path was already proven at +250 kHz (tests/test_decode.py).
The wideband lane is a different problem: the channel sits far from the tune
frequency and ddc_audio coarse-decimates by ~52 before it can afford a FIR. A
boxcar that long is a poor anti-alias filter, so the question this answers is
whether that shortcut costs us decodes - and whether a strong neighbour destroys
a weak channel.

Run on linuxGR (needs multimon-ng and gen_packets from direwolf):

    cd /home/cmilkosk/rf-monitor && python3 tests/test_wideband.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import rf_decode  # noqa: E402
import rf_wideband_lane as lane  # noqa: E402
from test_decode import make_aprs_wav, synth_iq  # noqa: E402

RATE = 5_000_000
OFFSET = 1_500_000
APRS_HZ = 144_390_000


def with_interferer(iq, offset_hz: float = -1_500_000.0, amplitude: float = 20.0):
    """A neighbour 26 dB stronger than the packet we want, still in the window."""
    t = np.arange(iq.size, dtype=np.float64)
    return (iq + amplitude * np.exp(2j * np.pi * offset_hz * t / RATE)).astype(np.complex64)


def main() -> int:
    failures = 0
    with_tmp = Path("/tmp/rf-wideband-test.wav")
    if not make_aprs_wav(with_tmp):
        print("SKIP: gen_packets not available (install direwolf to run this test)")
        return 0

    try:
        iq, rate, center = synth_iq(with_tmp, offset_hz=OFFSET, rate=RATE)
        print(f"fixture: {iq.size} samples @ {rate / 1e6:.1f} MS/s, packet at +{OFFSET / 1e3:.0f} kHz")

        # 1. spectrum sees the channel
        report = lane.channel_snr(iq, rate, center, [{"freq_hz": APRS_HZ, "label": "APRS", "decoder": "aprs"}])
        snr = report[0]["snr_db"]
        print(f"1. channel_snr -> {snr:.1f} dB carrier={report[0]['carrier']}")
        if not report[0]["carrier"]:
            failures += 1
            print("   FAIL: spectrum did not see the packet")

        # 2. clean wideband capture decodes
        audio = rf_decode.ddc_audio(iq, rate, center, APRS_HZ)
        hits = rf_decode.run_multimon(audio, APRS_HZ)
        print(f"2. clean capture -> {len(hits)} hit(s)")
        for hit in hits[:2]:
            print(f"   {hit['protocol']}: {hit['line'][:70]}")
        if not hits:
            failures += 1
            print("   FAIL: wideband DDC lost the packet that the narrowband path decodes")

        # 3. strong neighbour must not destroy it
        loud = with_interferer(iq)
        audio2 = rf_decode.ddc_audio(loud, rate, center, APRS_HZ)
        hits2 = rf_decode.run_multimon(audio2, APRS_HZ)
        print(f"3. +26 dB neighbour at -1.5 MHz -> {len(hits2)} hit(s)")
        if not hits2:
            failures += 1
            print("   FAIL: a strong in-window neighbour killed the channel (boxcar aliasing)")

        # 4. a channel that is not in the capture must not decode
        empty = rf_decode.ddc_audio(iq, rate, center, 145_050_000)
        hits3 = rf_decode.run_multimon(empty, 145_050_000)
        print(f"4. empty channel 145.050 -> {len(hits3)} hit(s)")
        if hits3:
            failures += 1
            print("   FAIL: decoded a channel that carries nothing")

        # 5. noise alone must not look like a carrier
        rng = np.random.default_rng(11)
        noise_iq = ((rng.standard_normal(iq.size) + 1j * rng.standard_normal(iq.size))
                    .astype(np.complex64) * 0.05)
        rep = lane.channel_snr(noise_iq, rate, center,
                               [{"freq_hz": APRS_HZ, "label": "APRS", "decoder": "aprs"}])
        print(f"5. noise only -> {rep[0]['snr_db']:.1f} dB carrier={rep[0]['carrier']}")
        if rep[0]["carrier"]:
            failures += 1
            print("   FAIL: noise scored as a carrier - floor is not local")

        print("\n%d failure(s)" % failures)
        return 1 if failures else 0
    finally:
        with_tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
