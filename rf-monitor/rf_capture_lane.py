#!/usr/bin/env python3
"""RF Monitor v2 - capture lane on the second radio (PortaPack, HackRF mode).

Why this exists: the scout's captures share the main HackRF with the always-on
sweep, so every capture is a 4 s grab wedged into the sweep's idle window and the
sweep pauses while it runs. A second radio changes what is possible:

* the sweep keeps sweeping while a capture is in flight - no shared flock;
* dwell can be long enough for content that does not fit in 4 s (a slow APRS
  beacon, a pager burst that starts late, a data frame mid-transmission);
* extend-on-progress keeps listening while the signal is still there instead of
  cutting it off on a fixed timer.

The radio is pinned by serial. With two HackRFs attached an unpinned
`hackrf_transfer` takes whichever enumerated first, which is how a capture lane
and the sweep end up fighting over one radio and why that is not left to chance.

Spur masking: the PortaPack leaks its own spurs around 89.0 and 101.5 MHz. Those
are real FM broadcast channels here, so a capture on them would mostly be the
radio hearing itself. Episodes inside the mask are marked `class='spur-masked'`
and never captured on this radio; the main HackRF sweep still sees them.

Configuration (EnvironmentFile, mode 600 - the serial is not a public value):

    RF_CAPTURE_DEVICE_SERIAL   PortaPack serial (required)
    RF_LANE_DWELL_SECONDS      base chunk, default 30
    RF_LANE_MAX_DWELL_SECONDS  ceiling for extend-on-progress, default 90
    RF_LANE_MIN_SCORE          queue threshold, default 60
    RF_LANE_MAX_PER_RUN        captures per pass, default 1
    RF_LANE_INTERVAL           seconds between passes, default 60
    RF_LANE_SPUR_MASK          comma-separated Hz, default 89000000,101500000

    python3 rf_capture_lane.py --once
    python3 rf_capture_lane.py --loop
    python3 rf_capture_lane.py --stats
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import rf_voice_gate as voice_gate
import signals_store as store

LOG_PREFIX = "rf_capture_lane"

DWELL_SECONDS = float(os.environ.get("RF_LANE_DWELL_SECONDS", "30"))
MAX_DWELL_SECONDS = float(os.environ.get("RF_LANE_MAX_DWELL_SECONDS", "90"))
MIN_SCORE = float(os.environ.get("RF_LANE_MIN_SCORE", "60"))
MAX_PER_RUN = int(os.environ.get("RF_LANE_MAX_PER_RUN", "1"))
INTERVAL = float(os.environ.get("RF_LANE_INTERVAL", "60"))
DEVICE_SERIAL = os.environ.get("RF_CAPTURE_DEVICE_SERIAL", "").strip()
# A dwell that lands on nothing but the receiver's own noise is not a recording of
# anything: it fills the artifact store with static and gets mislabelled as a chirp.
# Blank captures are dropped (class='noise') instead of being kept and decoded.
NOISE_FLATNESS = float(os.environ.get("RF_NOISE_FLATNESS", "0.80"))
NOISE_WIDE_HZ = int(os.environ.get("RF_NOISE_WIDE_HZ", "1500000"))
NOISE_PURGE = os.environ.get("RF_NOISE_PURGE", "1").strip().lower() not in ("0", "false", "no")
SPUR_MASK = tuple(
    int(part) for part in os.environ.get("RF_LANE_SPUR_MASK", "89000000,101500000").split(",")
    if part.strip().isdigit()
)
# Bands where the decode ladder can actually produce a frame. The queue is
# ordered by score, and score tracks signal strength - which means the loudest
# entries are broadcast and radar, the two things that never decode. With one
# long capture per pass, spend it where a frame is possible; fall back to the
# highest score when nothing decodable is pending so coverage is not lost.
PRIORITY_BANDS_HZ: tuple[tuple[int, int, str], ...] = (
    (144_000_000, 148_000_000, "APRS / packet"),
    (162_000_000, 174_000_000, "weather / EAS / paging"),
    (315_000_000, 316_000_000, "315 MHz ISM"),
    (433_000_000, 435_000_000, "433.92 MHz ISM"),
    (902_000_000, 928_000_000, "915 MHz ISM"),
    (929_000_000, 932_000_000, "929 MHz paging"),
)


def priority_for(freq_hz: int) -> str | None:
    for low, high, label in PRIORITY_BANDS_HZ:
        if low <= int(freq_hz) <= high:
            return label
    return None


def pick_queue(max_per_run: int, min_score: float) -> list[dict[str, Any]]:
    """Decodable bands first, then score - see PRIORITY_BANDS_HZ."""
    candidates = store.pending_capture(limit=max(200, max_per_run), min_score=min_score)
    if not candidates:
        return []
    candidates.sort(key=lambda ep: (0 if priority_for(ep["freq_hz"]) else 1, -float(ep.get("score") or 0)))
    return candidates[:max_per_run]


def log(message: str) -> None:
    print(f"{LOG_PREFIX}: {message}", flush=True)


def decode_callback(target_hz: int, window_seconds: float = 20.0):
    """Decode from the capture's IQ, bounded to the ends of the dwell.

    A 90 s dwell is 180M complex samples; running the down-converter over all of
    it costs more than the capture did. Protocol frames are short, so the first
    and last `window_seconds` of the dwell are enough to catch a beacon that was
    already running or one that started late.
    """
    def callback(iq, sample_rate_hz: int, tune_hz: int, out_dir: Path) -> list[dict[str, Any]]:
        import rf_decode

        limit = int(sample_rate_hz * window_seconds)
        hits = rf_decode.decode_iq_hits(iq[:limit], sample_rate_hz, tune_hz, target_hz)
        if not hits and iq.size > 2 * limit:
            hits = rf_decode.decode_iq_hits(iq[-limit:], sample_rate_hz, tune_hz, target_hz)
        return hits

    return callback


def record_bundle(episode: dict[str, Any], bundle: dict[str, Any], capture_id: str) -> dict[str, Any]:
    """Persist artifacts, features, intel and decode result for one capture."""
    import rf_decode as decode
    from rf_signal_scout import classify_signal

    episode_id = int(episode["id"])
    stored: list[dict[str, Any]] = []
    for art in bundle.get("artifacts") or []:
        if "png" in art:
            store.add_artifact(episode_id, "png", art["png"], art.get("png_bytes") or 0)
            stored.append({"kind": "png", "path": art["png"]})
    for mode, info in (bundle.get("audio") or {}).items():
        if info.get("mp3"):
            store.add_artifact(episode_id, f"audio-{mode}", info["mp3"], info.get("mp3_bytes") or 0)
            stored.append({"kind": f"audio-{mode}", "path": info["mp3"]})
        elif info.get("wav"):
            store.add_artifact(episode_id, f"audio-{mode}", info["wav"], info.get("bytes") or 0)
            stored.append({"kind": f"audio-{mode}", "path": info["wav"]})

    # Silero VAD verdict on the demodulated audio: the only stage of this lane that
    # actually listens for a human voice. It labels, it does not gate - a VAD outage
    # or disagreement must never discard a capture the radio already made.
    vad = None
    for art in stored:
        if str(art.get("kind", "")).startswith("audio-"):
            vad = voice_gate.gate_file(art["path"])
            if vad and vad.get("gate_pass"):
                break
    if vad:
        log(f"ep{episode_id} {voice_gate.summary(vad)}")

    with store.connect() as conn:
        # freq_hz and `likely` are re-derived by the scout on every sweep, so the voice
        # verdict is stored in its own columns: this is the only stage that listened, and
        # its answer must survive later reclassification.
        conn.execute(
            "UPDATE episodes SET capture_id=?, decode_status='captured', features_json=?,"
            " demod_freq_hz=?, voice_verdict=?, voice_speech_pct=?, voice_max_prob=?,"
            " voice_checked_at=datetime('now') WHERE id=?",
            (capture_id, json.dumps(bundle.get("features")),
             # the capture tuned off-centre by LO_OFFSET_HZ and mixed it back out, so the
             # audio sits at target_hz; episode.freq_hz is re-derived by the scout later.
             int((bundle.get("capture") or {}).get("target_hz") or episode["freq_hz"]),
             (vad or {}).get("verdict") if vad else "unchecked",
             (vad or {}).get("speech_pct") if vad else None,
             (vad or {}).get("max_prob") if vad else None,
             episode_id),
        )
        conn.commit()

    intel = classify_signal(int(episode["freq_hz"]), {"features": bundle.get("features") or {}})
    if vad:
        intel = dict(intel)
        intel["vad"] = vad
        if vad.get("gate_pass"):
            intel["likely"] = "Voice (demodulated audio)"
            intel["confidence"] = "high"
    store.set_intel(episode_id, intel)

    pre_hits = bundle.get("decode") if isinstance(bundle.get("decode"), list) else None
    decoded = decode.decode_episode(episode, stored, pre_hits=pre_hits)

    # Voice is a filter here too, but a guarded one: drop the audio only when the clip
    # HAS audio, the gate says no voice, AND the decode ladder found nothing. APRS,
    # LoRa and pager frames legitimately carry no voice, so a decoded frame always
    # survives; the spectrogram png stays either way as the evidence for the episode.
    if vad and not vad.get("gate_pass") and decoded.get("status") != "decoded":
        _drop_audio(episode_id, stored, voice_gate.summary(vad))

    return {"intel": intel, "decoded": decoded, "artifacts": len(stored)}


def _drop_audio(episode_id: int, stored: list[dict[str, Any]], why: str) -> None:
    """Unregister and delete the audio artifacts of a capture that is not voice."""
    audio = [a for a in stored if str(a.get("kind", "")).startswith("audio-")]
    if not audio:
        return
    with store.connect() as conn:
        conn.execute("DELETE FROM artifacts WHERE episode_id=? AND kind LIKE 'audio-%'",
                     (episode_id,))
        conn.execute(
            "UPDATE episodes SET decode_status='rejected', decoded_summary=? WHERE id=?",
            (f"no voice and nothing decoded ({why}) - audio discarded", episode_id),
        )
        conn.commit()
    for art in audio:
        try:
            Path(art["path"]).unlink(missing_ok=True)
        except OSError:
            pass
    stored[:] = [a for a in stored if not str(a.get("kind", "")).startswith("audio-")]
    log(f"ep{episode_id} {why} - no voice, nothing decoded: audio discarded")


def is_blank_capture(features: dict[str, Any]) -> bool:
    """True when the dwell found nothing but the receiver's own noise.

    ep36778 sat at 151.6 MHz with spectral flatness 0.93 across 5 MHz and no carrier
    anywhere in the spectrum, yet it was stored and labelled "Radar / FMCW sweep
    candidate". Flat *and* wide is noise: a real emitter leaves structure.
    """
    try:
        flatness = float(features.get("spectral_flatness") or 0.0)
        occupied = int(features.get("occupied_bandwidth_hz") or 0)
    except (TypeError, ValueError):
        return False
    return flatness >= NOISE_FLATNESS and occupied >= NOISE_WIDE_HZ


def capture_one(episode: dict[str, Any], *, dwell: float, max_dwell: float,
                serial: str, spur_mask: tuple[int, ...]) -> dict[str, Any]:
    """Capture + decode a single episode on the pinned radio."""
    import rf_v2_capture as capture

    episode_id = int(episode["id"])
    out_dir = store.episode_artifact_dir(episode_id)
    freq_mhz = int(episode["freq_hz"]) / 1e6
    started = time.monotonic()
    try:
        bundle = capture.capture_and_analyse(
            episode, out_dir,
            duration_seconds=dwell, max_duration_seconds=max_dwell,
            device_serial=serial, lock_path=capture.PORTAPACK_LOCK_PATH,
            spur_mask_hz=spur_mask,
            decode_cb=decode_callback(int(episode["freq_hz"])),
        )
    except capture.SpurMaskedError as exc:
        with store.connect() as conn:
            conn.execute(
                "UPDATE episodes SET class='spur-masked', decode_status='skipped',"
                " decoded_summary=? WHERE id=?",
                (f"not captured: {exc}"[:400], episode_id),
            )
            conn.commit()
        log(f"ep{episode_id} {freq_mhz:.3f} MHz skipped (spur mask)")
        return {"episode_id": episode_id, "status": "spur-masked"}
    except Exception as exc:
        store.set_decode_status(episode_id, "failed", str(exc)[:300])
        log(f"ep{episode_id} {freq_mhz:.3f} MHz capture FAILED: {exc}")
        return {"episode_id": episode_id, "status": "failed", "error": str(exc)[:200]}

    capture_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{episode['freq_hz']}"
    recorded = record_bundle(episode, bundle, capture_id)
    elapsed = time.monotonic() - started
    dwell_actual = (bundle.get("capture") or {}).get("dwell_seconds")
    decoded = recorded["decoded"]
    log(
        f"ep{episode_id} {freq_mhz:.3f} MHz dwell={dwell_actual}s "
        f"bw={(recorded['intel'].get('features') or {}).get('occupied_bandwidth_hz')} "
        f"likely={recorded['intel'].get('likely')} "
        f"decode={decoded['status']}"
        + (f"/{decoded['decode_type']}: {(decoded.get('summary') or '')[:90]}"
           if decoded["status"] == "decoded" else "")
        + f" in {elapsed:.1f}s"
    )
    if NOISE_PURGE and is_blank_capture(bundle.get("features") or {}) and decoded["status"] != "decoded":
        with store.connect() as conn:
            conn.execute(
                "UPDATE episodes SET class='noise', decode_status='rejected', decoded_summary=?"
                " WHERE id=?",
                ("blank capture: flat wideband noise, no carrier in the dwell", episode_id),
            )
            conn.commit()
        shutil.rmtree(out_dir, ignore_errors=True)
        log(f"ep{episode_id} {freq_mhz:.3f} MHz blank (flatness "
            f"{(bundle.get('features') or {}).get('spectral_flatness')}) - purged, nothing recorded")
        return {"episode_id": episode_id, "status": "blank", "decode": decoded["status"],
                "seconds": round(elapsed, 1)}
    return {"episode_id": episode_id, "status": "captured", "dwell": dwell_actual,
            "decode": decoded["status"], "decode_type": decoded.get("decode_type"),
            "summary": decoded.get("summary"), "seconds": round(elapsed, 1)}


def pass_once(*, dwell: float = DWELL_SECONDS, max_dwell: float = MAX_DWELL_SECONDS,
              min_score: float = MIN_SCORE, max_per_run: int = MAX_PER_RUN,
              serial: str = DEVICE_SERIAL, spur_mask: tuple[int, ...] = SPUR_MASK) -> dict[str, Any]:
    if not serial:
        return {"error": "RF_CAPTURE_DEVICE_SERIAL is not set - refusing to capture on an unpinned radio"}
    queue = pick_queue(max_per_run, min_score)
    if not queue:
        return {"queue": 0, "results": []}
    results = [capture_one(episode, dwell=dwell, max_dwell=max_dwell,
                           serial=serial, spur_mask=spur_mask) for episode in queue]
    return {"queue": len(queue), "results": results}


def stats() -> dict[str, Any]:
    with store.connect() as conn:
        spur = conn.execute("SELECT count(*) FROM episodes WHERE class='spur-masked'").fetchone()[0]
        failed = conn.execute("SELECT count(*) FROM episodes WHERE decode_status='failed'").fetchone()[0]
        captured = conn.execute("SELECT count(*) FROM episodes WHERE capture_id IS NOT NULL").fetchone()[0]
        last = conn.execute(
            "SELECT id, freq_hz, capture_id, decode_type, decoded_summary FROM episodes"
            " WHERE capture_id IS NOT NULL ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
    return {
        "serial_set": bool(DEVICE_SERIAL),
        "dwell_seconds": DWELL_SECONDS, "max_dwell_seconds": MAX_DWELL_SECONDS,
        "min_score": MIN_SCORE, "spur_mask_hz": list(SPUR_MASK),
        "captured_rows": captured, "spur_masked_rows": spur, "failed_rows": failed,
        "latest": dict(last) if last else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RF Monitor capture lane (second radio)")
    parser.add_argument("--once", action="store_true", help="one pass then exit")
    parser.add_argument("--loop", action="store_true", help="keep capturing")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--dwell", type=float, default=DWELL_SECONDS)
    parser.add_argument("--max-dwell", type=float, default=MAX_DWELL_SECONDS)
    parser.add_argument("--min-score", type=float, default=MIN_SCORE)
    parser.add_argument("--max-per-run", type=int, default=MAX_PER_RUN)
    parser.add_argument("--interval", type=float, default=INTERVAL)
    args = parser.parse_args(argv)

    store.init_db()

    if args.stats:
        print(json.dumps(stats(), indent=2))
        return 0

    if args.loop:
        log(f"capture lane up: dwell={args.dwell:.0f}s max={args.max_dwell:.0f}s "
            f"min_score={args.min_score:.0f} interval={args.interval:.0f}s "
            f"serial_pinned={bool(DEVICE_SERIAL)}")
        while True:
            result = pass_once(dwell=args.dwell, max_dwell=args.max_dwell,
                               min_score=args.min_score, max_per_run=args.max_per_run)
            if result.get("error"):
                log(result["error"])
                time.sleep(max(30.0, args.interval))
                continue
            time.sleep(max(5.0, args.interval))
        return 0

    result = pass_once(dwell=args.dwell, max_dwell=args.max_dwell,
                       min_score=args.min_score, max_per_run=args.max_per_run)
    print(json.dumps(result, indent=2))
    return 0 if not result.get("error") else 2


if __name__ == "__main__":
    raise SystemExit(main())