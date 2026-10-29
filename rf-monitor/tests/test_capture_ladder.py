"""Gain step-down + extend-on-progress used to collide on chunk filenames.

Regression: when attempt 0 clips, the ladder steps the gain down and selects
capture.part1.iq. The extend-on-progress loop then named its chunk
capture.part{len(parts)}.iq = capture.part1.iq as well, so `parts` held the same
path twice; concatenation read it, unlinked it, then read it again and raised
FileNotFoundError: capture.part1.iq. Every overloaded low-VHF capture died that
way and left no artifacts at all.

Run: python3 tests/test_capture_ladder.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rf_v2_capture as cap  # noqa: E402

RATE = 2_000_000
_STATE = {"calls": 0}


def _fake_capture(frequency_hz, out_iq, *, duration_seconds=4.0, sample_rate_hz=RATE,
                  lna_gain_db=32, vga_gain_db=24, amp_enable=0,
                  device_serial=None, lock_path=None):
    """Attempt 0 clips hard, every retry is clean - exactly what a strong carrier does."""
    _STATE["calls"] += 1
    amp = 1.0 if _STATE["calls"] == 1 else 0.05
    n = int(sample_rate_hz * duration_seconds)
    t = np.arange(n)
    tone = amp * np.exp(2j * np.pi * 0.01 * t)
    # hackrf_transfer writes signed 8-bit interleaved IQ; read_iq reads int8 / 128.
    interleaved = np.empty(2 * n, dtype=np.int8)
    interleaved[0::2] = np.clip(np.round(tone.real * 128), -128, 127).astype(np.int8)
    interleaved[1::2] = np.clip(np.round(tone.imag * 128), -128, 127).astype(np.int8)
    out_iq.parent.mkdir(parents=True, exist_ok=True)
    interleaved.tofile(out_iq)
    return {"path": str(out_iq), "bytes": out_iq.stat().st_size,
            "seconds": duration_seconds, "lna_db": lna_gain_db, "vga_db": vga_gain_db,
            "sample_rate_hz": sample_rate_hz}


def main() -> int:
    real_capture, real_present = cap.capture_hackrf, cap.signal_present
    cap.capture_hackrf = _fake_capture
    cap.signal_present = lambda samples, min_db=12.0: True
    tmp = Path(tempfile.mkdtemp(prefix="cap-ladder-"))
    out = tmp / "ep1"
    try:
        bundle = cap.capture_and_analyse(
            {"id": 1, "freq_hz": 26_000_000, "bandwidth_hz": 1_900_000},
            out, keep_iq=True, duration_seconds=1.0, max_duration_seconds=3.0)
    finally:
        cap.capture_hackrf, cap.signal_present = real_capture, real_present

    iq_path = Path(bundle["artifacts"][-1]["iq"])
    seconds = bundle["duration_seconds"]
    leftovers = sorted(p.name for p in out.glob("capture.part*.iq"))
    print(f"gains={bundle['capture']['gains']} chunks={_STATE['calls']} "
          f"iq_seconds={seconds:.2f} rate={bundle['capture'].get('sample_rate_hz')} "
          f"leftovers={leftovers}")
    assert seconds >= 1.9, f"base + one extend chunk should be ~2 s, got {seconds:.2f}s"
    assert iq_path.exists() and iq_path.stat().st_size > 0, "no concatenated IQ written"
    assert not leftovers, f"chunk files left behind: {leftovers}"
    print("PASS: step-down plus extend produced both chunks and no orphan parts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
