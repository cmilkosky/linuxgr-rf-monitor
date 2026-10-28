#!/usr/bin/env python3
"""RF Monitor v2 - wideband lane: one capture, a whole band of channels.

The capture lane listens to one frequency at a time. That is the wrong shape for
a band like 144-148 MHz, where the interesting things (APRS 144.390, packet
145.010) are far apart but all fit inside one HackRF window. This lane takes one
wide capture and decodes every channel in the band plan out of it:

    capture 144-148 MHz at 5 MS/s  ->  DDC each channel to baseband
        ->  multimon-ng per channel  ->  episodes for whatever decodes

Why this beats more dongles: an RTL parked on 2 MHz is one watchlist slot. The
HackRF window is ~4 MHz here and ~16 MHz at full 20 MS/s, so a single radio covers
a band that would otherwise take two to eight parked dongles - and the sweep is
untouched, because this runs on the PortaPack with its own flock.

Honest limit, measured 2026-10-28: the sweep's 10.1 ms/bin means it catches a
0.5 s APRS burst about 0.85% of the time. This lane is the answer to that - it
dwells, so it hears the bursts the sweep steps over.

    python3 rf_wideband_lane.py --list-bands
    python3 rf_wideband_lane.py --once --band wx-162
    python3 rf_wideband_lane.py --loop
    python3 rf_wideband_lane.py --stats
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

import rf_decode
import signals_store as store
from rf_v2_capture import PORTAPACK_LOCK_PATH, capture_hackrf, read_iq, remove_dc

DWELL_SECONDS = float(os.environ.get("RF_WIDEBAND_DWELL_SECONDS", "6"))
MIN_SNR_DB = float(os.environ.get("RF_WIDEBAND_MIN_SNR", "8"))
# Bandwidth over which channel power is averaged for the gate. 1 kHz suits NBFM (weather,
# pager, APRS): narrow enough that empty skirts don't dilute the carrier, wide enough
# (~33 bins at 2 MS/s) that one hot bin cannot masquerade as a signal.
TOP_BANDWIDTH_HZ = float(os.environ.get("RF_WIDEBAND_TOP_BW_HZ", "1000"))
INTERVAL = float(os.environ.get("RF_WIDEBAND_INTERVAL", "300"))
DEVICE_SERIAL = os.environ.get("RF_WIDEBAND_DEVICE_SERIAL", "").strip()
ENABLED_BANDS = os.environ.get("RF_WIDEBAND_BANDS", "vhf-144,wx-162,pager-929")
SPOOL = Path(os.environ.get("RF_WIDEBAND_SPOOL", "/tmp/rf-wideband"))


def _channels(start_hz: int, count: int, step_hz: int, prefix: str, decoder: str) -> list[dict[str, Any]]:
    return [
        {"freq_hz": start_hz + i * step_hz, "label": f"{prefix} {start_hz + i * step_hz}", "decoder": decoder}
        for i in range(count)
    ]


# A band is a centre frequency, a sample rate that covers it, and the channels
# worth pulling out. Sample rate stays just above the span: 20 MS/s would cover
# 16 MHz we are not using and cost 4x the RAM for nothing.
BANDS: dict[str, dict[str, Any]] = {
    "vhf-144": {
        "label": "144-148 MHz APRS / packet",
        "center_hz": 146_000_000,
        "sample_rate_hz": 5_000_000,
        "channels": [
            {"freq_hz": 144_390_000, "label": "APRS 144.390", "decoder": "aprs"},
            {"freq_hz": 145_010_000, "label": "packet 145.010", "decoder": "aprs"},
            {"freq_hz": 145_050_000, "label": "packet 145.050", "decoder": "aprs"},
        ],
    },
    "wx-162": {
        "label": "NOAA weather 162.400-162.550",
        "center_hz": 162_475_000,
        "sample_rate_hz": 2_000_000,
        "channels": [
            {"freq_hz": 162_400_000 + i * 25_000, "label": f"WX {i + 1} {162_400_000 + i * 25_000}",
             "decoder": "voice"}
            for i in range(7)
        ],
    },
    "pager-929": {
        "label": "paging 929.6-930.0",
        "center_hz": 929_800_000,
        "sample_rate_hz": 2_000_000,
        "channels": [
            {"freq_hz": 929_662_500 + i * 75_000, "label": f"pager {929_662_500 + i * 75_000}",
             "decoder": "pager"}
            for i in range(5)
        ],
    },
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# spectrum: which channels actually carry a carrier
# --------------------------------------------------------------------------- #
def channel_snr(iq: np.ndarray, sample_rate_hz: int, center_hz: int,
                channels: list[dict[str, Any]], seg: int = 1 << 16, segments: int = 8,
                half_width_hz: float = 6_000.0, floor_span_hz: float = 50_000.0) -> list[dict[str, Any]]:
    """Welch-averaged PSD -> per-channel SNR against a LOCAL noise floor.

    Cheap on purpose: 8 short FFTs, not a full-band transform of a 30M-sample
    capture. A channel that is quiet is skipped before the expensive DDC, which
    is what keeps a 7-channel weather band from costing 7 decodes every pass.

    The floor is measured within `floor_span_hz` of the channel with the channel
    itself excluded. Using the band-wide median instead manufactures carriers:
    measured at 144.390 MHz the local floor sits ~15 dB below the band median, so
    plain noise scored 13 dB there and the lane chased a signal that was not on
    the air (the RTL on the same antenna saw nothing).
    """
    n = iq.size
    if n < seg:
        seg = max(1024, 1 << int(np.log2(max(1024, n))))
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
        return [{"freq_hz": c["freq_hz"], "label": c["label"], "snr_db": 0.0,
                 "peak_snr_db": 0.0, "carrier": False} for c in channels]
    psd /= used
    bin_hz = float(sample_rate_hz) / seg
    half_bins = max(1, int(round(half_width_hz / bin_hz)))
    floor_bins = max(half_bins + 1, int(round(floor_span_hz / bin_hz)))
    report = []
    for channel in channels:
        offset_bins = int(round((channel["freq_hz"] - center_hz) / bin_hz))
        near = (offset_bins + np.arange(-floor_bins, floor_bins + 1)) % seg
        # local floor: the neighbourhood, minus the channel we are measuring
        keep = np.ones(near.size, dtype=bool)
        centre_index = floor_bins
        keep[max(0, centre_index - half_bins):centre_index + half_bins + 1] = False
        floor = float(np.median(psd[near[keep]])) or 1e-12
        window_bins = psd[near[centre_index - half_bins:centre_index + half_bins + 1]]
        peak = float(window_bins.max())
        peak_snr = 10.0 * np.log10(max(peak, 1e-12) / floor)
        # Gate on the mean power of the OCCUPIED bandwidth, not the best single bin and not
        # the whole 6 kHz window. Measured on one 162.5 MHz capture (2026-10-28):
        #   peak        noise 4.9/5.2 dB   WX7 27.0 dB   -> 10 dB of free SNR for noise
        #   mean all    noise 0.5/-0.3 dB  WX7  7.6 dB   -> real station falls under the gate
        #   mean top1k  noise 3.1/2.6 dB   WX7 16.2 dB   -> 13 dB of separation
        # The window mean is diluted by the empty skirts of a narrowband carrier; the top
        # 1 kHz is where an NBFM station's power actually is, and averaging 30+ bins keeps
        # a single-bin spur from reading as a carrier.
        n_top = max(1, int(round(TOP_BANDWIDTH_HZ / bin_hz)))
        occupied = float(np.sort(window_bins)[-n_top:].mean())
        snr = 10.0 * np.log10(max(occupied, 1e-12) / floor)
        report.append({
            "freq_hz": channel["freq_hz"], "label": channel["label"],
            "snr_db": round(float(snr), 1), "peak_snr_db": round(float(peak_snr), 1),
            "carrier": bool(snr >= MIN_SNR_DB),
        })
    return report


def _write_wav(path: Path, pcm: bytes, rate: int = rf_decode.AUDIO_RATE) -> int:
    """s16le mono -> minimal WAV so the operator can just play it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(b"RIFF")
        handle.write(struct.pack("<I", 36 + len(pcm)))
        handle.write(b"WAVEfmt ")
        handle.write(struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
        handle.write(b"data")
        handle.write(struct.pack("<I", len(pcm)))
        handle.write(pcm)
    return len(pcm) + 44


# --------------------------------------------------------------------------- #
# one band pass
# --------------------------------------------------------------------------- #
def run_band(band_name: str, *, dwell: float = DWELL_SECONDS) -> dict[str, Any]:
    band = BANDS[band_name]
    SPOOL.mkdir(parents=True, exist_ok=True)
    iq_path = SPOOL / f"{band_name}.iq"
    started = utcnow()
    try:
        meta = capture_hackrf(
            band["center_hz"], iq_path,
            duration_seconds=dwell,
            sample_rate_hz=band["sample_rate_hz"],
            device_serial=DEVICE_SERIAL or None,
            lock_path=PORTAPACK_LOCK_PATH,
        )
    except Exception as exc:  # radio busy, unplugged, or capture failed
        return {"band": band_name, "started": started, "error": f"{type(exc).__name__}: {exc}"}

    try:
        iq = remove_dc(read_iq(iq_path))
    finally:
        try:
            iq_path.unlink()
        except OSError:
            pass

    snr_report = channel_snr(iq, band["sample_rate_hz"], band["center_hz"], band["channels"])
    carriers = [entry for entry in snr_report if entry["carrier"]]
    hits_total: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []

    for entry in carriers:
        channel = next(c for c in band["channels"] if c["freq_hz"] == entry["freq_hz"])
        try:
            audio = rf_decode.ddc_audio(iq, band["sample_rate_hz"], band["center_hz"], channel["freq_hz"])
        except Exception as exc:
            episodes.append({"channel": channel["label"], "status": "ddc-failed", "detail": str(exc)})
            continue
        hits = rf_decode.run_multimon(audio, channel["freq_hz"])
        if not hits:
            hits = rf_decode.decode_iq(iq, band["sample_rate_hz"], band["center_hz"], channel["freq_hz"])
        if not hits:
            episodes.append({"channel": channel["label"], "status": "carrier-no-decode",
                             "snr_db": entry["snr_db"]})
            continue
        hits_total.extend(hits)
        episodes.extend(_record_hits(band, channel, entry, hits, audio))

    return {
        "band": band_name,
        "label": band["label"],
        "started": started,
        "finished": utcnow(),
        "capture": {k: meta.get(k) for k in ("frequency_hz", "sample_rate_hz", "sample_count", "duration_seconds", "iq_bytes")},
        "channels": snr_report,
        "carriers": len(carriers),
        "hits": len(hits_total),
        "episodes": episodes,
    }


def _record_hits(band: dict[str, Any], channel: dict[str, Any], entry: dict[str, Any],
                 hits: list[dict[str, Any]], audio: bytes) -> list[dict[str, Any]]:
    """One episode per decoded channel, refreshed in place while it stays active."""
    out = []
    summary = rf_decode.summarise(hits)
    for hit in hits[:4]:
        blob = {
            "freq_hz": int(channel["freq_hz"]),
            "low_hz": int(channel["freq_hz"]) - 6_000,
            "high_hz": int(channel["freq_hz"]) + 6_000,
            "n_bins": 1,
            "peak_snr_db": float(entry.get("peak_snr_db", entry["snr_db"])),
            "mean_snr_db": max(0.0, float(entry["snr_db"]) - 3.0),
            "peak_rssi_db": float(entry["snr_db"]),
            "floor_db": 0.0,
            "duty": 1.0,
            "score": min(100.0, 40.0 + float(entry["snr_db"]) * 2.0),
            "band": band["label"],
            "likely": hit.get("label"),
            "confidence": 0.9,
            "class": "decoded",
            "known_emitter": channel["label"],
            "features_json": json.dumps({
                "wideband_band": band["name"],
                "channel": channel["label"],
                "protocol": hit.get("protocol"),
                "line": hit.get("line"),
                "snr_db": entry["snr_db"],
            }),
        }
        result = store.upsert_blob(blob, tolerance_hz=6_000)
        episode_id = int(result["id"])
        store.mark_decoded(episode_id, hit.get("decode_type") or "data", summary or hit.get("line", ""),
                           capture_id=f"wideband-{band['name']}")
        store.record_attempt(episode_id, hit.get("decoder") or "multimon", "ok",
                             (hit.get("line") or "")[:200])
        artifact_dir = store.episode_artifact_dir(episode_id)
        wav = artifact_dir / "wideband-audio.wav"
        try:
            size = _write_wav(wav, audio)
            store.add_artifact(episode_id, "audio", wav, size)
        except Exception:
            pass
        out.append({"episode_id": episode_id, "channel": channel["label"],
                    "protocol": hit.get("protocol"), "decode_type": hit.get("decode_type"),
                    "created": result.get("created"), "line": (hit.get("line") or "")[:120]})
    return out


def run_cycle(*, dwell: float = DWELL_SECONDS) -> list[dict[str, Any]]:
    wanted = [name.strip() for name in ENABLED_BANDS.split(",") if name.strip() in BANDS]
    return [run_band(name, dwell=dwell) for name in wanted]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RF Monitor wideband lane")
    parser.add_argument("--list-bands", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--band", default="")
    parser.add_argument("--dwell", type=float, default=DWELL_SECONDS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    store.init_db()
    if args.list_bands:
        for name, band in BANDS.items():
            print("%-12s %-34s center %.3f MHz  %d channels  %.1f MS/s" % (
                name, band["label"], band["center_hz"] / 1e6, len(band["channels"]),
                band["sample_rate_hz"] / 1e6))
        return 0

    if args.band and args.band not in BANDS:
        print(f"unknown band {args.band}; known: {', '.join(BANDS)}", file=sys.stderr)
        return 2

    if args.once or args.band:
        result = run_band(args.band or sorted(BANDS)[0], dwell=args.dwell)
        print(json.dumps(result, indent=2) if args.json else _format_band(result))
        return 0 if not result.get("error") else 1

    if args.loop:
        print(f"wideband lane: bands={ENABLED_BANDS} dwell={DWELL_SECONDS}s interval={INTERVAL}s", flush=True)
        while True:
            for result in run_cycle():
                print(_format_band(result), flush=True)
            time.sleep(INTERVAL)

    print(_format_band(run_band(sorted(BANDS)[0], dwell=args.dwell)))
    return 0


def _format_band(result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"[{result['band']}] error: {result['error']}"
    lines = [f"[{result['band']}] {result['label']}  capture={result['capture']['duration_seconds']}s "
             f"carriers={result['carriers']}/{len(result['channels'])} hits={result['hits']}"]
    for channel in result["channels"]:
        lines.append("   %-26s %6.1f dB %s" % (channel["label"], channel["snr_db"],
                                               "carrier" if channel["carrier"] else "-"))
    for episode in result["episodes"]:
        if episode.get("episode_id"):
            lines.append("   -> ep%s %s %s: %s" % (episode["episode_id"], episode["channel"],
                                                   episode.get("protocol"), episode.get("line", "")))
        else:
            lines.append("   -> %s %s" % (episode["channel"], episode.get("status")))
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
