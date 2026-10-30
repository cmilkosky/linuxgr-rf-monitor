"""Detector regression: AM-modulate the real 123.825 MHz recording onto synthetic IQ
and require the airband recorder's scoring to accept it - and to reject a bare carrier.

Uses audio known to be speech (captured off air at 19:33 EDT) instead of waiting for a
live transmission to happen.
"""
import sys
import wave

import numpy as np

sys.path.insert(0, "/home/cmilkosk/rf-monitor")
import rf_airband_recorder as rec  # noqa: E402
import rf_decode  # noqa: E402

RATE = 2_000_000
CENTER = 120_000_000
OFFSET = 500_000
DURATION = 8.0

SAMPLE = "/tmp/airband-sample.wav"
if not __import__("os").path.exists(SAMPLE):
    print("SKIP no sample at %s - copy a known airband recording there first" % SAMPLE)
    raise SystemExit(0)

with wave.open(SAMPLE, "rb") as h:
    pcm = np.frombuffer(h.readframes(h.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    src_rate = h.getframerate()
n = int(DURATION * RATE)
t_dst = np.arange(n) / RATE
t_src = np.arange(pcm.size) / src_rate
audio = np.interp(t_dst, t_src, pcm[: int(DURATION * src_rate)])
audio = audio / (np.abs(audio).max() + 1e-9)
t = np.arange(n) / RATE
carrier = np.exp(2j * np.pi * OFFSET * t)
rng = np.random.default_rng(7)
noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.02


def score(iq, label, expect):
    power, freqs, spec_mean, bin_hz, per_channel = rec._channel_power(iq, RATE, CENTER)
    floor = float(np.median(power)) + 1e-12
    ch = int(np.argmin(np.abs(freqs - (CENTER + OFFSET))))
    pcm_out = rf_decode.ddc_audio(iq, RATE, CENTER, int(round(freqs[ch])), out_rate=48_000,
                                  channel_hz=8_000.0, audio_cutoff_hz=4_000.0)
    a = np.frombuffer(pcm_out, dtype="<i2").astype(np.float32) / 32768.0
    m = rec._score_audio(a)
    side = rec._sideband_ratio(spec_mean, ch, per_channel, bin_hz)
    lvl = 10 * np.log10(power[ch] / floor)
    print("%-16s %.3f MHz  level=+%.1f dB  side=%.3f  dyn=%.1f dB  quiet=%.2f  speech=%.2f  (channels=%d)"
          % (label, freqs[ch] / 1e6, lvl, side, m["dyn_db"], m["quiet"], m["speech"], power.size))
    return m, side, lvl


# Real airband AM runs deep: the captured 123.825 MHz transmission measures a
# depth of 0.74. Drive the envelope from 0.2 to 1.0 so the synthetic matches it.
env_audio = (audio - audio.min()) / (audio.max() - audio.min() + 1e-9)
voice = ((0.2 + 0.8 * env_audio) * carrier + noise).astype(np.complex64)
m, side, lvl = score(voice, "voice", True)
assert rec.SIDEBAND_MIN <= side <= rec.SIDEBAND_MAX, "voice rejected: side %.3f" % side
assert m["dyn_db"] >= rec.DYN_MIN_DB, "voice rejected: dyn %.1f dB" % m["dyn_db"]
print("PASS voice accepted (side %.3f, dyn %.1f dB)" % (side, m["dyn_db"]))

quiet = (carrier + noise).astype(np.complex64)
m2, side2, _ = score(quiet, "bare carrier", False)
assert m2["dyn_db"] < rec.DYN_MIN_DB, "bare carrier accepted (dyn %.1f dB)" % m2["dyn_db"]
print("PASS bare carrier rejected (side %.3f, dyn %.1f dB)" % (side2, m2["dyn_db"]))