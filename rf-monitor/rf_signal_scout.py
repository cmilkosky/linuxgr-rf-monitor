#!/usr/bin/env python3
"""RF Monitor v2 - signal scout.

Reads the sweep data the existing collector already writes, finds blobs of
adjacent 1 MHz bins that rose above their own noise floor, scores them, and
records them as *episodes* (one row per signal, not per minute). It then grabs
an IQ capture for the most promising un-captured episodes inside the HackRF's
~59 s idle window and stores a spectrogram + audio previews.

Deliberately not an anomaly-vs-baseline detector: the v1 rule (delta >= 18 dB
over a 6 h median AND RSSI >= -55 dB) never fired - 6152 consecutive detector
runs reported anomalies=0, because the whole 1-6 GHz span sits inside a ~36 dB
window with a -55 dB floor, so -55 dB admitted 5996 of 6000 bins while 18 dB is
half the entire dynamic range. This scout scores SNR above each bin's own floor
instead.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests

import signals_store as store
from rf_signal_intel import classify_signal

LOG = logging.getLogger("rf_signal_scout")

CONFIG_PATH = Path(os.environ.get("RF_MONITOR_ENV", "/home/cmilkosk/.config/hackrf-influx.env"))
INTERVAL_SECONDS = int(os.environ.get("RF_SCOUT_INTERVAL", "60"))
FLOOR_HOURS = float(os.environ.get("RF_SCOUT_FLOOR_HOURS", "6"))
RECENT_MINUTES = float(os.environ.get("RF_SCOUT_RECENT_MINUTES", "10"))
SNR_MIN_DB = float(os.environ.get("RF_SCOUT_SNR_MIN", "8.0"))
BURST_SNR_MIN = float(os.environ.get("RF_SCOUT_BURST_SNR_MIN", "25.0"))
EXCESS_MIN_DB = float(os.environ.get("RF_SCOUT_EXCESS_MIN", "15.0"))
ARTIFACT_MAX_HZ = int(os.environ.get("RF_SCOUT_ARTIFACT_MAX_HZ", "6500000"))
MATCH_HZ = int(os.environ.get("RF_SCOUT_MATCH_HZ", "1500000"))
STALE_MINUTES = int(os.environ.get("RF_SCOUT_STALE_MIN", "15"))
MIN_SCORE_UNEXPLAINED = float(os.environ.get("RF_SCOUT_MIN_SCORE", "40"))
MIN_SCORE_KNOWN = float(os.environ.get("RF_SCOUT_MIN_SCORE_KNOWN", "45"))
MAX_CAPTURES_PER_RUN = int(os.environ.get("RF_SCOUT_MAX_CAPTURES", "3"))
CAPTURE_MIN_SCORE = float(os.environ.get("RF_SCOUT_CAPTURE_MIN_SCORE", "45"))
CAPTURES_PER_HOUR = int(os.environ.get("RF_SCOUT_CAPTURES_PER_HOUR", "60"))
RETENTION_EVERY_SECONDS = int(os.environ.get("RF_SCOUT_RETENTION_EVERY", "600"))

# Bands where a real signal is expected. "known" only affects whether the row
# competes in the unexplained feed - it does not stop the row existing, because
# these are exactly the bands the decoders care about.
KNOWN_BANDS: list[tuple[str, float, float]] = [
    ("FM broadcast", 87.5e6, 108.0e6),
    ("Aviation AM voice", 108.0e6, 137.0e6),
    ("NOAA weather radio", 162.3e6, 162.6e6),
    ("Cellular downlink 600 MHz", 617.0e6, 652.0e6),
    ("Cellular downlink 700 MHz", 728.0e6, 757.0e6),
    ("Cellular downlink 850 MHz", 869.0e6, 894.0e6),
    ("GPS L1 / GNSS", 1565.0e6, 1585.0e6),
    ("Cellular 1.7 GHz (AWS)", 1710.0e6, 1755.0e6),
    ("Cellular 1.9 GHz PCS", 1850.0e6, 1915.0e6),
    ("Cellular downlink 1.9 GHz", 1930.0e6, 1995.0e6),
    ("Cellular downlink 2.1 GHz", 2110.0e6, 2200.0e6),
    ("Wi-Fi / ISM 2.4 GHz", 2400.0e6, 2483.5e6),
    ("Cellular 2.5 GHz (BRS/EBS)", 2500.0e6, 2690.0e6),
    ("CBRS 3.5 GHz", 3550.0e6, 3700.0e6),
    ("Wi-Fi / U-NII 5 GHz", 5150.0e6, 5895.0e6),
]

# v2 band labels: specific enough to say something useful. rf_signal_intel only
# knows broad VHF/UHF ranges, so 1015 MHz used to read "broad radio band".
V2_BANDS: list[tuple[str, str, float, float]] = [
    ("ADS-B / Mode S", "aircraft transponder", 1085.0e6, 1095.0e6),
    ("FM broadcast", "music/voice broadcast", 87.5e6, 108.0e6),
    ("Airband AM voice", "aviation voice/navigation", 108.0e6, 137.0e6),
    ("2 m amateur band", "amateur voice/APRS", 144.0e6, 148.0e6),
    ("NOAA weather radio", "weather voice", 162.3e6, 162.6e6),
    ("406 MHz EPIRB/PLB", "emergency beacon", 406.0e6, 406.2e6),
    ("70 cm amateur band", "amateur voice/data", 420.0e6, 450.0e6),
    ("FRS/GMRS", "personal voice", 462.0e6, 468.0e6),
    ("Cellular uplink / ISM 900", "mobile uplink", 896.0e6, 960.0e6),
    ("Aeronautical radionavigation", "DME / TACAN / GNSS", 960.0e6, 1215.0e6),
    ("GNSS / secondary radar", "navigation", 1215.0e6, 1300.0e6),
    ("Cellular 1.7 GHz (AWS)", "mobile downlink", 1710.0e6, 1755.0e6),
    ("Cellular 1.9 GHz PCS", "mobile", 1850.0e6, 1995.0e6),
    ("Wi-Fi / ISM 2.4 GHz", "Wi-Fi/Bluetooth/Zigbee", 2400.0e6, 2483.5e6),
    ("Cellular 2.5 GHz (BRS/EBS)", "mobile broadband", 2500.0e6, 2690.0e6),
    ("Wi-Fi / U-NII 5 GHz", "Wi-Fi", 5150.0e6, 5895.0e6),
    ("HF / shortwave", "below 30 MHz - HackRF has no upconverter, low confidence", 6.5e6, 30.0e6),
]

# Bands worth spending a capture on even when they are "known".
DECODE_HINTS: list[tuple[str, float, float]] = [
    ("fm-rds", 87.5e6, 108.0e6),
    ("am-airband", 108.0e6, 137.0e6),
    ("ax25-aprs", 144.0e6, 148.0e6),
    ("acars", 129.0e6, 132.0e6),
    ("nfm-noaa", 162.3e6, 162.6e6),
    ("rtl433", 430.0e6, 435.0e6),
    ("nfm-frs", 462.0e6, 468.0e6),
    ("rtl433", 902.0e6, 928.0e6),
    ("adsb", 1085.0e6, 1095.0e6),
]


# --------------------------------------------------------------------------- #
# influx
# --------------------------------------------------------------------------- #
def load_env(path: Path = CONFIG_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value.strip().strip('"').strip("'")
    return env


def flux_query(env: dict[str, str], query: str, timeout: float = 45.0) -> list[dict[str, str]]:
    response = requests.post(
        f"{env['INFLUX_URL'].rstrip('/')}/api/v2/query",
        params={"org": env["INFLUX_ORG"]},
        headers={
            "Authorization": f"Token {env['INFLUX_TOKEN']}",
            "Accept": "application/csv",
            "Content-Type": "application/vnd.flux",
        },
        data=query,
        timeout=timeout,
    )
    response.raise_for_status()
    return [
        row
        for row in csv.DictReader(io.StringIO(response.text))
        if row.get("result") == "_result" and row.get("_value") not in (None, "")
    ]


def flux_duration(value: float, unit: str) -> str:
    if float(value).is_integer():
        return f"{int(value)}{unit}"
    seconds = max(1, round(value * {"h": 3600, "m": 60}[unit]))
    return f"{seconds}s"


def fetch_floor(env: dict[str, str]) -> dict[int, float]:
    bucket = env["INFLUX_BUCKET"]
    floor_duration = flux_duration(FLOOR_HOURS, "h")
    recent_duration = flux_duration(RECENT_MINUTES, "m")
    query = f'''
from(bucket: "{bucket}")
  |> range(start: -{floor_duration}, stop: -{recent_duration})
  |> filter(fn: (r) => r._measurement == "rf_sweep" and r._field == "rssi_db")
  |> group(columns: ["frequency_hz"])
  |> median()
  |> keep(columns: ["frequency_hz", "_value"])
'''
    floor: dict[int, float] = {}
    for row in flux_query(env, query):
        try:
            floor[int(row["frequency_hz"])] = float(row["_value"])
        except (KeyError, TypeError, ValueError):
            continue
    return floor


def fetch_recent(env: dict[str, str]) -> list[tuple[str, int, float]]:
    bucket = env["INFLUX_BUCKET"]
    recent_duration = flux_duration(RECENT_MINUTES, "m")
    query = f'''
from(bucket: "{bucket}")
  |> range(start: -{recent_duration})
  |> filter(fn: (r) => r._measurement == "rf_sweep" and r._field == "rssi_db")
  |> keep(columns: ["_time", "frequency_hz", "_value"])
'''
    rows: list[tuple[str, int, float]] = []
    for row in flux_query(env, query, timeout=60.0):
        try:
            rows.append((row["_time"], int(row["frequency_hz"]), float(row["_value"])))
        except (KeyError, TypeError, ValueError):
            continue
    return rows


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def _contiguous(indexes: np.ndarray) -> list[np.ndarray]:
    if indexes.size == 0:
        return []
    breaks = np.where(np.diff(indexes) > 1)[0] + 1
    return np.split(indexes, breaks)


def detect_blobs(
    floor: dict[int, float],
    recent: list[tuple[str, int, float]],
    snr_min: float = SNR_MIN_DB,
) -> list[dict[str, Any]]:
    if not recent:
        return []
    freqs = np.array(sorted({f for _t, f, _v in recent}), dtype=np.int64)
    # The collector stamps ~158 rows per timestamp inside one sweep (20 ms apart),
    # so bucket to the minute - otherwise the time axis is 380 slices wide and the
    # duty-cycle denominator is nonsense.
    times = sorted({t[:16] for t, _f, _v in recent})
    if freqs.size == 0 or not times:
        return []
    f_index = {int(f): i for i, f in enumerate(freqs)}
    t_index = {t: i for i, t in enumerate(times)}
    matrix = np.full((freqs.size, len(times)), np.nan, dtype=np.float32)
    for stamp, freq, value in recent:
        matrix[f_index[freq], t_index[stamp[:16]]] = value

    floor_arr = np.array([floor.get(int(f), np.nan) for f in freqs], dtype=np.float32)
    # One spectrum-wide noise floor, so a carrier that has been on for days still
    # registers as strong even though it is no longer "new" against its own median.
    with np.errstate(all="ignore"):
        import warnings as _w

        with _w.catch_warnings():
            _w.simplefilter("ignore", RuntimeWarning)
            global_floor = float(np.nanmedian(floor_arr))
    import warnings

    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        recent_max = np.nanmax(matrix, axis=1)
        recent_p75 = np.nanpercentile(matrix, 75, axis=1)
        recent_std = np.nanstd(matrix, axis=1)

    # Three statistics, because no single one covers the cases:
    #   strength  = peak level above the whole spectrum's noise floor  -> "loudest"
    #   excess    = typical level above that floor                    -> "always on"
    #   delta     = typical level above this bin's own 6 h median      -> "changed"
    # A max-vs-median comparison is also biased upward by ~6 dB (measured p50 =
    # 6.05 dB across 6000 bins), so the typical level uses p75, not the max.
    strength = recent_max - global_floor
    excess = recent_p75 - global_floor
    delta = recent_p75 - floor_arr
    valid = np.isfinite(delta) & (freqs >= ARTIFACT_MAX_HZ)
    candidates = valid & (
        (delta >= snr_min) | (excess >= EXCESS_MIN_DB) | (strength >= BURST_SNR_MIN)
    )
    indexes = np.where(candidates)[0]

    blobs: list[dict[str, Any]] = []
    for group in _contiguous(indexes):
        seg = slice(int(group[0]), int(group[-1]) + 1)
        seg_freqs = freqs[seg].astype(np.float64)
        seg_delta = delta[seg].astype(np.float64)
        seg_strength = strength[seg].astype(np.float64)
        weights = np.clip(np.maximum(seg_delta, 0.0) + 0.5, 0.1, None)
        center = float((seg_freqs * weights).sum() / weights.sum())
        peak_pos = int(group[int(np.argmax(seg_strength))])
        blob_peak_snr = float(seg_strength.max())
        peak_floor = float(floor_arr[peak_pos])
        peak_row = matrix[peak_pos]
        # "on-air" = fraction of minutes the bin is above the spectrum floor at
        # all. (Measuring it against its own 6 h median reports 0 % for a carrier
        # that has simply been on the whole time.)
        finite = np.isfinite(peak_row)
        if finite.any():
            duty = float(np.mean(peak_row[finite] >= (global_floor + 6.0)))
        else:
            duty = 0.0
        blobs.append(
            {
                "freq_hz": int(round(center / 1e6) * 1e6),
                "center_hz": int(round(center)),
                "low_hz": int(seg_freqs.min() - 500_000),
                "high_hz": int(seg_freqs.max() + 500_000),
                "bandwidth_hz": int(round((seg_freqs.max() - seg_freqs.min()) + 1_000_000)),
                "n_bins": int(group.size),
                "peak_snr_db": round(blob_peak_snr, 2),
                "mean_snr_db": round(float(excess[seg].mean()), 2),
                "delta_db": round(float(seg_delta.max()), 2),
                "peak_rssi_db": round(float(recent_max[peak_pos]), 2),
                "floor_db": round(peak_floor, 2),
                "duty": round(duty, 3),
                "jitter_db": round(float(recent_std[peak_pos]), 2),
                "peak_freq_hz": int(freqs[peak_pos]),
            }
        )
    blobs.sort(key=lambda b: b["peak_snr_db"], reverse=True)
    return blobs


def known_band(freq_hz: int) -> str | None:
    """KNOWN_BANDS and DECODE_HINTS are in Hz - compare in Hz, not MHz."""
    for name, low, high in KNOWN_BANDS:
        if low <= float(freq_hz) <= high:
            return name
    return None


def decode_hint(freq_hz: int) -> str | None:
    for hint, low, high in DECODE_HINTS:
        if low <= float(freq_hz) <= high:
            return hint
    return None


def score_blob(blob: dict[str, Any], is_new: bool, known: str | None) -> float:
    """0-100 interestingness: how loud, how much it changed, and how novel."""
    score = min(35.0, max(0.0, float(blob["peak_snr_db"])) * 1.5)      # strength
    score += min(25.0, max(0.0, float(blob.get("delta_db") or 0.0)) * 1.5)  # change
    if is_new:
        score += 15.0
    score += -30.0 if known else 15.0
    score += 8.0 if float(blob["duty"]) < 0.5 else 3.0
    if int(blob["n_bins"]) == 1:
        score += 4.0
    if int(blob["n_bins"]) > 20:
        score -= 6.0
    if int(blob["freq_hz"]) < 30_000_000:
        score -= 5.0  # HackRF has no upconverter; HF is low confidence
    return max(0.0, min(100.0, score))


def describe_blob(blob: dict[str, Any], known: str | None) -> dict[str, Any]:
    freq_hz = float(blob["freq_hz"])
    if known:
        return {"band": known, "likely": known, "confidence": "high",
                "class": "known", "known_emitter": known}
    for name, kind, low, high in V2_BANDS:
        if low <= freq_hz <= high:
            return {"band": f"{name} ({kind})", "likely": name, "confidence": "medium",
                    "class": "unexplained", "known_emitter": None}
    intel = classify_signal(blob["freq_hz"])
    return {
        "band": ", ".join(b["name"] for b in intel.get("bands", [])) or None,
        "likely": intel.get("likely"),
        "confidence": intel.get("confidence"),
        "class": "unexplained",
        "known_emitter": None,
    }


# --------------------------------------------------------------------------- #
# capture queue
# --------------------------------------------------------------------------- #
def _live_decoder(target_hz: int):
    """Build a decode callback bound to one episode's frequency."""
    def callback(iq, sample_rate_hz: int, tune_hz: int, out_dir: Path) -> list[dict[str, Any]]:
        import rf_decode

        return rf_decode.decode_iq_hits(iq, sample_rate_hz, tune_hz, target_hz)

    return callback


def process_captures(env: dict[str, str], budget: int) -> list[str]:
    if budget <= 0:
        return []
    import rf_v2_capture as capture
    import rf_decode as decode

    notes: list[str] = []
    for episode in store.pending_capture(limit=budget, min_score=CAPTURE_MIN_SCORE):
        out_dir = store.episode_artifact_dir(int(episode["id"]))
        try:
            bundle = capture.capture_and_analyse(
                episode, out_dir, keep_iq=False,
                decode_cb=_live_decoder(int(episode["freq_hz"])),
            )
        except Exception as exc:
            store.set_decode_status(int(episode["id"]), "failed", str(exc)[:300])
            notes.append(f"ep{episode['id']} capture failed: {exc}")
            LOG.warning("capture failed for episode %s: %s", episode["id"], exc)
            continue

        capture_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{episode['freq_hz']}"
        store.add_artifact(int(episode["id"]), "png", bundle["artifacts"][0]["png"],
                           bundle["artifacts"][0]["png_bytes"])
        stored: list[dict[str, Any]] = [{"kind": "png", "path": bundle["artifacts"][0]["png"]}]
        for mode, info in (bundle.get("audio") or {}).items():
            if info.get("mp3"):
                store.add_artifact(int(episode["id"]), f"audio-{mode}", info["mp3"], info["mp3_bytes"])
                stored.append({"kind": f"audio-{mode}", "path": info["mp3"]})
            elif info.get("wav"):
                store.add_artifact(int(episode["id"]), f"audio-{mode}", info["wav"], info["bytes"])
                stored.append({"kind": f"audio-{mode}", "path": info["wav"]})
        with store.connect() as conn:
            conn.execute(
                "UPDATE episodes SET capture_id=?, decode_status='captured', features_json=? WHERE id=?",
                (capture_id, json.dumps(bundle.get("features")), int(episode["id"])),
            )
            conn.commit()

        intel = classify_signal(int(episode["freq_hz"]), {"features": bundle.get("features") or {}})
        store.set_intel(int(episode["id"]), intel)

        # Decode stage: use hits already obtained from full-rate IQ, else run the
        # audio ladder over the artifacts we just wrote.
        pre_hits = bundle.get("decode") if isinstance(bundle.get("decode"), list) else None
        decoded = decode.decode_episode(episode, stored, pre_hits=pre_hits)
        if decoded["status"] == "decoded":
            notes.append(
                f"ep{episode['id']} DECODED {decoded['decode_type']}: "
                f"{(decoded.get('summary') or '')[:140]}"
            )
            LOG.info("decoded ep%s as %s: %s", episode["id"], decoded["decode_type"],
                     (decoded.get("summary") or "")[:200])

        notes.append(
            f"ep{episode['id']} {episode['freq_hz']/1e6:.3f}MHz "
            f"bw={intel.get('features', {}).get('occupied_bandwidth_hz')} "
            f"likely={intel.get('likely')}"
        )
    return notes


# --------------------------------------------------------------------------- #
# main loop
# --------------------------------------------------------------------------- #
def run_once(env: dict[str, str], *, dry_run: bool = False, capture_budget: int = 0) -> dict[str, Any]:
    started = time.monotonic()
    floor = fetch_floor(env)
    recent = fetch_recent(env)
    if not floor or not recent:
        LOG.warning("no sweep data returned (floor=%s recent=%s)", len(floor), len(recent))
        return {"blobs": 0, "created": 0, "updated": 0, "captures": [], "seconds": 0.0}

    blobs = detect_blobs(floor, recent)
    created = updated = skipped = 0
    for blob in blobs:
        known = known_band(blob["freq_hz"])
        is_new = not store.seen_before(blob["freq_hz"], MATCH_HZ, store.local_day_start_utc(1))
        blob["score"] = round(score_blob(blob, is_new, known), 1)
        threshold = MIN_SCORE_KNOWN if known else MIN_SCORE_UNEXPLAINED
        if known and decode_hint(blob["freq_hz"]):
            threshold = min(threshold, 15.0)  # decode-worthy band: capture it
        if blob["score"] < threshold:
            skipped += 1
            continue
        blob.update(describe_blob(blob, known))
        if dry_run:
            LOG.info(
                "DRY %8.3f MHz strength=%5.1f delta=%5.1f bw=%4.1fMHz duty=%.2f bins=%2d class=%-11s score=%5.1f %s",
                blob["freq_hz"] / 1e6, blob["peak_snr_db"], blob.get("delta_db") or 0.0,
                blob["bandwidth_hz"] / 1e6, blob["duty"], blob["n_bins"], blob["class"],
                blob["score"], blob["likely"],
            )
            continue
        result = store.upsert_blob(blob, MATCH_HZ, STALE_MINUTES)
        if result["created"]:
            created += 1
            LOG.info(
                "NEW ep%d %.3f MHz snr=%.1f bw=%.1fMHz score=%.1f new=%s class=%s",
                result["id"], blob["freq_hz"] / 1e6, blob["peak_snr_db"],
                blob["bandwidth_hz"] / 1e6, blob["score"], is_new, blob["class"],
            )
        else:
            updated += 1

    notes: list[str] = []
    if not dry_run:
        store.close_stale(STALE_MINUTES)
        notes = process_captures(env, capture_budget)

    elapsed = time.monotonic() - started
    summary = {
        "blobs": len(blobs),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "captures": notes,
        "seconds": round(elapsed, 2),
    }
    LOG.info(
        "scan blobs=%d new=%d updated=%d skipped=%d captures=%d in %.2fs",
        summary["blobs"], created, updated, skipped, len(notes), elapsed,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RF Monitor v2 signal scout")
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument("--dry-run", action="store_true", help="detect and log, write nothing")
    parser.add_argument("--captures", type=int, default=None, help="captures to attempt this pass")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    env = load_env()
    store.init_db()

    capture_budget = MAX_CAPTURES_PER_RUN if args.captures is None else args.captures
    if args.dry_run:
        capture_budget = 0

    if args.once:
        run_once(env, dry_run=args.dry_run, capture_budget=capture_budget)
        return 0

    last_retention = 0.0
    capture_stamps: list[float] = []
    while True:
        started = time.monotonic()
        try:
            now = time.monotonic()
            capture_stamps = [t for t in capture_stamps if now - t < 3600]
            budget = max(0, min(capture_budget, CAPTURES_PER_HOUR - len(capture_stamps)))
            summary = run_once(env, capture_budget=budget)
            capture_stamps.extend([now] * len(summary.get("captures") or []))
        except Exception as exc:
            LOG.error("scout pass failed: %s", exc)
        if time.monotonic() - last_retention > RETENTION_EVERY_SECONDS:
            try:
                info = store.enforce_cap()
                LOG.info("artifacts %s", info)
            except Exception as exc:
                LOG.error("retention failed: %s", exc)
            last_retention = time.monotonic()
        time.sleep(max(0.0, INTERVAL_SECONDS - (time.monotonic() - started)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
