#!/usr/bin/env python3
"""RF Monitor v2 - decode stage.

The scout captures an episode (spectrogram + AM/NFM/WFM audio) and stops. This
module is the missing second half: it runs a ladder of protocol decoders against
that capture and, when one produces a valid frame, calls
``signals_store.mark_decoded()`` so the episode lands in the dashboard's
VOICE / ISM / AIRCRAFT-APRS-PAGER / SATELLITE sections.

Two entry points:

* ``decode_audio_file()``  - offline, from the audio artifact the capture already
  wrote. This is what the backfill uses, so the 1281 episodes already on disk can
  be classified without touching the radio.
* ``decode_iq()``          - live, from the in-memory IQ right after capture,
  while the samples are still at full rate (better than the 48 kHz audio
  artifact). Used by the scout and, for rtl_433, for ISM-band episodes only.

Design rules that keep this honest:

1. A decoder either produces a checksummed frame or it does not. ``multimon-ng``
   protocols are used here only where that is true - AFSK1200 (AX.25 FCS),
   POCSAG/FLEX (BCH FEC), EAS (SAME preamble + header). Protocols that fire on
   noise (MORSE_CW, DTMF) are deliberately *not* in the ladder, because a
   decoded row in the dashboard is a claim about the content.
2. Every attempt is written to ``decode_attempts`` whether or not it succeeds,
   so a re-run can skip work and an operator can see what was tried.
3. Failure is not an error: an episode that decodes to nothing keeps its
   ``captured`` status. Only a real frame changes ``decode_type``.

Usage (on linuxGR, from /home/cmilkosk/rf-monitor):

    python3 rf_decode.py --stats                 # what is installed / pending
    python3 rf_decode.py --backfill --limit 50   # decode 50 un-attempted episodes
    python3 rf_decode.py --backfill              # all of them
    python3 rf_decode.py --episode 1897          # one episode
    python3 rf_decode.py --backfill --dry-run    # report, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import signals_store as store

LOG_PREFIX = "rf_decode"

# --------------------------------------------------------------------------- #
# decoders
# --------------------------------------------------------------------------- #
# multimon-ng protocols we trust to mean something. Every one of these carries
# its own error detection, so a hit is a real frame rather than a lucky guess.
MULTIMON_PROTOCOLS: tuple[str, ...] = (
    "AFSK1200",     # AX.25 / APRS 1200 Bd
    "AFSK2400",     # AX.25 2400 Bd variants
    "AFSK2400_2",
    "AFSK2400_3",
    "UFSK1200",     # 1200 Bd FSK (utility/telemetry)
    "HAPN4800",     # 4800 Bd packet
    "FSK9600",      # 9600 Bd packet
    "POCSAG512",    # paging
    "POCSAG1200",
    "POCSAG2400",
    "FLEX",         # paging
    "EAS",          # NOAA weather alert (SAME)
)

# multimon-ng tag -> (decode_type, human label). decode_type must be one of
# signals_store.DECODE_TYPES or the dashboard filter will not see it.
PROTOCOL_TYPES: dict[str, tuple[str, str]] = {
    "AFSK1200": ("aprs", "APRS / AX.25 1200 Bd packet"),
    "AFSK2400": ("aprs", "AX.25 2400 Bd packet"),
    "AFSK2400_2": ("aprs", "AX.25 2400 Bd packet"),
    "AFSK2400_3": ("aprs", "AX.25 2400 Bd packet"),
    "UFSK1200": ("aprs", "1200 Bd FSK data"),
    "HAPN4800": ("aprs", "4800 Bd packet"),
    "FSK9600": ("aprs", "9600 Bd packet"),
    "POCSAG512": ("pager", "POCSAG 512 pager"),
    "POCSAG1200": ("pager", "POCSAG 1200 pager"),
    "POCSAG2400": ("pager", "POCSAG 2400 pager"),
    "FLEX": ("pager", "FLEX pager"),
    "EAS": ("voice", "NOAA EAS / SAME weather alert"),
}

# multimon-ng wants 22050 Hz signed-16-bit mono for the 1200 Bd decoders.
AUDIO_RATE = 22050

# rtl_433 only makes sense where its protocols live. Tuned to the bands the
# HackRF can actually reach with this antenna.
ISM_BANDS_HZ: tuple[tuple[int, int, str], ...] = (
    (314_000_000, 316_000_000, "315 MHz ISM"),
    (344_000_000, 346_000_000, "345 MHz ISM"),
    (433_000_000, 435_000_000, "433.92 MHz ISM"),
    (868_000_000, 870_000_000, "868 MHz ISM"),
    (902_000_000, 928_000_000, "915 MHz ISM"),
)


def which(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def tools_status() -> dict[str, str | None]:
    return {
        "ffmpeg": which("ffmpeg"),
        "sox": which("sox"),
        "multimon-ng": which("multimon-ng"),
        "rtl_433": which("rtl_433"),
        "direwolf": which("direwolf"),
        "readsb": which("readsb", "dump1090-fa", "dump1090"),
        "satdump": which("satdump"),
        "acarsdec": which("acarsdec"),
        "redsea": which("redsea"),
    }


def ism_band(freq_hz: int) -> str | None:
    for low, high, label in ISM_BANDS_HZ:
        if low <= int(freq_hz) <= high:
            return label
    return None


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
def _hit(decoder: str, protocol: str, line: str, freq_hz: int) -> dict[str, Any]:
    decode_type, label = PROTOCOL_TYPES.get(protocol, ("data", protocol))
    return {
        "decoder": decoder,
        "protocol": protocol,
        "decode_type": decode_type,
        "label": label,
        "line": line.strip(),
        "freq_hz": int(freq_hz),
    }


_HIT_RE = re.compile(r"^([A-Z][A-Z0-9_]+):\s*(.+)$")

# multimon-ng emits synthetic frames on noise - "fm ? to ;-</UO-9 QSO Nr 2483" is
# not a packet anyone sent, it is the decoder guessing at random bits. Two of them
# reached the dashboard as APRS before this filter existed. A real AX.25 frame has
# a callsign on both sides of "to".
_AX25_HEADER_RE = re.compile(r"\bfm\s+([A-Z0-9]{1,6}(?:-\d{1,2})?)\s+to\s+([A-Z0-9]{1,6}(?:-\d{1,2})?)\b")


def ax25_header_valid(line: str) -> bool:
    """True when the frame carries a plausible AX.25 source and destination."""
    return bool(_AX25_HEADER_RE.search(line))


def parse_multimon(output: str, freq_hz: int) -> list[dict[str, Any]]:
    """Keep only frames from protocols in PROTOCOL_TYPES; drop the rest.

    multimon-ng prints an AX.25 header line tagged with the protocol and then the
    info field on an *untagged* following line, so continuation lines are folded
    into the frame they belong to. Without that the APRS payload - the only part
    an operator cares about - is lost.
    """
    hits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _HIT_RE.match(line)
        if match:
            protocol = match.group(1)
            if protocol not in PROTOCOL_TYPES:
                current = None
                continue
            current = _hit("multimon-ng", protocol, line, freq_hz)
            if current["decode_type"] == "aprs" and not ax25_header_valid(line):
                current = None
                continue
            hits.append(current)
        elif current is not None:
            current["line"] = f"{current['line']} {line}".strip()
    return hits


# --------------------------------------------------------------------------- #
# audio path (offline; what the backfill uses)
# --------------------------------------------------------------------------- #
def audio_to_raw_22050(src: Path) -> bytes:
    """Decode any audio artifact to the 22050 Hz s16le mono that multimon wants."""
    ffmpeg = which("ffmpeg")
    if ffmpeg:
        cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-i", str(src),
               "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(AUDIO_RATE), "-"]
    else:
        sox = which("sox")
        if not sox:
            raise RuntimeError("neither ffmpeg nor sox available to resample audio")
        cmd = [sox, str(src), "-t", "raw", "-e", "signed-integer", "-b", "16",
               "-r", str(AUDIO_RATE), "-c", "1", "-"]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"audio convert failed rc={result.returncode}: "
                           f"{result.stderr.decode('utf-8', 'replace')[-200:]}")
    return result.stdout


def run_multimon(raw: bytes, freq_hz: int, timeout: float = 120.0) -> list[dict[str, Any]]:
    exe = which("multimon-ng")
    if not exe:
        raise RuntimeError("multimon-ng not installed")
    cmd = [exe, "-t", "raw", "-q"]
    for protocol in MULTIMON_PROTOCOLS:
        cmd += ["-a", protocol]
    cmd.append("-")
    result = subprocess.run(cmd, input=raw, capture_output=True, timeout=timeout)
    stdout = result.stdout.decode("utf-8", "replace")
    return parse_multimon(stdout, freq_hz)


def decode_audio_file(path: Path | str, freq_hz: int) -> list[dict[str, Any]]:
    """Decode one audio artifact. Returns hits (empty list = nothing there)."""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(str(src))
    return run_multimon(audio_to_raw_22050(src), freq_hz)


# --------------------------------------------------------------------------- #
# IQ path (live; full-rate samples right after capture)
# --------------------------------------------------------------------------- #
def iq_to_cu8(iq, out_path: Path) -> dict[str, Any]:
    """HackRF IQ (complex float, -1..1) -> rtl_433's cu8 interleaved bytes.

    rtl_433 wants 8-bit unsigned samples centred on 128 as I,Q,I,Q... Casting a
    complex array to float (the old code) silently dropped Q and handed rtl_433
    a stream of I samples at twice the real rate, so nothing could ever decode.
    """
    import numpy as np

    arr = np.asarray(iq, dtype=np.complex64)
    interleaved = np.empty(arr.size * 2, dtype=np.float32)
    interleaved[0::2] = arr.real
    interleaved[1::2] = arr.imag
    u8 = np.clip(np.round(interleaved * 127.0 + 128.0), 0, 255).astype(np.uint8)
    u8.tofile(str(out_path))
    return {"path": str(out_path), "bytes": out_path.stat().st_size, "complex_samples": int(arr.size)}


def decode_iq(iq, sample_rate_hz: int, center_hz: int, freq_hz: int) -> list[dict[str, Any]]:
    """Decode from full-rate IQ.

    Only rtl_433 is attempted here: it is the one decoder that wants raw IQ
    rather than audio, and it only covers the ISM sensor bands. Everything else
    in the ladder works from demodulated audio (see decode_audio_file).
    """
    exe = which("rtl_433")
    if not exe:
        return []
    band = ism_band(freq_hz)
    if not band:
        return []
    hits: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="rf-decode-") as tmp:
        cu8 = Path(tmp) / "capture.cu8"
        iq_to_cu8(iq, cu8)
        cmd = [exe, "-r", str(cu8), "-s", str(int(sample_rate_hz)),
               "-f", str(int(center_hz)), "-F", "json", "-M", "level"]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            return []
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("_type") == "error":
                continue
            model = record.get("model") or "ISM device"
            ident = record.get("id")
            summary = f"{band}: {model}" + (f" id={ident}" if ident is not None else "")
            hits.append({
                "decoder": "rtl_433",
                "protocol": f"rtl_433:{model}",
                "decode_type": "ism",
                "label": f"{band} sensor ({model})",
                "line": json.dumps(record, sort_keys=True)[:800],
                "freq_hz": int(record.get("freq") or freq_hz),
                "summary": summary,
            })
    return hits


def _fir_lowpass(samples, sample_rate_hz: float, cutoff_hz: float, taps: int = 201):
    """Windowed-sinc low pass, numpy only (no scipy on the radio host)."""
    import numpy as np

    samples = np.asarray(samples, dtype=np.float32)
    if samples.size < 32:
        return samples
    taps = int(min(taps, max(11, (samples.size // 4) * 2 + 1)))
    if taps % 2 == 0:
        taps += 1
    n = np.arange(taps) - (taps - 1) / 2.0
    fc = min(0.45, float(cutoff_hz) / max(1.0, float(sample_rate_hz)))
    kernel = np.sinc(2.0 * fc * n) * np.hamming(taps)
    total = float(kernel.sum())
    if total == 0.0:
        return samples
    kernel = (kernel / total).astype(np.float32)
    return np.convolve(samples, kernel, mode="same").astype(np.float32)


def ddc_audio(iq, sample_rate_hz: int, center_hz: int, target_hz: int,
              out_rate: int = AUDIO_RATE, channel_hz: float = 10_000.0,
              audio_cutoff_hz: float = 4_000.0) -> bytes:
    """Mix the target to baseband, FM-discriminate, resample - numpy only.

    This is the piece the v2 audio artifacts were missing: the capture tunes
    off-centre, so a decoder that just discriminates the raw IQ hears the LO
    centre, not the signal. Returns s16le mono at `out_rate`, ready for
    multimon-ng's stdin.
    """
    import numpy as np

    fs = float(sample_rate_hz)
    x = np.asarray(iq)
    # Signal at target sits at (target - center) in the captured baseband, so the
    # correction is a shift by (center - target). Getting this backwards tunes to
    # the mirror frequency: measured on a real capture, WX7 at 162.550 with the LO
    # at 162.475 appears at +74.8 kHz, and only exp(-j*2pi*74.8k*t) brings it to DC.
    offset = float(target_hz) - float(center_hz)
    if abs(offset) > 1e-6:
        t = np.arange(x.size, dtype=np.float64)
        x = (x * np.exp(-2j * np.pi * offset * t / fs)).astype(np.complex64)

    # Coarse decimation first: keeps the FIR cheap on multi-million-sample captures.
    factor = max(1, int(fs // 96_000.0))
    if factor > 1:
        usable = x.size - (x.size % factor)
        x = x[:usable].reshape(-1, factor).mean(axis=1)
    rate = fs / factor

    # Channel filter around baseband, then the discriminator.
    if channel_hz < rate / 2.0:
        x = _fir_lowpass(x.real, rate, channel_hz) + 1j * _fir_lowpass(x.imag, rate, channel_hz)
    if x.size < 16:
        raise RuntimeError("capture too short to decode")
    audio = np.angle(x[1:] * np.conj(x[:-1])).astype(np.float32)
    audio_rate = rate

    # Audio low pass, decimate toward out_rate, then interpolate to it exactly.
    audio = _fir_lowpass(audio, audio_rate, audio_cutoff_hz)
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
    pcm = np.clip(audio / peak * 0.85 * 32767.0, -32768, 32767).astype("<i2")
    return pcm.tobytes()


def decode_iq_hits(iq, sample_rate_hz: int, center_hz: int, target_hz: int) -> list[dict[str, Any]]:
    """Full live decode: DDC -> multimon-ng, plus rtl_433 for ISM bands."""
    hits: list[dict[str, Any]] = []
    try:
        hits.extend(run_multimon(ddc_audio(iq, sample_rate_hz, center_hz, target_hz), target_hz))
    except Exception:
        pass
    if not hits:
        hits.extend(decode_iq(iq, sample_rate_hz, center_hz, target_hz))
    return hits


# --------------------------------------------------------------------------- #
# episode integration
# --------------------------------------------------------------------------- #
def summarise(hits: list[dict[str, Any]], limit: int = 3) -> str:
    """One-line operator-facing summary of the best hits."""
    parts: list[str] = []
    for hit in hits[:limit]:
        text = hit.get("summary") or hit["line"]
        text = re.sub(r"\s+", " ", text).strip()
        parts.append(f"[{hit['label']}] {text[:180]}")
    if len(hits) > limit:
        parts.append(f"+{len(hits) - limit} more frame(s)")
    return " | ".join(parts)


def decode_episode(
    episode: dict[str, Any],
    artifacts: Iterable[dict[str, Any]] | None = None,
    *,
    iq=None,
    sample_rate_hz: int | None = None,
    center_hz: int | None = None,
    audio_modes: tuple[str, ...] = ("nfm", "am", "wfm"),
    pre_hits: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the ladder for one episode and record the outcome.

    Returns {"episode_id", "status", "decode_type", "summary", "hits", "tried"}.
    status is "decoded" when a frame was produced, else "no_decode".
    `pre_hits` lets a caller that already decoded from live IQ skip re-decoding.
    """
    episode_id = int(episode["id"])
    freq_hz = int(episode["freq_hz"])
    started = store.utcnow()
    hits: list[dict[str, Any]] = list(pre_hits or [])
    tried: list[str] = []
    errors: list[str] = []

    # 1) full-rate IQ, if the caller had the samples in hand
    if iq is not None and sample_rate_hz and not hits:
        tried.append("rtl_433")
        t0 = time.monotonic()
        try:
            hits.extend(decode_iq(iq, sample_rate_hz, center_hz or freq_hz, freq_hz))
        except Exception as exc:  # a decoder fault must never kill the capture
            errors.append(f"rtl_433: {exc}"[:300])
        _record(episode_id, "rtl_433", "ok" if hits else "no_frame",
                f"{time.monotonic() - t0:.2f}s {errors[-1] if errors else ''}".strip(),
                started, dry_run)

    # 2) the audio artifacts the capture already wrote
    by_kind = {str(a["kind"]): a for a in (artifacts or []) if a.get("kind")}
    for mode in audio_modes:
        if hits:
            break
        art = by_kind.get(f"audio-{mode}")
        if not art:
            continue
        path = Path(str(art["path"]))
        if not path.exists():
            continue
        tried.append(f"multimon-ng:{mode}")
        t0 = time.monotonic()
        try:
            found = decode_audio_file(path, freq_hz)
        except Exception as exc:
            errors.append(f"multimon-ng/{mode}: {exc}"[:300])
            _record(episode_id, f"multimon-ng/{mode}", "error", errors[-1], started, dry_run)
            continue
        hits.extend(found)
        _record(episode_id, f"multimon-ng/{mode}", "ok" if found else "no_frame",
                f"{len(found)} frame(s) in {time.monotonic() - t0:.2f}s", started, dry_run)

    if hits:
        decode_type = hits[0]["decode_type"]
        summary = summarise(hits)
        if not dry_run:
            store.mark_decoded(episode_id, decode_type, summary)
        return {"episode_id": episode_id, "status": "decoded", "decode_type": decode_type,
                "summary": summary, "hits": hits, "tried": tried, "errors": errors}

    if not dry_run and tried:
        # Nothing decoded: record that honestly. Leaving it at 'captured' made a
        # negative result indistinguishable from a row nobody had ever tried.
        store.set_decode_status(episode_id, "no_frame")
    return {"episode_id": episode_id, "status": "no_decode", "decode_type": None,
            "summary": None, "hits": [], "tried": tried, "errors": errors}


def _record(episode_id: int, decoder: str, status: str, detail: str,
            started: str, dry_run: bool) -> None:
    if dry_run:
        return
    try:
        store.record_attempt(episode_id, decoder, status, detail, started)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# backfill
# --------------------------------------------------------------------------- #
def pending_episodes(limit: int | None = None, *, since: str | None = None,
                     retry: bool = False) -> list[dict[str, Any]]:
    """Captured episodes that have no decode_type and (by default) no attempts."""
    sql = (
        "SELECT e.* FROM episodes e WHERE e.decode_type IS NULL"
        " AND e.capture_id IS NOT NULL"
        # 'unavailable' = captured once, but its audio was evicted by the artifact cap
        # before any decoder saw it. Nothing can decode it, so it is not "pending".
        " AND COALESCE(e.decode_status,'none') != 'unavailable'"
    )
    params: list[Any] = []
    if not retry:
        sql += " AND NOT EXISTS (SELECT 1 FROM decode_attempts d WHERE d.episode_id = e.id)"
    if since:
        sql += " AND e.first_seen >= ?"
        params.append(since)
    sql += " ORDER BY e.score DESC, e.last_seen DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with store.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def artifacts_for(episode_id: int) -> list[dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT kind, path, bytes FROM artifacts WHERE episode_id=? AND purged=0",
            (int(episode_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def backfill(limit: int | None = None, *, since: str | None = None, retry: bool = False,
             dry_run: bool = False, verbose: bool = True) -> dict[str, Any]:
    episodes = pending_episodes(limit, since=since, retry=retry)
    started_at = store.utcnow()
    counts: dict[str, int] = {"decoded": 0, "no_decode": 0, "skipped": 0}
    by_type: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    t0 = time.monotonic()
    for index, episode in enumerate(episodes, 1):
        artifacts = artifacts_for(int(episode["id"]))
        if not any(str(a["kind"]).startswith("audio-") for a in artifacts):
            counts["skipped"] += 1
            continue
        result = decode_episode(episode, artifacts, dry_run=dry_run)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        if result["status"] == "decoded":
            by_type[result["decode_type"]] = by_type.get(result["decode_type"], 0) + 1
            if len(examples) < 20:
                examples.append({"episode_id": result["episode_id"],
                                 "freq_mhz": round(int(episode["freq_hz"]) / 1e6, 3),
                                 "decode_type": result["decode_type"],
                                 "summary": result["summary"]})
        if verbose and (index % 50 == 0 or result["status"] == "decoded"):
            elapsed = time.monotonic() - t0
            print(f"{LOG_PREFIX}: {index}/{len(episodes)} decoded={counts['decoded']} "
                  f"empty={counts['no_decode']} {elapsed:.0f}s", flush=True)
    report = {
        "started_at": started_at,
        "finished_at": store.utcnow(),
        "considered": len(episodes),
        "counts": counts,
        "by_type": by_type,
        "seconds": round(time.monotonic() - t0, 1),
        "examples": examples,
        "dry_run": dry_run,
    }
    return report


def stats() -> dict[str, Any]:
    with store.connect() as conn:
        total = conn.execute("SELECT count(*) FROM episodes").fetchone()[0]
        captured = conn.execute("SELECT count(*) FROM episodes WHERE capture_id IS NOT NULL").fetchone()[0]
        decoded = conn.execute("SELECT count(*) FROM episodes WHERE decode_type IS NOT NULL").fetchone()[0]
        attempts = conn.execute("SELECT count(*) FROM decode_attempts").fetchone()[0]
        by_type = conn.execute(
            "SELECT decode_type, count(*) FROM episodes WHERE decode_type IS NOT NULL"
            " GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        pending = len(pending_episodes())
    return {
        "episodes": total, "captured": captured, "decoded": decoded,
        "attempt_rows": attempts, "pending_backfill": pending,
        "by_type": {row[0]: row[1] for row in by_type},
        "tools": tools_status(),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RF Monitor v2 decode stage")
    parser.add_argument("--stats", action="store_true", help="print decoder + DB status")
    parser.add_argument("--backfill", action="store_true", help="decode un-decoded episodes")
    parser.add_argument("--episode", type=int, help="decode a single episode id")
    parser.add_argument("--limit", type=int, help="max episodes for --backfill")
    parser.add_argument("--since", help="only episodes first seen at/after this UTC stamp")
    parser.add_argument("--retry", action="store_true", help="re-try episodes that were attempted before")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    store.init_db()

    if args.stats:
        report: dict[str, Any] = stats()
    elif args.episode is not None:
        episode = store.get_episode(args.episode)
        if not episode:
            print(f"{LOG_PREFIX}: no episode {args.episode}", file=sys.stderr)
            return 2
        report = decode_episode(episode, artifacts_for(args.episode), dry_run=args.dry_run)
    elif args.backfill:
        report = backfill(args.limit, since=args.since, retry=args.retry, dry_run=args.dry_run)
    else:
        parser.print_help()
        return 1

    print(json.dumps(report, indent=2, default=str) if args.json else json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
