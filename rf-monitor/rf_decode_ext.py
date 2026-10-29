"""External decoders for the RF monitor: APRS, FT8/FT4, ACARS, AIS.

The multimon-ng ladder only knows digital *pager/EAS/APRS-AFSK* frames from
demodulated audio. These four tools decode things it never will, so they are
band-gated: each one runs only when the episode sits on its calling frequency
(within a tolerance) and the capture actually covers that frequency. All of them
need full-rate IQ, so an episode backfilled from audio artifacts cannot use them.

Design notes that cost real debugging time:
  * FT8/FT4 are USB, not FM. Feeding a discriminator output to jt9 decodes
    nothing; the real part of the baseband is the audio.
  * ACARS is AM at 12.5 kHz audio. acarsdec wants an envelope-detected WAV.
  * AIS is GMSK: AIS-catcher wants raw CU8 IQ centred between channels A and B
    (162.000 MHz), so the IQ is re-mixed and decimated here first.
  * jt9 only decodes slot-aligned audio (FT8 15 s, FT4 7.5 s, UTC grid), so the
    window is cut on the slot boundary when the capture start time is known.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

import rf_decode

# Calling frequencies (dial), tolerance, audio rate, demodulator.
FT8_2M, FT4_2M = 144_174_000, 144_170_000
FT8_70CM, FT4_70CM = 432_174_000, 432_170_000
FT8_6M, FT4_6M = 50_313_000, 50_318_000
APRS_NARROW = 144_390_000
ACARS_CHANNELS = (131_550_000, 131_725_000, 130_425_000)
AIS_CENTER = 162_000_000

NARROW_TOL = 40_000
ACARS_TOL = 30_000
AIS_TOL = 120_000


# --------------------------------------------------------------------------- #
# demodulation (numpy only - the system scipy has a broken ABI)
# --------------------------------------------------------------------------- #
def _audio(iq, sample_rate_hz: int, center_hz: int, dial_hz: int, out_rate: int,
           mode: str, channel_hz: float = 10_000.0, cutoff_hz: float = 4_000.0) -> bytes:
    """Mix `dial_hz` to baseband and demodulate to s16le mono at `out_rate`."""
    x = np.asarray(iq)
    fs = float(sample_rate_hz)
    offset = float(dial_hz) - float(center_hz)
    if abs(offset) > 1e-6:
        t = np.arange(x.size, dtype=np.float64)
        x = (x * np.exp(-2j * np.pi * offset * t / fs)).astype(np.complex64)

    factor = max(1, int(fs // 96_000.0))
    if factor > 1:
        usable = x.size - (x.size % factor)
        x = x[:usable].reshape(-1, factor).mean(axis=1)
    rate = fs / factor

    if mode == "fm":
        if channel_hz < rate / 2.0:
            x = (rf_decode._fir_lowpass(x.real, rate, channel_hz)
                 + 1j * rf_decode._fir_lowpass(x.imag, rate, channel_hz))
        audio = np.angle(x[1:] * np.conj(x[:-1])).astype(np.float32)
        audio = rf_decode._fir_lowpass(audio, rate, cutoff_hz)
    elif mode == "usb":
        audio = rf_decode._fir_lowpass(x.real.astype(np.float32), rate, channel_hz)
    elif mode == "am":
        audio = rf_decode._fir_lowpass(np.abs(x).astype(np.float32), rate, cutoff_hz)
    else:
        raise ValueError(f"unknown demod {mode}")
    audio_rate = rate

    step = max(1, int(audio_rate // out_rate))
    if step > 1:
        usable = audio.size - (audio.size % step)
        audio = audio[:usable].reshape(-1, step).mean(axis=1)
        audio_rate /= step
    if abs(audio_rate - out_rate) > 1.0 and audio.size > 8:
        duration = audio.size / audio_rate
        count = max(1, int(duration * out_rate))
        src = np.linspace(0.0, duration, audio.size, endpoint=False)
        dst = np.linspace(0.0, duration, count, endpoint=False)
        audio = np.interp(dst, src, audio).astype(np.float32)

    audio = audio - float(audio.mean())
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak <= 0.0:
        raise RuntimeError("empty audio after demodulation")
    return np.clip(audio / peak * 0.85 * 32767.0, -32768, 32767).astype("<i2").tobytes()


def _write_wav(path: Path, pcm: bytes, rate: int) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return path


def _slot_window(pcm: bytes, rate: int, slot_s: float, start_epoch: float | None) -> bytes:
    """Cut one slot-aligned window out of `pcm` (jt9 needs the UTC grid)."""
    width = int(round(slot_s * rate)) * 2
    if len(pcm) <= width:
        return pcm
    if start_epoch is None:
        return pcm[:width]
    offset = (-start_epoch) % slot_s
    start = int(round(offset * rate)) * 2
    if start + width > len(pcm):
        start = max(0, len(pcm) - width)
    return pcm[start:start + width]


# --------------------------------------------------------------------------- #
# runners
# --------------------------------------------------------------------------- #
def _run_direwolf(pcm: bytes, tmp: Path, dial_hz: int) -> list[dict[str, Any]]:
    exe = shutil.which("direwolf")
    if not exe:
        raise RuntimeError("direwolf not installed")
    cmd = [exe, "-r", "48000", "-n", "1", "-b", "16", "-B", "1200", "-q", "h", "-"]
    proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=120)
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    hits = []
    for line in text.splitlines():
        # direwolf prefixes each decoded frame with [channel.time], e.g. [0.3]
        match = re.match(r"^\[\d+(?:\.\d+)?[LM]?\]\s*(\S.*)$", line.strip())
        if match and ">" in match.group(1):
            hits.append(rf_decode._hit("direwolf", "AFSK1200", match.group(1).strip(), dial_hz))
    return hits


def _run_jt9(pcm: bytes, tmp: Path, dial_hz: int, mode: str,
             start_epoch: float | None) -> list[dict[str, Any]]:
    exe = shutil.which("jt9")
    if not exe:
        raise RuntimeError("jt9 not installed")
    slot = 7.5 if mode == "ft4" else 15.0
    wav = _write_wav(tmp / f"{mode}.wav", _slot_window(pcm, 12_000, slot, start_epoch), 12_000)
    flag = "-5" if mode == "ft4" else "-8"
    proc = subprocess.run([exe, flag, str(wav)], capture_output=True, timeout=180)
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    hits = []
    for line in text.splitlines():
        match = re.match(r"^\s*\d{6}\s+(-?\d+)\s+([-\d.]+)\s+(\d+)\s+(.+)$", line)
        if match:
            hit = rf_decode._hit("jt9", mode.upper(), match.group(4).strip(), dial_hz)
            hit["summary"] = f"{match.group(4).strip()} (snr {match.group(1)} dB)"
            hits.append(hit)
    return hits


def _run_acarsdec(pcm: bytes, tmp: Path, dial_hz: int) -> list[dict[str, Any]]:
    exe = shutil.which("acarsdec")
    if not exe:
        raise RuntimeError("acarsdec not installed")
    wav = _write_wav(tmp / "acars.wav", pcm, 12_500)
    proc = subprocess.run([exe, "-f", str(wav), "-o", "1", "-e"], capture_output=True, timeout=120)
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    hits = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean or clean.startswith(("-", "Acarsdec", "Usage", "Up to")):
            continue
        if re.search(r"(Aircraft|Reg|Flight|Label|Msgno|Mode|Message)", clean, re.I):
            hits.append(rf_decode._hit("acarsdec", "ACARS", clean, dial_hz))
    return hits


def _run_ais(iq, sample_rate_hz: int, center_hz: int, tmp: Path) -> list[dict[str, Any]]:
    exe = shutil.which("AIS-catcher")
    if not exe:
        raise RuntimeError("AIS-catcher not installed")
    # Re-mix to 162.000 MHz (between channels A and B) and decimate to 96 kHz.
    fs = float(sample_rate_hz)
    x = np.asarray(iq)
    offset = AIS_CENTER - float(center_hz)
    if abs(offset) > 1e-6:
        t = np.arange(x.size, dtype=np.float64)
        x = (x * np.exp(-2j * np.pi * offset * t / fs)).astype(np.complex64)
    factor = max(1, int(fs // 96_000.0))
    if factor > 1:
        usable = x.size - (x.size % factor)
        x = x[:usable].reshape(-1, factor).mean(axis=1)
    rate = int(round(fs / factor))
    cu8 = tmp / "ais.cu8"
    rf_decode.iq_to_cu8(x, cu8)
    proc = subprocess.run([exe, "-r", "CU8", str(cu8), "-s", str(rate), "-c", "AB",
                           "-o", "1", "-q"], capture_output=True, timeout=180)
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    hits = []
    for line in text.splitlines():
        clean = line.strip()
        if clean.startswith("!AIVDM") or clean.startswith("!AIVDO"):
            hits.append(rf_decode._hit("AIS-catcher", "AIS", clean, AIS_CENTER))
    return hits


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Candidate:
    name: str
    protocol: str
    dial_hz: int
    tolerance_hz: int
    out_rate: int
    mode: str
    runner: str            # direwolf | jt9-ft8 | jt9-ft4 | acarsdec | ais
    channel_hz: float = 10_000.0
    cutoff_hz: float = 4_000.0


CANDIDATES: tuple[Candidate, ...] = (
    Candidate("direwolf/aprs", "aprs", APRS_NARROW, NARROW_TOL, 48_000, "fm", "direwolf",
              channel_hz=12_000.0, cutoff_hz=4_000.0),
    Candidate("jt9/ft8-2m", "ft8", FT8_2M, NARROW_TOL, 12_000, "usb", "jt9-ft8",
              channel_hz=3_000.0, cutoff_hz=3_000.0),
    Candidate("jt9/ft4-2m", "ft4", FT4_2M, NARROW_TOL, 12_000, "usb", "jt9-ft4",
              channel_hz=3_000.0, cutoff_hz=3_000.0),
    Candidate("jt9/ft8-70cm", "ft8", FT8_70CM, NARROW_TOL, 12_000, "usb", "jt9-ft8",
              channel_hz=3_000.0, cutoff_hz=3_000.0),
    Candidate("jt9/ft4-70cm", "ft4", FT4_70CM, NARROW_TOL, 12_000, "usb", "jt9-ft4",
              channel_hz=3_000.0, cutoff_hz=3_000.0),
    Candidate("jt9/ft8-6m", "ft8", FT8_6M, NARROW_TOL, 12_000, "usb", "jt9-ft8",
              channel_hz=3_000.0, cutoff_hz=3_000.0),
    Candidate("jt9/ft4-6m", "ft4", FT4_6M, NARROW_TOL, 12_000, "usb", "jt9-ft4",
              channel_hz=3_000.0, cutoff_hz=3_000.0),
) + tuple(
    Candidate(f"acarsdec/{freq // 1_000_000}.{freq % 1_000_000 // 1_000:03d}", "acars", freq,
              ACARS_TOL, 12_500, "am", "acarsdec", channel_hz=12_000.0, cutoff_hz=6_000.0)
    for freq in ACARS_CHANNELS
) + (
    Candidate("ais-catcher", "ais", AIS_CENTER, AIS_TOL, 96_000, "iq", "ais"),
)


def matching_candidates(freq_hz: int, sample_rate_hz: int, center_hz: int) -> list[Candidate]:
    """Candidates whose dial is close to the episode AND inside the captured span."""
    lo = center_hz - sample_rate_hz / 2.0
    hi = center_hz + sample_rate_hz / 2.0
    out = []
    for cand in CANDIDATES:
        if abs(int(freq_hz) - cand.dial_hz) > cand.tolerance_hz:
            continue
        if not (lo <= cand.dial_hz <= hi):
            continue
        out.append(cand)
    return out


def decode_external(iq, sample_rate_hz: int, center_hz: int, freq_hz: int,
                    start_epoch: float | None = None):
    """Run every matching decoder. Returns [(decoder, status, detail, hits), ...]."""
    results = []
    for cand in matching_candidates(freq_hz, sample_rate_hz, center_hz):
        tmp = Path(tempfile.mkdtemp(prefix="rf-ext-"))
        try:
            if cand.runner == "ais":
                hits = _run_ais(iq, sample_rate_hz, center_hz, tmp)
            else:
                pcm = _audio(iq, sample_rate_hz, center_hz, cand.dial_hz, cand.out_rate,
                             cand.mode, channel_hz=cand.channel_hz, cutoff_hz=cand.cutoff_hz)
                if cand.runner == "direwolf":
                    hits = _run_direwolf(pcm, tmp, cand.dial_hz)
                elif cand.runner == "jt9-ft8":
                    hits = _run_jt9(pcm, tmp, cand.dial_hz, "ft8", start_epoch)
                elif cand.runner == "jt9-ft4":
                    hits = _run_jt9(pcm, tmp, cand.dial_hz, "ft4", start_epoch)
                elif cand.runner == "acarsdec":
                    hits = _run_acarsdec(pcm, tmp, cand.dial_hz)
                else:
                    raise RuntimeError(f"unknown runner {cand.runner}")
            results.append((cand.name, "ok" if hits else "no_frame",
                            f"{len(hits)} frame(s)", hits))
        except Exception as exc:  # a decoder fault must never kill the capture
            results.append((cand.name, "error", str(exc)[:300], []))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return results
