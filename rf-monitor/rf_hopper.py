#!/usr/bin/env python3
"""RF Monitor v2 - hopper lane: one parked RTL, every watchlist frequency.

The watchlist records intent ("this frequency matters"); the hopper is what
actually serves it. RTL2 hears ~2.4 MHz at a time, and the watchlist spans
144.390 MHz to 930.0 MHz - 786 MHz apart, so no single tuning serves the list.
So the radio hops: dwell on one frequency, decode, retune, next.

    capture (rtl_sdr, unsigned cu8)  ->  DDC the watched frequency to baseband
        ->  multimon-ng  ->  episode, but only when something really decodes

What this is not: a carrier detector. A carrier with no decode produces no
episode, because the sweep already logs carriers and a pager carrier that cannot
be demodulated is not evidence of a message. Only validated decodes (AX.25 FCS,
POCSAG BCH) become episodes.

ADS-B entries are skipped on purpose - readsb owns that radio and tar1090 needs
the whole 2 MHz around 1090 continuously.

    python3 rf_hopper.py --list
    python3 rf_hopper.py --once
    python3 rf_hopper.py --loop
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

import rf_decode
import rf_wideband_lane
import rf_watchlist
import signals_store as store

RADIO = os.environ.get("RF_HOPPER_RADIO", "rtl-2")
DEVICE = os.environ.get("RF_HOPPER_DEVICE", "1")
DWELL_SECONDS = float(os.environ.get("RF_HOPPER_DWELL_SECONDS", "15"))
SAMPLE_RATE_HZ = int(float(os.environ.get("RF_HOPPER_SAMPLE_RATE", "2400000")))
INTERVAL_SECONDS = float(os.environ.get("RF_HOPPER_INTERVAL", "5"))

# 100 kHz off centre: the R820T's DC spike sits exactly where a centred signal
# would be, and it is loud enough to bury a POCSAG carrier. DDC undoes the offset.
DC_OFFSET_HZ = 100_000

# Decoders that own a radio outright and must not be hopped.
EXCLUDED_DECODERS = {"adsb"}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_rtl_iq(path: Path) -> np.ndarray:
    """RTL-SDR writes UNSIGNED 8-bit IQ - the opposite of HackRF's signed cu8.

    Deliberately not reusing rf_v2_capture.read_iq: that reader assumes signed
    bytes, which is correct for hackrf_transfer and wrong here. Reading an RTL
    capture as signed folds the spectrum and manufactures a full-scale DC term.
    """
    raw = np.fromfile(path, dtype=np.uint8)
    if raw.size < 4096:
        raise RuntimeError("capture too short to analyse")
    raw = raw[: raw.size - (raw.size % 2)]
    iq = ((raw[0::2].astype(np.float32) - 127.5)
          + 1j * (raw[1::2].astype(np.float32) - 127.5))
    return (iq / 128.0).astype(np.complex64)


def select_entries(radio: str = RADIO) -> list[dict[str, Any]]:
    """Enabled parked entries this radio can serve, ADS-B excluded.

    Entries with no radio assigned are adopted: they were recorded when no
    parked radio was free, and this lane is that radio arriving.
    """
    out = []
    for entry in rf_watchlist.list_entries(enabled_only=True):
        if (entry.get("mode") or "parked") != "parked":
            continue
        if (entry.get("decoder") or "") in EXCLUDED_DECODERS:
            continue
        assigned = entry.get("radio") or ""
        if assigned and assigned != radio:
            continue
        out.append(entry)
    return out


def adopt(entry: dict[str, Any], radio: str = RADIO) -> None:
    """Write the radio assignment once this lane can actually serve the entry."""
    if entry.get("radio") == radio:
        return
    with store.connect() as conn:
        conn.execute("UPDATE watchlist SET radio=? WHERE id=?", (radio, int(entry["id"])))
        conn.commit()


def channel_power_snr(iq: np.ndarray, sample_rate_hz: int, center_hz: int, freq_hz: int,
                      seg: int = 1 << 16, segments: int = 8,
                      half_width_hz: float = 6_000.0,
                      floor_span_hz: float = 50_000.0) -> float:
    """Mean power in the channel vs the median of its local neighbourhood.

    Deliberately NOT rf_wideband_lane.channel_snr, which compares the single MAX
    bin in the channel against the neighbourhood median. That is fine for gating a
    wideband pass, where a real carrier is 20-60 dB up, but it is biased on a
    single-channel dwell: at 2.4 MS/s the channel spans ~330 noise bins, and the
    maximum of 330 exponentially distributed bins sits ~10 dB above the median for
    free. Measured 2026-10-28 the wideband metric reported 15.9 dB "SNR" at
    144.390 MHz on pure noise, while rtl_power on the same device saw +0.2 dB and
    a mean-based read of the same capture gave -0.2 dB.

    Averaging the channel bins instead of taking the max removes that bias, so a
    quiet channel reads ~0 dB and a real carrier still reads high.
    """
    n = iq.size
    if n < seg:
        return 0.0
    window = np.hanning(seg).astype(np.float32)
    starts = np.linspace(0, max(0, n - seg), num=segments).astype(int)
    psd = np.zeros(seg, dtype=np.float64)
    used = 0
    for start in starts:
        chunk = iq[start:start + seg]
        if chunk.size < seg:
            continue
        psd += np.abs(np.fft.fft(chunk * window)) ** 2
        used += 1
    if not used:
        return 0.0
    psd /= used
    bin_hz = float(sample_rate_hz) / seg
    half_bins = max(1, int(round(half_width_hz / bin_hz)))
    floor_bins = max(half_bins + 1, int(round(floor_span_hz / bin_hz)))
    offset_bins = int(round((freq_hz - center_hz) / bin_hz))
    near = (offset_bins + np.arange(-floor_bins, floor_bins + 1)) % seg
    keep = np.ones(near.size, dtype=bool)
    keep[max(0, floor_bins - half_bins):floor_bins + half_bins + 1] = False
    floor = float(np.median(psd[near[keep]])) or 1e-12
    channel = float(psd[near[floor_bins - half_bins:floor_bins + half_bins + 1]].mean())
    return round(float(10.0 * np.log10(max(channel, 1e-12) / floor)), 1)


def capture(freq_hz: int, seconds: float, device: str = DEVICE) -> tuple[Path, int]:
    """One dwell on one frequency. Returns (iq file, centre frequency used)."""
    centre_hz = int(freq_hz) + DC_OFFSET_HZ
    samples = int(SAMPLE_RATE_HZ * seconds)
    workdir = Path(tempfile.mkdtemp(prefix="rf-hop-"))
    path = workdir / "iq.cu8"
    cmd = ["rtl_sdr", "-d", str(device), "-f", str(centre_hz),
           "-s", str(SAMPLE_RATE_HZ), "-n", str(samples), str(path)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=seconds + 30)
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise RuntimeError(f"rtl_sdr did not finish in {seconds + 30:.0f}s") from exc
    if not path.exists() or path.stat().st_size < 4096:
        shutil.rmtree(workdir, ignore_errors=True)
        raise RuntimeError("rtl_sdr produced no capture (device busy?)")
    return path, centre_hz


def hop(entry: dict[str, Any], *, dwell: float = DWELL_SECONDS) -> dict[str, Any]:
    """Dwell on one watchlist frequency, decode it, record any real decode."""
    freq_hz = int(entry["freq_hz"])
    started = utcnow()
    result: dict[str, Any] = {
        "freq_hz": freq_hz,
        "label": entry.get("label"),
        "decoder": entry.get("decoder"),
        "started": started,
    }
    path, centre_hz = capture(freq_hz, dwell)
    try:
        iq = read_rtl_iq(path)
        snr = channel_power_snr(iq, SAMPLE_RATE_HZ, centre_hz, freq_hz)
        audio = rf_decode.ddc_audio(iq, SAMPLE_RATE_HZ, centre_hz, freq_hz)
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)

    hits = rf_decode.run_multimon(audio, freq_hz)
    result.update({"snr_db": round(float(snr), 1), "finished": utcnow(),
                   "hits": len(hits), "episodes": []})
    if not hits:
        # No decode is a real answer: report it, do not manufacture an episode.
        result["status"] = "no-decode"
        return result

    result["status"] = "decoded"
    summary = rf_decode.summarise(hits)
    for hit in hits[:4]:
        blob = {
            "freq_hz": freq_hz,
            "low_hz": freq_hz - 6_000,
            "high_hz": freq_hz + 6_000,
            "n_bins": 1,
            "peak_snr_db": float(snr),
            "mean_snr_db": max(0.0, float(snr) - 3.0),
            "peak_rssi_db": float(snr),
            "floor_db": 0.0,
            "duty": 1.0,
            "score": min(100.0, 40.0 + float(snr) * 2.0),
            "band": "hopper",
            "likely": hit.get("label"),
            "confidence": 0.9,
            "class": "decoded",
            "known_emitter": entry.get("label"),
            "features_json": json.dumps({
                "hopper_radio": RADIO,
                "watchlist_id": entry.get("id"),
                "protocol": hit.get("protocol"),
                "line": hit.get("line"),
                "snr_db": round(float(snr), 1),
            }),
        }
        saved = store.upsert_blob(blob, tolerance_hz=6_000)
        episode_id = int(saved["id"])
        store.mark_decoded(episode_id, hit.get("decode_type") or "data",
                           summary or hit.get("line", ""), capture_id=f"hopper-{RADIO}")
        store.record_attempt(episode_id, hit.get("decoder") or "multimon", "ok",
                             (hit.get("line") or "")[:200])
        wav = store.episode_artifact_dir(episode_id) / "hopper-audio.wav"
        try:
            size = rf_wideband_lane._write_wav(wav, audio)
            store.add_artifact(episode_id, "audio", wav, size)
        except Exception:
            pass
        result["episodes"].append({
            "episode_id": episode_id,
            "protocol": hit.get("protocol"),
            "decode_type": hit.get("decode_type"),
            "line": (hit.get("line") or "")[:120],
        })
    return result


def run_cycle(*, dwell: float = DWELL_SECONDS) -> list[dict[str, Any]]:
    entries = select_entries()
    results = []
    for entry in entries:
        adopt(entry)
        try:
            results.append(hop(entry, dwell=dwell))
        except Exception as exc:
            results.append({"freq_hz": entry.get("freq_hz"), "label": entry.get("label"),
                            "status": "error", "detail": str(exc), "finished": utcnow()})
    return results


def format_result(result: dict[str, Any]) -> str:
    mhz = float(result.get("freq_hz") or 0) / 1e6
    status = result.get("status", "?")
    if status == "error":
        return "%.3f MHz  ERROR  %s" % (mhz, result.get("detail"))
    head = "%.3f MHz  %-10s snr %5.1f dB  %d hit(s)" % (
        mhz, status, result.get("snr_db") or 0.0, result.get("hits") or 0)
    if result.get("episodes"):
        head += "  " + "; ".join("%s#%s" % (e.get("decode_type"), e.get("episode_id"))
                                 for e in result["episodes"])
    return head


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RF Monitor hopper lane")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--dwell", type=float, default=DWELL_SECONDS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    store.init_db()
    if args.list:
        for entry in select_entries():
            print("id=%-3s %9.3f MHz  %-28s decoder=%s" % (
                entry.get("id"), entry["freq_hz"] / 1e6, entry.get("label"), entry.get("decoder")))
        return 0

    if args.loop:
        print("hopper lane: radio=%s device=%s dwell=%.0fs interval=%.0fs"
              % (RADIO, DEVICE, DWELL_SECONDS, INTERVAL_SECONDS), flush=True)
        while True:
            for result in run_cycle():
                print(format_result(result), flush=True)
            time.sleep(INTERVAL_SECONDS)

    results = run_cycle(dwell=args.dwell)
    print(json.dumps(results, indent=2) if args.json
          else "\n".join(format_result(r) for r in results))
    return 0 if all(r.get("status") != "error" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
