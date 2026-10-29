"""RF Monitor v2 - capture + analysis helpers for the signal characterizer.

The HackRF is shared with the always-on sweep. The collector holds an flock on
RF_HACKRF_LOCK only while a sweep pass runs (measured 0.79-0.82 s out of every
60 s), so a capture takes the same lock and runs inside the idle window instead
of stopping any service. If a sweep is in flight the capture waits for it.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import numpy as np

LOCK_PATH = Path(os.environ.get("RF_HACKRF_LOCK", "/tmp/hackrf-monitor-device.lock"))
HACKRF_TRANSFER = os.environ.get("HACKRF_TRANSFER", "hackrf_transfer")

# Narrow signals get a 2 MS/s window (cheapest that still sees +/-1 MHz around
# the 1 MHz sweep bin); wider blobs get 5 MS/s.
NARROW_RATE_HZ = int(os.environ.get("RF_NARROW_RATE_HZ", "2000000"))
WIDE_RATE_HZ = int(os.environ.get("RF_WIDE_RATE_HZ", "5000000"))
CAPTURE_SECONDS = float(os.environ.get("RF_CAPTURE_SECONDS", "4"))
LNA_GAIN_DB = int(os.environ.get("RF_LNA_GAIN_DB", "32"))
VGA_GAIN_DB = int(os.environ.get("RF_VGA_GAIN_DB", "24"))
AMP_ENABLE = int(os.environ.get("RF_AMP_ENABLE", "0"))
LO_OFFSET_NARROW_HZ = int(os.environ.get("RF_LO_OFFSET_NARROW_HZ", "250000"))
LO_OFFSET_WIDE_HZ = int(os.environ.get("RF_LO_OFFSET_WIDE_HZ", "600000"))
# The PortaPack's own leakage lands here; a capture on those frequencies would be
# the radio hearing itself. Half-width is deliberately generous (150 kHz).
SPUR_MASK_HZ: tuple[int, ...] = (89_000_000, 101_500_000)
SPUR_HALF_WIDTH_HZ = int(os.environ.get("RF_SPUR_HALF_WIDTH_HZ", "150000"))
# Separate flock for the second radio so a capture never waits on a sweep.
PORTAPACK_LOCK_PATH = Path(os.environ.get("RF_PORTAPACK_LOCK", "/tmp/rf-portapack-device.lock"))


@contextmanager
def device_lock(timeout_seconds: float = 120.0, path: Path | None = None) -> Iterator[None]:
    lock_path = Path(path) if path is not None else LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with open(lock_path, "w") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError("timed out waiting for the HackRF device lock")
                time.sleep(0.15)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def choose_sample_rate(bandwidth_hz: int) -> int:
    return WIDE_RATE_HZ if int(bandwidth_hz) > 1_500_000 else NARROW_RATE_HZ


def capture_hackrf(
    frequency_hz: int,
    out_iq: Path,
    *,
    duration_seconds: float = CAPTURE_SECONDS,
    sample_rate_hz: int = NARROW_RATE_HZ,
    lna_gain_db: int = LNA_GAIN_DB,
    vga_gain_db: int = VGA_GAIN_DB,
    amp_enable: int = AMP_ENABLE,
    device_serial: str | None = None,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    """Grab one IQ capture from a HackRF. Caller owns the returned meta.

    `device_serial` pins the radio. With two HackRFs attached (main + PortaPack)
    an unpinned capture gets whichever enumerated first, which is how a capture
    lane and the sweep end up fighting over one radio. `lock_path` picks the
    flock: the main radio shares `RF_HACKRF_LOCK` with the sweep, the PortaPack
    has its own so captures never wait on a sweep.
    """
    out_iq.parent.mkdir(parents=True, exist_ok=True)
    sample_count = int(duration_seconds * sample_rate_hz)
    command = [
        HACKRF_TRANSFER,
        "-r", str(out_iq),
        "-f", str(int(frequency_hz)),
        "-s", str(int(sample_rate_hz)),
        "-n", str(sample_count),
        "-a", str(int(amp_enable)),
        "-l", str(int(lna_gain_db)),
        "-g", str(int(vga_gain_db)),
    ]
    if device_serial:
        command += ["-d", str(device_serial)]
    started = datetime.now(timezone.utc)
    with device_lock(timeout_seconds=duration_seconds + 120, path=lock_path):
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=duration_seconds + 30
        )
    meta = {
        "started_at": started.isoformat(),
        "frequency_hz": int(frequency_hz),
        "sample_rate_hz": int(sample_rate_hz),
        "sample_count": sample_count,
        "duration_seconds": duration_seconds,
        "command": command,
        "returncode": result.returncode,
        "stderr_tail": (result.stderr or "")[-1500:],
    }
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-400:] or f"hackrf_transfer rc={result.returncode}")
    if not out_iq.exists() or out_iq.stat().st_size == 0:
        raise RuntimeError("hackrf_transfer produced no samples")
    meta["iq_bytes"] = out_iq.stat().st_size
    return meta


def read_iq(path: Path) -> np.ndarray:
    """hackrf_transfer writes SIGNED 8-bit interleaved IQ.

    v1 read the same files as unsigned and subtracted 127.5, which manufactures a
    constant DC of ~1.37x full scale (mean I ~ +126, mean Q ~ -124) and buries
    every real signal under that fake carrier. Signed is the correct format:
    residual DC then measures ~0.05, i.e. the normal HackRF LO leakage.
    """
    raw = np.fromfile(path, dtype=np.int8)
    if raw.size < 4096:
        raise RuntimeError("capture too short to analyse")
    raw = raw[: raw.size - (raw.size % 2)]
    iq = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
    return (iq / 128.0).astype(np.complex64)


def remove_dc(iq: np.ndarray) -> np.ndarray:
    """Block the LO/DC spike so it cannot masquerade as the strongest signal."""
    return (iq - iq.mean()).astype(np.complex64)


def _colorise(norm: np.ndarray) -> np.ndarray:
    img = np.zeros((norm.shape[0], norm.shape[1], 3), dtype=np.uint8)
    img[..., 0] = np.clip(255 * np.minimum(1, norm * 2.1), 0, 255)
    img[..., 1] = np.clip(255 * np.maximum(0, (norm - 0.22) * 1.55), 0, 255)
    img[..., 2] = np.clip(255 * np.maximum(0, 1.0 - norm * 2.6), 0, 255)
    return np.flipud(img)


def spectrogram_png(iq: np.ndarray, sample_rate_hz: int, center_hz: int, out_png: Path) -> dict[str, Any]:
    from PIL import Image, ImageDraw

    usable = iq[: min(iq.size, int(sample_rate_hz * CAPTURE_SECONDS))]
    nfft = 1024 if usable.size < 4096 else 2048
    hop = max(256, (usable.size - nfft) // 420)
    starts = np.arange(0, max(1, usable.size - nfft + 1), hop, dtype=np.int64)
    window = np.hanning(nfft).astype(np.float32)
    spectra = np.empty((nfft, starts.size), dtype=np.float32)
    for col, start in enumerate(starts):
        chunk = usable[start : start + nfft]
        if chunk.size < nfft:
            chunk = np.pad(chunk, (0, nfft - chunk.size))
        spectra[:, col] = np.abs(np.fft.fftshift(np.fft.fft(chunk * window))).astype(np.float32)
    power_db = 20 * np.log10(spectra + 1e-12)
    low = float(np.percentile(power_db, 5))
    high = float(np.percentile(power_db, 99.5))
    norm = np.clip((power_db - low) / max(1e-6, high - low), 0, 1)
    img = Image.fromarray(_colorise(norm), mode="RGB")
    resample = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
    img = img.resize((880, 380), resample)

    canvas = Image.new("RGB", (1000, 470), (16, 20, 24))
    canvas.paste(img, (96, 44))
    draw = ImageDraw.Draw(canvas)
    lo_mhz = (center_hz - sample_rate_hz / 2) / 1e6
    hi_mhz = (center_hz + sample_rate_hz / 2) / 1e6
    dur = usable.size / sample_rate_hz
    draw.text((16, 14), f"{center_hz/1e6:.3f} MHz  {dur:.1f}s @ {sample_rate_hz/1e6:.1f} MS/s", fill=(231, 237, 242))
    draw.text((16, 46), f"{hi_mhz:.3f}", fill=(156, 170, 181))
    draw.text((16, 408), f"{lo_mhz:.3f}", fill=(156, 170, 181))
    draw.text((96, 434), "0.0 s", fill=(156, 170, 181))
    draw.text((900, 434), f"{dur:.1f} s", fill=(156, 170, 181))
    draw.rectangle((95, 43, 977, 425), outline=(43, 53, 61))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    # A full-RGB waterfall compresses badly (~780 kB); a 64-colour palette keeps
    # the detail at roughly a fifth of the bytes, which matters against the 1 GB cap.
    try:
        canvas = canvas.convert("P", palette=Image.ADAPTIVE, colors=64)
    except Exception:
        pass
    canvas.save(out_png, optimize=True)
    return {"png": str(out_png), "png_bytes": out_png.stat().st_size, "frames": int(starts.size)}


def _fir_lowpass(samples: np.ndarray, sample_rate_hz: float, cutoff_hz: float, taps: int = 201) -> np.ndarray:
    """Windowed-sinc low pass. numpy only - scipy.signal on this host is ABI-broken
    against numpy 2.x (ImportError: numpy.core.multiarray failed to import)."""
    if samples.size < 32:
        return samples.astype(np.float32)
    taps = int(min(taps, max(11, (samples.size // 4) * 2 + 1)))
    if taps % 2 == 0:
        taps += 1
    n = np.arange(taps) - (taps - 1) / 2.0
    fc = min(0.45, float(cutoff_hz) / max(1.0, float(sample_rate_hz)))
    kernel = np.sinc(2.0 * fc * n) * np.hamming(taps)
    total = float(kernel.sum())
    if total == 0.0:
        return samples.astype(np.float32)
    kernel = (kernel / total).astype(np.float32)
    return np.convolve(samples, kernel, mode="same").astype(np.float32)


def _demodulate(iq: np.ndarray, sample_rate_hz: int, mode: str,
                offset_hz: float = 0.0) -> np.ndarray:
    """AM envelope / FM discriminator, decimated to 48 kHz, numpy only.

    `offset_hz` is the tune offset used by the capture (LO minus signal). The
    capture deliberately tunes off-centre to dodge the DC spike, so without
    mixing that offset out the 3.5 kHz NFM passband lands 250-600 kHz away from
    the signal of interest and the audio is the wrong station - or nothing.
    """
    if offset_hz:
        t = np.arange(iq.size, dtype=np.float64)
        iq = (iq * np.exp(-2j * np.pi * float(offset_hz) * t / float(sample_rate_hz))).astype(np.complex64)

    if mode == "am":
        audio = np.abs(iq).astype(np.float32)
        cutoff = 5_000.0
    elif mode in ("nfm", "wfm"):
        audio = np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32)
        cutoff = 3_500.0 if mode == "nfm" else 15_000.0
    else:
        raise ValueError(f"unsupported audio mode {mode}")

    target = 48_000
    factor = max(1, int(sample_rate_hz // target))
    if factor > 1:
        usable = audio[: audio.size - (audio.size % factor)]
        if usable.size:
            # boxcar decimation acts as the anti-alias filter
            audio = usable.reshape(-1, factor).mean(axis=1).astype(np.float32)
    decimated_rate = float(sample_rate_hz) / float(factor)
    if audio.size < 8:
        raise RuntimeError("capture too short for audio")
    audio = _fir_lowpass(audio, decimated_rate, cutoff)
    if abs(decimated_rate - target) > 1.0:
        duration = audio.size / decimated_rate
        count = max(1, int(duration * target))
        src = np.linspace(0.0, duration, audio.size, endpoint=False)
        dst = np.linspace(0.0, duration, count, endpoint=False)
        audio = np.interp(dst, src, audio).astype(np.float32)
    return audio


def write_wav(samples: np.ndarray, out_wav: Path, sample_rate_hz: int = 48_000) -> dict[str, Any]:
    import wave

    samples = samples.astype(np.float32)
    if samples.size == 0:
        raise RuntimeError("empty audio")
    samples = samples - float(np.mean(samples))
    peak = float(np.max(np.abs(samples)))
    if peak > 0:
        samples = samples / peak
    pcm = np.clip(samples * 0.85 * 32767, -32768, 32767).astype("<i2")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate_hz)
        handle.writeframes(pcm.tobytes())
    return {
        "wav": str(out_wav),
        "bytes": out_wav.stat().st_size,
        "duration_seconds": round(pcm.size / sample_rate_hz, 3),
    }


def _to_mp3(wav_path: Path, mp3_path: Path) -> None:
    """48 kHz mono WAV -> 64 kbps MP3 (~8 kB/s instead of 96 kB/s)."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-codec:a", "libmp3lame",
         "-b:a", "64k", "-ac", "1", "-ar", "48000", str(mp3_path)],
        check=True, capture_output=True, timeout=90,
    )


def audio_previews(iq: np.ndarray, sample_rate_hz: int, out_dir: Path,
                   offset_hz: float = 0.0) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for mode in ("am", "nfm", "wfm"):
        wav_path = out_dir / f"audio-{mode}.wav"
        try:
            audio = _demodulate(iq, sample_rate_hz, mode, offset_hz=offset_hz)
            info = write_wav(audio, wav_path)
            info["rms"] = round(float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))), 6)
            mp3_path = out_dir / f"audio-{mode}.mp3"
            try:
                _to_mp3(wav_path, mp3_path)
                info["mp3"] = str(mp3_path)
                info["mp3_bytes"] = mp3_path.stat().st_size
                wav_path.unlink(missing_ok=True)
                info.pop("wav", None)
                info.pop("bytes", None)
            except Exception as exc:  # keep the WAV if ffmpeg is unavailable
                info["mp3_error"] = str(exc)[:160]
            results[mode] = info
        except Exception as exc:  # one bad mode must not lose the capture
            results[mode] = {"error": str(exc)[:200]}
    return results


GAIN_LADDER: tuple[tuple[int, int], ...] = (
    (int(os.environ.get("RF_LNA_GAIN_DB", "32")), int(os.environ.get("RF_VGA_GAIN_DB", "24"))),
    (24, 16),
    (16, 16),
    (8, 8),
)
OVERLOAD_P99 = float(os.environ.get("RF_OVERLOAD_P99", "0.80"))


class SpurMaskedError(RuntimeError):
    """Target frequency sits inside the receiver's own spur mask."""


def signal_present(samples, min_db: float = 12.0) -> bool:
    """Cheap 'is something still there' test used for extend-on-progress."""
    seg = np.asarray(samples, dtype=np.complex64)
    if seg.size < 4096:
        return False
    window = seg[: (seg.size // 4096) * 4096].reshape(-1, 4096)
    spec = np.abs(np.fft.fftshift(np.fft.fft(window, axis=1))).mean(axis=0)
    db = 20.0 * np.log10(np.where(spec > 0, spec, 1e-12))
    return bool(db.max() - np.median(db) >= min_db)


def capture_and_analyse(
    episode: dict[str, Any],
    out_dir: Path,
    *,
    keep_iq: bool = False,
    decode_cb=None,
    duration_seconds: float | None = None,
    max_duration_seconds: float | None = None,
    device_serial: str | None = None,
    lock_path: Path | None = None,
    spur_mask_hz: Iterable[int] = (),
) -> dict[str, Any]:
    """Capture one episode, write spectrogram + audio previews, return the bundle.

    `decode_cb(iq, sample_rate_hz, tune_hz, out_dir)` runs while the samples are
    still in memory, so the decode stage sees full-rate IQ instead of the 48 kHz
    audio artifact. Its return value lands in bundle["decode"].

    `max_duration_seconds` > `duration_seconds` turns on extend-on-progress: the
    capture keeps adding chunks while the signal is still on the air, which is
    what a 4 s grab cannot do for a slow beacon or a data burst that starts late.
    `spur_mask_hz` refuses frequencies where the receiver's own leakage would be
    mistaken for a signal (the PortaPack spurs at ~89.0 and ~101.5 MHz).
    """
    from rf_signal_intel import extract_iq_features

    frequency_hz = int(episode["freq_hz"])
    for spur in spur_mask_hz or ():
        if abs(frequency_hz - int(spur)) <= SPUR_HALF_WIDTH_HZ:
            raise SpurMaskedError(
                f"{frequency_hz / 1e6:.3f} MHz is within {SPUR_HALF_WIDTH_HZ // 1000} kHz of the "
                f"{int(spur) / 1e6:.1f} MHz receiver spur - not capturable on this radio"
            )
    sample_rate_hz = choose_sample_rate(int(episode.get("bandwidth_hz") or 1_000_000))
    # Tune off-centre so the signal of interest is never sitting on the DC spike.
    offset_hz = LO_OFFSET_WIDE_HZ if sample_rate_hz >= WIDE_RATE_HZ else LO_OFFSET_NARROW_HZ
    tune_hz = frequency_hz + offset_hz
    if tune_hz + sample_rate_hz / 2 > 6_000_000_000:
        tune_hz = frequency_hz - offset_hz
    out_dir.mkdir(parents=True, exist_ok=True)
    iq_path = out_dir / "capture.iq"
    # A capture killed mid-dwell (service restart, SIGTERM) leaves its chunk files
    # behind - 4 MB/s of disk each, and they count against the artifact cap.
    for stale in out_dir.glob("capture.part*.iq"):
        stale.unlink(missing_ok=True)

    base_seconds = float(duration_seconds or CAPTURE_SECONDS)
    max_seconds = max(base_seconds, float(max_duration_seconds or base_seconds))

    # First chunk doubles as gain selection: step down while the front end is
    # anywhere near full scale.
    parts: list[Path] = []
    meta: dict[str, Any] = {}
    gains = GAIN_LADDER[0]
    first_p99: float | None = None
    for attempt, (lna, vga) in enumerate(GAIN_LADDER):
        part = out_dir / f"capture.part{attempt}.iq"
        meta = capture_hackrf(tune_hz, part, duration_seconds=base_seconds,
                              sample_rate_hz=sample_rate_hz, lna_gain_db=lna, vga_gain_db=vga,
                              device_serial=device_serial, lock_path=lock_path)
        chunk_iq = remove_dc(read_iq(part))
        p99 = float(np.percentile(np.abs(chunk_iq), 99))
        first_p99 = p99
        if p99 <= OVERLOAD_P99 or attempt == len(GAIN_LADDER) - 1:
            gains = (lna, vga)
            parts.append(part)
            break
        print(f"overload p99={p99:.2f} at lna={lna}/vga={vga}; stepping gain down", flush=True)
        part.unlink(missing_ok=True)

    # Extend-on-progress: keep dwelling while the signal keeps showing up.
    captured_seconds = base_seconds
    while captured_seconds + base_seconds <= max_seconds:
        tail = remove_dc(read_iq(parts[-1]))[-int(sample_rate_hz * 2):]
        if not signal_present(tail):
            break
        # The gain ladder already consumed part0..partN, so len(parts) can name a
        # chunk that is still in `parts` (attempt 1 clipped -> parts == [part1]).
        # Reusing that name made the concatenation below read, unlink and then
        # re-read the same file: FileNotFoundError, capture lost. Always take a
        # name that is not on disk.
        index = len(parts)
        while (out_dir / f"capture.part{index}.iq").exists():
            index += 1
        part = out_dir / f"capture.part{index}.iq"
        meta = capture_hackrf(tune_hz, part, duration_seconds=base_seconds,
                              sample_rate_hz=sample_rate_hz, lna_gain_db=gains[0],
                              vga_gain_db=gains[1], device_serial=device_serial, lock_path=lock_path)
        parts.append(part)
        captured_seconds += base_seconds

    if len(parts) == 1:
        parts[0].replace(iq_path)
    else:
        with open(iq_path, "wb") as handle:
            for part in dict.fromkeys(parts):   # dedupe: one read per file
                if not part.exists():
                    raise RuntimeError(f"capture chunk vanished before concat: {part.name}")
                handle.write(part.read_bytes())
                part.unlink(missing_ok=True)
    iq = remove_dc(read_iq(iq_path))
    meta["dwell_seconds"] = round(captured_seconds, 2)
    # The extend loop replaces `meta` with the last chunk's, so the first chunk's
    # overload measurement and the gains that survived are re-applied here.
    meta["p99_amplitude"] = round(first_p99, 4) if first_p99 is not None else None
    meta["gains"] = {"lna_db": gains[0], "vga_db": gains[1]}
    meta["target_hz"] = frequency_hz
    meta["lo_offset_hz"] = tune_hz - frequency_hz
    bundle: dict[str, Any] = {"capture": meta, "artifacts": []}
    try:
        features = extract_iq_features(iq, sample_rate_hz, tune_hz)
        bundle["features"] = features
        bundle["artifacts"].append(spectrogram_png(iq, sample_rate_hz, tune_hz, out_dir / "spectrum.png"))
        bundle["audio"] = audio_previews(iq, sample_rate_hz, out_dir,
                                         offset_hz=float(meta.get("lo_offset_hz") or 0.0))
        if decode_cb is not None:
            try:
                bundle["decode"] = decode_cb(iq, sample_rate_hz, tune_hz, out_dir)
            except Exception as exc:  # a decoder fault must never lose a capture
                bundle["decode"] = {"error": str(exc)[:300]}
        bundle["samples"] = int(iq.size)
        bundle["duration_seconds"] = round(iq.size / sample_rate_hz, 3)
        bundle["mean_power_dbfs"] = features.get("mean_power_dbfs")
    finally:
        if not keep_iq:
            try:
                iq_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            bundle.setdefault("artifacts", []).append(
                {"iq": str(iq_path), "iq_bytes": iq_path.stat().st_size if iq_path.exists() else 0}
            )

    (out_dir / "capture.json").write_text(json.dumps(bundle, indent=2, default=str))
    return bundle
