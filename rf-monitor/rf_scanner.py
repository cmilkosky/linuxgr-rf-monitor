#!/usr/bin/env python3
"""Watchlist wideband scanner (design A).

Why this exists: the hopper polled one frequency at a time (15 s dwell, 3.4 s
overhead per hop), so each channel was observed ~8% of the time and airband
voice could never be decoded at all - multimon-ng only speaks data protocols.
This lane does what the hardware is actually good at: one capture covers a whole
2.4 MHz chunk, every watchlist channel inside it is measured in the same FFT
pass, and whatever breaks squelch gets demodulated AM *and* NFM, gated by Silero,
transcribed by the homelab whisper ASR, and decoded for data protocols by
multimon-ng. Everything above squelch becomes an episode with audio attached -
voice, data, or an unidentified carrier.

Plan comes from the watchlist itself: entries are grouped into chunks that fit
in one capture, so adding a frequency to /watchlist is all it takes to widen the
scan.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np

import rf_decode
import rf_v2_capture as cap
import rf_voice_gate as voice_gate
import rf_watchlist
import signals_store as store

DEVICE = os.environ.get("RF_SCANNER_DEVICE", "2")
RATE = int(float(os.environ.get("RF_SCANNER_RATE", 2_400_000)))
DWELL_S = float(os.environ.get("RF_SCANNER_DWELL_S", 5.0))
GAIN = os.environ.get("RF_SCANNER_GAIN", "auto")
SQUELCH_DB = float(os.environ.get("RF_SCANNER_SQUELCH_DB", 8.0))
COOLDOWN_S = float(os.environ.get("RF_SCANNER_COOLDOWN_S", 90.0))
MAX_PER_CHUNK = int(os.environ.get("RF_SCANNER_MAX_PER_CHUNK", 3))
EDGE_HZ = int(float(os.environ.get("RF_SCANNER_EDGE_HZ", 250_000)))
MIN_WIDTH_HZ = float(os.environ.get("RF_SCANNER_MIN_WIDTH_HZ", 12_500))
# How far the LO must stay from a watched channel. The R820T has a DC spike at the
# tuned centre that is ~10-30 dB above noise; a single-frequency chunk (126.775,
# 154.430, 930.0) otherwise parks that spike exactly on the channel and reads it as
# a huge signal, or buries a real one under it. Same reason rf_hopper tunes +100 kHz.
DC_GUARD_HZ = float(os.environ.get("RF_SCANNER_DC_GUARD_HZ", 150_000))
DC_SHIFT_HZ = int(float(os.environ.get("RF_SCANNER_DC_SHIFT_HZ", 300_000)))
# Channel width by decoder. Watchlist bandwidth_hz defaults to 2 MHz (the parked-radio
# slot width) and must NOT be used as the measurement window: averaging 2 MHz of noise
# dilutes a 25 kHz airband carrier to nothing. Only an explicit narrow width wins.
WIDTH_BY_DECODER = {"airband": 25_000, "voice-fm": 12_500, "aprs": 12_500,
                    "pager": 25_000, "weather": 25_000, "ism": 12_500}

ASR_URL = os.environ.get("RF_SCANNER_ASR_URL",
                         "http://192.168.202.59:10401/v1/audio/transcriptions")
ASR_ENABLE = os.environ.get("RF_SCANNER_ASR_ENABLE", "1") not in ("0", "false", "no")
ASR_TIMEOUT = float(os.environ.get("RF_SCANNER_ASR_TIMEOUT", 120))
# The Frigate audio ASR gateway on :10401 is auth'd (AUDIO_ADAPTER_API_KEY in
# /etc/frigate-audio-asr-gateway.env on linux5); without a bearer it answers 401 and
# every transcript silently disappears. Keep the key in the service EnvironmentFile.
ASR_KEY = os.environ.get("RF_SCANNER_ASR_KEY", "").strip()
KEEP_SILENT = os.environ.get("RF_SCANNER_KEEP_SILENT", "1") not in ("0", "false", "no")
FRAME = 32768
_last: dict[int, float] = {}


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


# --------------------------------------------------------------------------- #
# plan: watchlist -> capture chunks
# --------------------------------------------------------------------------- #
def channel_width(entry: dict) -> float:
    """Measurement window for one watchlist entry, in Hz."""
    bw = float(entry.get("bandwidth_hz") or 0)
    if 0 < bw <= 50_000:
        return max(MIN_WIDTH_HZ, bw)
    return max(MIN_WIDTH_HZ, float(WIDTH_BY_DECODER.get(
        (entry.get("decoder") or "").lower(), 12_500)))


def plan() -> list[dict]:
    """Group enabled watchlist entries into chunks that fit one capture."""
    usable = RATE - 2 * EDGE_HZ
    half = usable / 2.0
    entries = [e for e in rf_watchlist.list_entries(enabled_only=True)
               if (e.get("decoder") or "") != "adsb"
               and (e.get("mode") or "parked") == "parked"]
    entries.sort(key=lambda e: int(e["freq_hz"]))
    chunks: list[dict] = []
    for entry in entries:
        f = int(entry["freq_hz"])
        if chunks:
            cur = chunks[-1]
            if f - cur["freqs"][0] <= usable:
                cur["freqs"].append(f)
                cur["entries"].append(entry)
                continue
        chunks.append({"freqs": [f], "entries": [entry]})
    out = []
    for chunk in chunks:
        lo, hi = min(chunk["freqs"]), max(chunk["freqs"])
        centre = (lo + hi) // 2
        # Keep the LO's DC spike off every watched channel: nudge the tuning by one or
        # two steps, accepting the first offset that still fits all channels in-band.
        for shift in (DC_SHIFT_HZ, -DC_SHIFT_HZ, 2 * DC_SHIFT_HZ, -2 * DC_SHIFT_HZ):
            cand = centre + shift
            if (all(abs(f - cand) <= half for f in chunk["freqs"])
                    and min(abs(f - cand) for f in chunk["freqs"]) >= DC_GUARD_HZ):
                centre = cand
                break
        out.append({"centre_hz": centre, "entries": chunk["entries"],
                    "span_hz": hi - lo})
    return out


# --------------------------------------------------------------------------- #
# capture (RTL-SDR writes unsigned 8-bit IQ)
# --------------------------------------------------------------------------- #
def capture(centre_hz: int, seconds: float, path: Path) -> int:
    samples = int(RATE * seconds)
    cmd = ["rtl_sdr", "-d", str(DEVICE), "-f", str(int(centre_hz)),
           "-s", str(int(RATE)), "-n", str(samples)]
    if GAIN and GAIN != "auto":
        cmd += ["-g", str(GAIN)]
    cmd.append(str(path))
    proc = subprocess.run(cmd, capture_output=True, timeout=seconds + 60)
    if not path.exists() or path.stat().st_size < 4096:
        raise RuntimeError("rtl_sdr produced no capture: "
                           + proc.stderr.decode("utf-8", "replace")[-200:])
    return path.stat().st_size


def read_rtl_iq(path: Path) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    raw = raw[: raw.size - (raw.size % 2)]
    iq = ((raw[0::2].astype(np.float32) - 127.5)
          + 1j * (raw[1::2].astype(np.float32) - 127.5))
    return (iq / 128.0).astype(np.complex64)


# --------------------------------------------------------------------------- #
# detection: one PSD pass, every watchlist channel in the chunk
# --------------------------------------------------------------------------- #
def channel_snr(iq: np.ndarray, centre_hz: int, entries: list[dict]) -> dict[int, dict]:
    """Per-channel power, one FFT pass, every watchlist channel in the chunk.

    Measured per frame as the PEAK bin inside the channel, not the mean across its
    width: a narrow AM carrier (+34 dB in its own bin) averaged over 25 kHz of noise
    reads +6 dB and the squelch never opens. A carrier persists frame to frame, so
    averaging the peak over ~100 frames still rejects single-bin interference.
    """
    win = np.hanning(FRAME).astype(np.float32)
    hop = FRAME // 2
    freqs = np.fft.fftshift(np.fft.fftfreq(FRAME, d=1.0 / RATE)) + centre_hz
    dc = np.abs(freqs - centre_hz) <= 8_000
    masks: dict[int, np.ndarray] = {}
    entries_by_freq: dict[int, dict] = {}
    for entry in entries:
        f = int(entry["freq_hz"])
        half = max(6_000.0, channel_width(entry) / 2.0)
        sel = np.abs(freqs - f) <= half
        if sel.any():
            masks[f] = sel
            entries_by_freq[f] = entry
    if not masks:
        return {}
    series: dict[int, list[float]] = {f: [] for f in masks}
    floors: list[float] = []
    blocks = 0
    for start in range(0, iq.size - FRAME, hop):
        spec = np.abs(np.fft.fftshift(np.fft.fft(iq[start:start + FRAME] * win))) ** 2
        db = 10.0 * np.log10(spec + 1e-12)
        # Reference must be the same statistic as the channel metric: the channel is a
        # PEAK over ~1600 bins, so comparing it to the median of the whole band would
        # report ~+12 dB on pure noise (the max of that many noise bins) and open the
        # squelch on every channel. p99.5 of the band is the matched comparator.
        floors.append(float(np.percentile(db[~dc], 99.5)))
        for f, sel in masks.items():
            series[f].append(float(db[sel].max()))
        blocks += 1
    if blocks == 0:
        return {}
    floor = float(np.mean(floors))
    out = {}
    for f, vals in series.items():
        arr = np.asarray(vals)
        ch = float(arr.mean())
        out[f] = {"snr_db": round(ch - floor, 1), "channel_db": round(ch, 1),
                  "floor_db": round(floor, 1),
                  "p95_snr_db": round(float(np.percentile(arr - floor, 95)), 1),
                  "entry": entries_by_freq[f]}
    return out


# --------------------------------------------------------------------------- #
# demod: mix -> decimate -> channel filter -> discriminate -> audio
# --------------------------------------------------------------------------- #
def demod_channel(iq: np.ndarray, sample_rate_hz: int, centre_hz: int, target_hz: int,
                  mode: str = "nfm", out_rate: int = 48_000) -> np.ndarray:
    """Demodulate one channel out of a wide capture.

    Order matters and this is the bug that made the first version deaf: rf_v2_capture
    ._demodulate discriminates at the full 2.4 MS/s and then boxcars 50 samples
    together, which averages FM voice (noise-like at that rate) down to ~zero. Mix and
    decimate first, filter the channel, and only then discriminate - the same order
    rf_decode.ddc_audio uses, which is the path the hopper and airband lane trust.
    """
    fs = float(sample_rate_hz)
    x = np.asarray(iq)
    offset = float(target_hz) - float(centre_hz)
    if abs(offset) > 1e-6:
        t = np.arange(x.size, dtype=np.float64)
        x = (x * np.exp(-2j * np.pi * offset * t / fs)).astype(np.complex64)
    factor = max(1, int(fs // 96_000.0))
    if factor > 1:
        usable = x.size - (x.size % factor)
        x = x[:usable].reshape(-1, factor).mean(axis=1)
    rate = fs / factor
    if x.size < 16:
        raise RuntimeError("capture too short to demodulate")
    chan_hz = 8_000.0 if mode == "am" else 5_000.0
    x = (cap._fir_lowpass(x.real, rate, chan_hz)
         + 1j * cap._fir_lowpass(x.imag, rate, chan_hz)).astype(np.complex64)
    if mode == "am":
        audio = np.abs(x).astype(np.float32)
        cutoff = 4_500.0
    elif mode in ("nfm", "fm"):
        audio = np.angle(x[1:] * np.conj(x[:-1])).astype(np.float32)
        cutoff = 3_500.0
    else:
        raise ValueError(f"unsupported audio mode {mode}")
    audio = cap._fir_lowpass(audio, rate, cutoff)
    step = max(1, int(rate // out_rate))
    if step > 1:
        usable = audio.size - (audio.size % step)
        audio = audio[:usable].reshape(-1, step).mean(axis=1)
        rate /= step
    if abs(rate - out_rate) > 1.0 and audio.size > 8:
        duration = audio.size / rate
        count = max(1, int(duration * out_rate))
        src = np.linspace(0.0, duration, audio.size, endpoint=False)
        dst = np.linspace(0.0, duration, count, endpoint=False)
        audio = np.interp(dst, src, audio).astype(np.float32)
    audio = audio - float(audio.mean())
    return audio.astype(np.float32)


# --------------------------------------------------------------------------- #
# audio: 48k float, and a 16k wav for Silero + ASR
# --------------------------------------------------------------------------- #
def wav16(audio: np.ndarray, rate: int, path: Path) -> Path:
    n = max(1, int(audio.size * 16_000 / rate))
    a = np.interp(np.linspace(0, audio.size - 1, n), np.arange(audio.size), audio)
    a = a - float(a.mean())
    peak = float(np.max(np.abs(a))) or 1.0
    pcm = np.clip(a / peak * 0.85 * 32767, -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(pcm.tobytes())
    return path


def transcribe(wav_path: Path) -> dict | None:
    if not ASR_ENABLE:
        return None
    boundary = "----rfscan%d" % int(time.time())
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="file"; '
             f'filename="{wav_path.name}"\r\n').encode()
    body += b"Content-Type: audio/wav\r\n\r\n"
    body += wav_path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        ASR_URL, data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    if ASR_KEY:
        req.add_header("Authorization", f"Bearer {ASR_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=ASR_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"   ASR failed: {exc}")
        return None


# --------------------------------------------------------------------------- #
# episode write
# --------------------------------------------------------------------------- #
def record(entry: dict, freq_hz: int, level: dict, audio: np.ndarray, mode: str,
           vad: dict | None, asr: dict | None, hits: list[dict],
           iq: np.ndarray | None = None, centre_hz: int = 0) -> int:
    snr = level["snr_db"]
    width = channel_width(entry)
    text = (asr or {}).get("text", "").strip()
    speech_pct = float((vad or {}).get("speech_pct") or 0.0)
    if hits:
        likely, decode_type = hits[0].get("decode_type", "voice"), hits[0].get("decode_type")
    elif speech_pct >= 1.0 or text:
        decode_type = "aircraft" if mode == "am" else "voice"
        likely = "AM voice" if mode == "am" else "FM voice"
    else:
        decode_type = None
        likely = "unidentified carrier"
    blob = {
        "freq_hz": int(freq_hz), "low_hz": int(freq_hz - width / 2),
        "high_hz": int(freq_hz + width / 2), "bandwidth_hz": int(width),
        "n_bins": max(1, int(width / (RATE / FRAME))),
        "peak_snr_db": round(max(0.0, min(snr, 60.0)), 1),
        "mean_snr_db": round(max(0.0, min(snr, 60.0)), 1),
        "delta_db": round(float(level["channel_db"] - level["floor_db"]), 2),
        "peak_rssi_db": level["channel_db"], "floor_db": level["floor_db"],
        "duty": 1.0,
        "score": round(min(70.0, 25.0 + 45.0 * min(1.0, max(0.0, snr) / 20.0)), 1),
        "band": "Watchlist", "likely": likely,
        "confidence": "high" if (hits or text) else "medium",
        "class": "known" if hits else "unexplained", "known_emitter": entry.get("label"),
        "features_json": json.dumps({"scanner": "watchlist-wideband", "mode": mode,
                                     "speech_pct": speech_pct,
                                     "transcript": text[:400]}),
        "source": "watchlist-scanner",
    }
    match = store.match_open_episode(blob["low_hz"], blob["high_hz"], tolerance_hz=int(width))
    if match is None:
        match = store.match_recent_episode(blob["low_hz"], blob["high_hz"], tolerance_hz=int(width))
    episode_id = int(match["id"]) if match is not None else int(
        store.upsert_blob(blob, tolerance_hz=int(width))["id"])

    out_dir = store.episode_artifact_dir(episode_id)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    wav = out_dir / f"audio-{mode}-{int(freq_hz // 1000)}kHz-{stamp}.wav"
    info = cap.write_wav(audio, wav)
    cap._to_mp3(wav, wav.with_suffix(".mp3"))
    store.add_artifact(episode_id, f"audio-{mode}", wav, size=info["bytes"])
    if text:
        txt = out_dir / f"transcript-{int(freq_hz // 1000)}kHz-{stamp}.txt"
        txt.write_text(text + "\n", encoding="utf-8")
        store.add_artifact(episode_id, "transcript", txt)
    # The console (/api/episodes, /api/episodes/<id>/audio/<mode>) reads the capture
    # lane's naming: audio-<mode>.{wav,mp3}, spectrum.png, capture.json. Without these
    # a scanner recording is invisible in the UI even though the DB row exists.
    plain_wav = out_dir / f"audio-{mode}.wav"
    plain_mp3 = out_dir / f"audio-{mode}.mp3"
    if plain_wav.resolve() != wav.resolve():
        shutil.copyfile(wav, plain_wav)
    shutil.copyfile(wav.with_suffix(".mp3"), plain_mp3)
    if iq is not None and centre_hz:
        try:
            cap.spectrogram_png(iq, RATE, centre_hz, out_dir / "spectrum.png")
        except Exception as exc:                      # a missing picture must not
            log(f"   spectrogram failed: {exc}")      # cost us the episode row
    bundle = {
        "capture": {"target_hz": int(freq_hz), "centre_hz": int(centre_hz or 0),
                    "sample_rate_hz": int(RATE),
                    "dwell_seconds": round(audio.size / 48_000, 2),
                    "started_at": stamp, "mode": mode, "source": "watchlist-scanner"},
        "features": {"peak_frequency_mhz": round(freq_hz / 1e6, 6), "snr_db": snr,
                     "floor_db": level["floor_db"], "speech_pct": speech_pct,
                     "mode": mode, "likely": likely, "label": entry.get("label")},
        "audio": {mode: {"wav": plain_wav.name, "mp3": plain_mp3.name}},
        "duration_seconds": round(audio.size / 48_000, 2),
        "transcript": text[:4000],
    }
    (out_dir / "capture.json").write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    if vad:
        store.set_voice(episode_id, vad)
    with store.connect() as conn:
        conn.execute("UPDATE episodes SET demod_freq_hz=? WHERE id=?", (int(freq_hz), episode_id))
        conn.commit()
    summary = f"{mode.upper()} audio {audio.size / 48_000:.1f}s @ {freq_hz / 1e6:.3f} MHz (+{snr:.0f} dB"
    if text:
        summary += f"; '{text[:120]}'"
    elif hits:
        summary += f"; {hits[0].get('line', '')[:120]}"
    summary += ")"
    if decode_type:
        store.mark_decoded(episode_id, decode_type, summary)
    store.record_attempt(episode_id, "watchlist-scanner", "ok", summary)
    log(f"   EP{episode_id} {freq_hz / 1e6:.3f} MHz {likely}"
        + (f" speech={speech_pct:.1f}%" if vad else "")
        + (f" '{text[:80]}'" if text else "")
        + (f" {hits[0].get('decode_type')}:{hits[0].get('line','')[:60]}" if hits else ""))
    return episode_id


# --------------------------------------------------------------------------- #
# cycle
# --------------------------------------------------------------------------- #
def cycle(chunks: list[dict] | None = None, *, dwell: float | None = None) -> dict:
    dwell = DWELL_S if dwell is None else dwell
    stats = {"chunks": 0, "active": 0, "recorded": 0, "voice": 0, "data": 0}
    for chunk in chunks or plan():
        stats["chunks"] += 1
        tmp = Path(tempfile.mkdtemp(prefix="rfscan-"))
        path = tmp / "chunk.iq"
        try:
            size = capture(chunk["centre_hz"], dwell, path)
            iq = read_rtl_iq(path)
            levels = channel_snr(iq, chunk["centre_hz"], chunk["entries"])
            active = {f: lv for f, lv in levels.items() if lv["snr_db"] >= SQUELCH_DB}
            log(f"chunk {chunk['centre_hz'] / 1e6:.3f} MHz {size / 1e6:.1f} MB "
                f"{len(chunk['entries'])} watchlist ch, {len(active)} over {SQUELCH_DB:.0f} dB")
            done = 0
            for freq, lv in sorted(active.items(), key=lambda kv: -kv[1]["snr_db"]):
                if time.time() - _last.get(freq, 0.0) < COOLDOWN_S:
                    continue
                entry = lv["entry"]
                offset = float(freq - chunk["centre_hz"])
                hint = (entry.get("decoder") or "")
                order = ["am", "nfm"] if hint == "airband" else ["nfm", "am"]
                best = None
                for mode in order:
                    try:
                        audio = demod_channel(iq, RATE, chunk["centre_hz"], freq, mode)
                    except Exception as exc:
                        log(f"   {freq / 1e6:.3f} MHz {mode} demod failed: {exc}")
                        continue
                    if audio.size < 4_800:
                        continue
                    gpath = wav16(audio, 48_000, tmp / f"g-{mode}.wav")
                    vad = voice_gate.gate_file(gpath)
                    score = float((vad or {}).get("speech_pct") or 0.0)
                    if best is None or score > best[2]:
                        best = (mode, audio, score, vad, gpath)
                    if score >= 1.0:
                        break
                if best is None:
                    continue
                mode, audio, speech_pct, vad, gpath = best
                if not (speech_pct >= 1.0 or KEEP_SILENT):
                    log(f"   {freq / 1e6:.3f} MHz +{lv['snr_db']:.1f} dB no voice, skipped")
                    _last[freq] = time.time()
                    continue
                try:
                    hits = rf_decode.run_multimon(rf_decode.audio_to_raw_22050(gpath), freq)
                except Exception as exc:
                    log(f"   {freq / 1e6:.3f} MHz multimon failed: {exc}")
                    hits = []
                asr = transcribe(gpath) if (speech_pct >= 1.0 or hits) else None
                ep = record(entry, freq, lv, audio, mode, vad, asr, hits,
                            iq=iq, centre_hz=chunk["centre_hz"])
                stats["active"] += 1
                stats["recorded"] += 1
                if (asr or {}).get("text"):
                    stats["voice"] += 1
                if hits:
                    stats["data"] += 1
                _last[freq] = time.time()
                done += 1
                if done >= MAX_PER_CHUNK:
                    break
        except Exception as exc:  # never die on one bad chunk
            log(f"chunk {chunk['centre_hz'] / 1e6:.3f} MHz failed: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watchlist wideband scanner")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--dwell", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    store.init_db()
    if args.plan:
        for chunk in plan():
            print(f"{chunk['centre_hz'] / 1e6:9.3f} MHz span {chunk['span_hz'] / 1e6:5.2f} MHz  "
                  + ", ".join(f"{int(e['freq_hz']) / 1e6:.3f}" for e in chunk["entries"]))
        return 0

    if args.loop:
        log(f"watchlist scanner: device={DEVICE} rate={RATE} dwell={DWELL_S:.0f}s "
            f"squelch=+{SQUELCH_DB:.0f} dB asr={'on' if ASR_ENABLE else 'off'}")
        while True:
            stats = cycle(dwell=args.dwell)
            log(f"cycle done: {stats}")
            time.sleep(2.0)

    stats = cycle(dwell=args.dwell)
    print(json.dumps(stats, indent=2) if args.json else f"cycle: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
