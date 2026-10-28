#!/usr/bin/env python3
"""Live check: capture APRS on 144.390 and decode it.

Synthetic AFSK did not satisfy multimon-ng's AFSK1200 clock recovery, so this
tests the leg the way it will actually be used: a real 30 s capture of the APRS
channel through the production capture path, demodulated to NFM audio, then run
through rf_decode. Takes the HackRF device flock, so the sweep pauses for the
duration (normal behaviour for the scout's own captures).

    python3 tests/smoke_aprs_air.py [seconds]
"""

import sys
from pathlib import Path

sys.path.insert(0, "/home/cmilkosk/rf-monitor")

import rf_decode  # noqa: E402
import rf_v2_capture as capture  # noqa: E402

FREQ_HZ = 144_390_000
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
OUT = Path("/tmp/rf-aprs-air")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    iq_path = OUT / "aprs.iq"
    print(f"capturing {FREQ_HZ/1e6:.3f} MHz for {SECONDS:.0f}s ...", flush=True)
    meta = capture.capture_hackrf(FREQ_HZ + 250_000, iq_path,
                                 duration_seconds=SECONDS, sample_rate_hz=2_000_000)
    print(f"  captured {meta['iq_bytes']} bytes", flush=True)
    iq = capture.remove_dc(capture.read_iq(iq_path))
    audio = capture.audio_previews(iq, 2_000_000, OUT)
    nfm = audio.get("nfm", {})
    path = Path(nfm.get("mp3") or nfm.get("wav") or "")
    print(f"  nfm audio: {path} ({nfm.get('mp3_bytes') or nfm.get('bytes')} bytes)", flush=True)
    if not path.exists():
        print("FAIL: no NFM audio produced")
        return 1
    hits = rf_decode.decode_audio_file(path, FREQ_HZ)
    print(f"APRS hits: {len(hits)}")
    for hit in hits[:10]:
        print(f"  {hit['protocol']}: {hit['line'][:160]}")
    if hits:
        print("summary:", rf_decode.summarise(hits)[:300])
        print("PASS: AFSK1200 leg decodes real air traffic")
        return 0
    print("INCONCLUSIVE: no APRS beacon in this window (channel may have been quiet)")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
