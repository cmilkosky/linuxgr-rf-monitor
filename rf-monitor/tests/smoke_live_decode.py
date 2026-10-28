#!/usr/bin/env python3
"""Live end-to-end: production capture path -> decode stage, on real air.

Runs capture_and_analyse with the same decode callback the scout uses, so this
exercises the fixed audio demodulation and the IQ down-conversion together.
Default target is APRS on 144.390 MHz; pass a frequency in MHz to try another.

    RF_CAPTURE_SECONDS=30 python3 tests/smoke_live_decode.py [freq_mhz]
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/cmilkosk/rf-monitor")

import rf_decode  # noqa: E402
import rf_v2_capture as capture  # noqa: E402

FREQ_MHZ = float(sys.argv[1]) if len(sys.argv) > 1 else 144.39
OUT = Path("/tmp/rf-live-decode")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    target = int(FREQ_MHZ * 1e6)
    episode = {"id": 0, "freq_hz": target, "bandwidth_hz": 100_000}
    print(f"capture {FREQ_MHZ:.3f} MHz for {os.environ.get('RF_CAPTURE_SECONDS', '4')}s", flush=True)
    bundle = capture.capture_and_analyse(
        episode, OUT, keep_iq=False,
        decode_cb=lambda iq, sr, tune, out: rf_decode.decode_iq_hits(iq, sr, tune, target),
    )
    print(f"  lo_offset_hz = {bundle['capture'].get('lo_offset_hz')}")
    for mode, info in (bundle.get("audio") or {}).items():
        print(f"  audio-{mode}: rms={info.get('rms')} bytes={info.get('mp3_bytes') or info.get('bytes')}")
    hits = bundle.get("decode") or []
    if isinstance(hits, dict):
        print("  decode error:", hits)
        return 1
    print(f"decode hits: {len(hits)}")
    for hit in hits[:6]:
        print(f"  {hit['protocol']}: {hit['line'][:140]}")
    if hits:
        print("PASS: live capture decoded")
        return 0
    print("no frames decoded in this window")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
