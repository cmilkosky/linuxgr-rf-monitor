"""Airband voice recorder.

Parks the HackRF over 108-137 MHz in 8 MHz chunks, shortlists the loudest 25 kHz
channels from one STFT pass, demodulates those few to audio and keeps the ones
that actually sound like voice. Recordings are attached to the matching episode
as `audio-am`, so the "Aviation AM voice" row the UI already lists gains a
playable sample instead of being a label with nothing behind it.

Why it exists: airband blobs score 21-30 while the capture gate is 45, so
sweep-detected airband episodes were never captured and never had audio. This
lane ignores the score gate and listens on purpose.

Detection notes, measured on real captures:
  * within-window envelope modulation does NOT separate voice - real speech sits
    at 0.07-0.29 there, the same range as a fading carrier;
  * demodulating the shortlist and scoring the audio does: the known-good
    123.825 MHz transmission scores mod 0.74 with 17% of its energy in the
    300-3000 Hz speech band, while bare carriers sit at 0.06-0.11 / ~0.02;
  * rfft on complex IQ is a trap - it casts to real (losing Q) and returns only
    positive frequencies, so half of every chunk goes unexamined. fft+fftshift.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

import rf_decode
import rf_v2_capture as cap
import signals_store as store

START_HZ = int(float(os.environ.get("RF_AIRBAND_START_HZ", 108_000_000)))
STOP_HZ = int(float(os.environ.get("RF_AIRBAND_STOP_HZ", 137_000_000)))
CHUNK_RATE = int(float(os.environ.get("RF_AIRBAND_RATE_HZ", 2_000_000)))
DWELL_S = float(os.environ.get("RF_AIRBAND_DWELL_S", 20.0))
CHANNEL_HZ = float(os.environ.get("RF_AIRBAND_CHANNEL_HZ", 25_000))
SHORTLIST = int(os.environ.get("RF_AIRBAND_SHORTLIST", 8))
SIGNAL_RATIO = float(os.environ.get("RF_AIRBAND_SIGNAL_RATIO", 1.8))
SIDEBAND_MIN = float(os.environ.get("RF_AIRBAND_SIDEBAND_MIN", 0.008))
SIDEBAND_MAX = float(os.environ.get("RF_AIRBAND_SIDEBAND_MAX", 0.6))
DYN_MIN_DB = float(os.environ.get("RF_AIRBAND_DYN_MIN_DB", 12.0))
QUIET_MIN = float(os.environ.get("RF_AIRBAND_QUIET_MIN", 0.0))  # off by default:
# continuous speech in a 6 s window has no 20 dB-deep gaps, so this gate only ever
# rejected real transmissions. Dynamic range alone separates voice from hum/noise.
MOD_MIN = float(os.environ.get("RF_AIRBAND_MOD_MIN", 0.30))
SPEECH_MIN = float(os.environ.get("RF_AIRBAND_SPEECH_MIN", 0.10))
COOLDOWN_S = float(os.environ.get("RF_AIRBAND_COOLDOWN_S", 120))
MAX_PER_CYCLE = int(os.environ.get("RF_AIRBAND_MAX_PER_CYCLE", 3))
MIN_CLIP_S = float(os.environ.get("RF_AIRBAND_MIN_CLIP_S", 0.5))
LEVEL_MIN_DB = float(os.environ.get("RF_AIRBAND_LEVEL_MIN_DB", 6.0))
LNA = int(float(os.environ.get("RF_AIRBAND_LNA_DB", 32)))
VGA = int(float(os.environ.get("RF_AIRBAND_VGA_DB", 24)))
IDLE_S = float(os.environ.get("RF_AIRBAND_IDLE_S", 2.0))

FRAME = 4096
HOP = 1024
_last: dict[int, float] = {}
_stop = False


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


def chunk_centers() -> list[int]:
    centers, f = [], START_HZ + CHUNK_RATE // 2
    while f < STOP_HZ:
        centers.append(int(f))
        f += CHUNK_RATE
    return centers


def _channel_power(iq: np.ndarray, rate: int, center_hz: int):
    """Mean power per 25 kHz channel across the whole capture (one STFT pass)."""
    view = np.lib.stride_tricks.sliding_window_view(iq, FRAME)[::HOP]
    frames = view.shape[0]
    bin_hz = rate / FRAME
    per_channel = max(1, int(round(CHANNEL_HZ / bin_hz)))
    n_ch = max(1, FRAME // per_channel)
    edges = np.arange(n_ch) * per_channel
    acc = np.zeros(n_ch, dtype=np.float64)
    spec_acc = np.zeros(FRAME, dtype=np.float64)
    step = 512
    for start in range(0, frames, step):
        block = view[start:start + step]
        spec = np.abs(np.fft.fftshift(np.fft.fft(block, axis=1), axes=1)) ** 2
        acc += np.add.reduceat(spec, edges, axis=1)[:, :n_ch].sum(axis=0)
        spec_acc += spec.sum(axis=0)
    power = acc / max(1, frames)
    spec_mean = spec_acc / max(1, frames)
    freqs = center_hz - rate / 2 + (edges + per_channel / 2) * bin_hz
    return power, freqs, spec_mean, bin_hz, per_channel


def _sideband_ratio(spec_mean: np.ndarray, ch: int, per_channel: int, bin_hz: float) -> float:
    """Power in the +/-0.3-3 kHz sidebands over the channel's peak bin.

    An AM voice transmission puts real energy in its sidebands; a bare carrier puts
    almost none there, which is what separates the two without needing to demodulate.
    """
    lo, hi = ch * per_channel, min((ch + 1) * per_channel, spec_mean.size)
    seg = spec_mean[lo:hi]
    if seg.size < 8:
        return 0.0
    peak = float(seg.max())
    if peak <= 0:
        return 0.0
    centre = lo + int(np.argmax(seg))
    off_lo = max(1, int(round(300 / bin_hz)))
    off_hi = max(off_lo + 1, int(round(3000 / bin_hz)))
    side = 0.0
    for off in range(off_lo, off_hi):
        for idx in (centre - off, centre + off):
            if lo <= idx < hi:
                side += float(spec_mean[idx])
    return side / peak


def _depth(iq: np.ndarray, rate: int, center_hz: int, freq_hz: int,
           out_rate: int = 48_000) -> float:
    """AM modulation depth straight from the IQ envelope.

    Must be measured before any normalisation: a demodulated carrier is DC plus a
    whisper of noise, and normalising that noise to full scale makes it look
    wildly modulated (mod 4.4 measured on a bare carrier). The raw envelope keeps
    the depth honest - 0.7 for real voice, ~0.02 for a bare carrier.
    """
    x = np.asarray(iq)
    fs = float(rate)
    offset = float(freq_hz) - float(center_hz)
    t = np.arange(x.size, dtype=np.float64)
    x = (x * np.exp(-2j * np.pi * offset * t / fs)).astype(np.complex64)
    factor = max(1, int(fs // (4 * out_rate)))
    usable = x.size - (x.size % factor)
    env = np.abs(x[:usable].reshape(-1, factor).mean(axis=1))
    if env.size < 8:
        return 0.0
    win = max(4, int(0.02 * out_rate))
    env = np.convolve(env, np.ones(win) / win, mode="same")
    mean = float(env.mean())
    return float(env.std() / mean) if mean > 0 else 0.0


def _score_audio(audio: np.ndarray, rate: int = 48_000) -> dict[str, float]:
    """Voice-vs-hum metrics for a demodulated channel.

    Calibrated on three known clips: confirmed airband speech, mains hum and plain
    noise. Only the 20 ms envelope dynamic range separates them cleanly -
    voice 22.7 dB / hum 5.9 dB / noise 1.3 dB - with the quiet-frame share as a
    second opinion. Spectral flux and envelope periodicity did NOT separate them
    (0.59 / 0.61 / 0.59 and 1.0 for all three), and the 300-3000 Hz speech share
    alone calls hum "speech" (0.60).
    """
    out = {"dyn_db": 0.0, "quiet": 0.0, "speech": 0.0, "mod": 0.0}
    frame = max(16, int(0.02 * rate))
    n = (audio.size // frame) * frame
    if n < frame * 4:
        return out
    audio = audio - float(audio.mean())
    peak = float(np.abs(audio).max())
    if peak <= 0:
        return out
    audio = audio / peak
    frames = audio[:n].reshape(-1, frame)
    rms = np.sqrt((frames ** 2).mean(axis=1)) + 1e-9
    db = 20 * np.log10(rms)
    out["dyn_db"] = float(np.percentile(db, 95) - np.percentile(db, 5))
    out["quiet"] = float((db < np.percentile(db, 90) - 20).mean())
    out["mod"] = float(rms.std() / (rms.mean() + 1e-9))
    spec = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
    fe = np.fft.rfftfreq(audio.size, 1 / rate)
    out["speech"] = float(spec[(fe >= 300) & (fe <= 3000)].sum() / (spec.sum() + 1e-9))
    return out


def _record(iq, rate, center_hz, freq_hz, audio, mod, speech, level_db) -> bool:
    """Save WAV+MP3 for one channel and attach them to its episode."""
    blob = {
        "freq_hz": int(freq_hz), "low_hz": int(freq_hz - CHANNEL_HZ / 2),
        "high_hz": int(freq_hz + CHANNEL_HZ / 2), "bandwidth_hz": int(CHANNEL_HZ),
        "n_bins": max(1, int(CHANNEL_HZ / (rate / FRAME))),
        "peak_snr_db": round(max(0.0, min(level_db, 60.0)), 1),
        "mean_snr_db": round(max(0.0, min(level_db, 60.0)), 1),
        "delta_db": round(mod * 10, 2), "peak_rssi_db": -50.0, "floor_db": -95.0,
        "duty": round(min(mod, 1.0), 3),
        "score": round(min(70.0, 25.0 + 45.0 * min(1.0, mod * speech * 4)), 1),
        "band": "Airband", "likely": "Aviation AM voice", "confidence": "high",
        "class": "known", "known_emitter": None,
        "features_json": json.dumps({"recorder": "airband", "mod_index": round(mod, 3),
                                     "speech_share": round(speech, 3)}),
        "source": "airband-recorder",
    }
    match = store.match_open_episode(blob["low_hz"], blob["high_hz"], tolerance_hz=int(CHANNEL_HZ))
    if match is None:
        match = store.match_recent_episode(blob["low_hz"], blob["high_hz"], tolerance_hz=int(CHANNEL_HZ))
    if match is not None:
        episode_id = int(match["id"])
    else:
        episode_id = int(store.upsert_blob(blob, tolerance_hz=int(CHANNEL_HZ))["id"])

    out_dir = store.episode_artifact_dir(episode_id)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    wav = out_dir / f"audio-am-{int(freq_hz // 1000)}kHz-{stamp}.wav"
    info = cap.write_wav(audio, wav)
    cap._to_mp3(wav, wav.with_suffix(".mp3"))
    store.add_artifact(episode_id, "audio-am", wav, size=info["bytes"])
    store.mark_decoded(episode_id, "aircraft",
                       f"AM audio {audio.size / 48000:.1f}s @ {freq_hz / 1e6:.3f} MHz "
                       f"(+{level_db:.0f} dB, mod {mod:.2f}) - listen to confirm voice")
    store.record_attempt(episode_id, "airband-recorder", "ok",
                         f"{audio.size / 48000:.1f}s audio mod={mod:.2f} speech={speech:.2f}")
    log(f"RECORDED ep{episode_id} {freq_hz / 1e6:.3f} MHz {audio.size / 48000:.1f}s "
        f"mod={mod:.2f} speech={speech:.2f} -> {wav.with_suffix('.mp3').name}")
    return True


def cycle(centers: list[int] | None = None) -> int:
    recorded = 0
    for center in centers or chunk_centers():
        tmp = Path(tempfile.mkdtemp(prefix="airband-"))
        iq_path = tmp / "chunk.iq"
        try:
            with cap.device_lock(180.0, cap.PORTAPACK_LOCK_PATH):
                cap.capture_hackrf(center, iq_path, duration_seconds=DWELL_S, sample_rate_hz=CHUNK_RATE,
                                   lna_gain_db=LNA, vga_gain_db=VGA,
                                   device_serial=os.environ.get("RF_CAPTURE_DEVICE_SERIAL"), lock_path=None)
            iq = cap.read_iq(iq_path)
            power, freqs, spec_mean, bin_hz, per_channel = _channel_power(iq, CHUNK_RATE, center)
            floor = float(np.median(power)) + 1e-12
            ranked = np.argsort(power)[::-1][:SHORTLIST * 3]
            candidates = [int(i) for i in ranked if power[i] >= SIGNAL_RATIO * floor][:SHORTLIST]
            log(f"chunk {center / 1e6:.1f} MHz: {power.size} channels, {len(candidates)} above "
                f"{SIGNAL_RATIO:.1f}x floor")
            for ch in candidates:
                freq = int(round(freqs[ch]))
                if time.time() - _last.get(freq, 0) < COOLDOWN_S:
                    continue
                pcm = rf_decode.ddc_audio(iq, CHUNK_RATE, center, freq, out_rate=48_000,
                                          channel_hz=8_000.0, audio_cutoff_hz=4_000.0)
                audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                m = _score_audio(audio)
                side = _sideband_ratio(spec_mean, ch, per_channel, bin_hz)
                level_db = 10 * np.log10(power[ch] / floor)
                log(f"   {freq / 1e6:.3f} MHz level=+{level_db:.1f} dB side={side:.3f} "
                    f"dyn={m['dyn_db']:.1f}dB quiet={m['quiet']:.2f} speech={m['speech']:.2f}")
                if not (SIDEBAND_MIN <= side <= SIDEBAND_MAX):
                    continue
                if level_db < LEVEL_MIN_DB:
                    continue          # 3-9 dB above the floor is receiver noise, not a station
                if m["dyn_db"] < DYN_MIN_DB or m["quiet"] < QUIET_MIN:
                    continue
                if _record(iq, CHUNK_RATE, center, freq, audio, m["mod"], m["speech"], level_db):
                    _last[freq] = time.time()
                    recorded += 1
                    if recorded >= MAX_PER_CYCLE:
                        return recorded
        except Exception as exc:  # never die on one bad chunk
            log(f"chunk {center / 1e6:.1f} MHz failed: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return recorded


def main() -> int:
    parser = argparse.ArgumentParser(description="airband AM voice recorder")
    parser.add_argument("--once", action="store_true", help="one pass over all chunks, then exit")
    parser.add_argument("--center", type=int, default=None, help="single chunk centre in Hz")
    args = parser.parse_args()

    def _sig(_n, _f):
        global _stop
        _stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    store.init_db()
    log(f"airband recorder: {START_HZ / 1e6:.0f}-{STOP_HZ / 1e6:.0f} MHz in "
        f"{CHUNK_RATE / 1e6:.0f} MHz chunks, {DWELL_S:.0f}s dwell, {CHANNEL_HZ / 1000:.0f} kHz channels")
    centers = [args.center] if args.center else None
    while not _stop:
        cycle(centers)
        if args.once:
            break
        for _ in range(int(IDLE_S * 10)):
            if _stop:
                break
            time.sleep(0.1)
    log("airband recorder stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
