"""Chain A/B: measure the Flamingo+ / LaNA effect on the same four references.

Run once with the chain connected and once with it bypassed; each run records an
absolute, gain-independent snapshot so the two can be differenced honestly.

Absolute power matters here: read_iq() hands back IQ normalised to full scale, so
mean(|x|^2) is a true dBFS number. A 20 dB LNA ahead of the receiver must lift that
floor by roughly its gain - if it does not, the amplifier is not in circuit or is
not powered, whatever the SNR numbers elsewhere suggest.

    python3 rf_chain_ab.py --label both      # chain connected
    python3 rf_chain_ab.py --label bypass    # Flamingo+ and LaNA jumpered out
    python3 rf_chain_ab.py --compare both bypass
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/cmilkosk/rf-monitor")
import rf_v2_capture as cap  # noqa: E402

REFS = [
    (121_500_000, "airband 121.5"),
    (156_800_000, "VHF marine 156.8"),
    (433_920_000, "UHF 433.92"),
    (1_240_000_000, "L-band 1240"),
]
RATE = 2_000_000
SECONDS = 5.0
LNA_GAIN = 32
VGA_GAIN = 24
OUT_DIR = Path("/home/cmilkosk/rf-monitor/ab")


def measure(center: int) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="ab-"))
    iq_path = tmp / "s.iq"
    try:
        with cap.device_lock(180.0, cap.PORTAPACK_LOCK_PATH):
            cap.capture_hackrf(center, iq_path, duration_seconds=SECONDS, sample_rate_hz=RATE,
                               lna_gain_db=LNA_GAIN, vga_gain_db=VGA_GAIN,
                               device_serial=os.environ.get("RF_CAPTURE_DEVICE_SERIAL"), lock_path=None)
        iq = cap.read_iq(iq_path)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    power_dbfs = float(10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-20))
    frame = 4096
    n = (iq.size // frame) * frame
    spec = np.abs(np.fft.fftshift(np.fft.fft(iq[:n].reshape(-1, frame), axis=1), axes=1)) ** 2
    p = spec.mean(axis=0)
    p_db = 10 * np.log10(p + 1e-30)
    floor = float(np.median(p_db))
    peak = float(np.percentile(p_db, 99.99))
    return {"center_hz": center, "power_dbfs": round(power_dbfs, 2),
            "bin_floor_db": round(floor, 2), "bin_peak_db": round(peak, 2),
            "peak_over_floor_db": round(peak - floor, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label")
    ap.add_argument("--compare", nargs=2)
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.compare:
        a, b = args.compare
        pa, pb = OUT_DIR / f"{a}.json", OUT_DIR / f"{b}.json"
        if not (pa.exists() and pb.exists()):
            print("missing %s" % (pa if not pa.exists() else pb))
            return 1
        da, db = json.loads(pa.read_text()), json.loads(pb.read_text())
        print("%-16s %-12s %-12s %-10s %-12s %-12s" % ("reference", "power_A", "power_B", "delta_dB",
                                                       "snr_A", "snr_B"))
        for r in REFS:
            name = r[1]
            ra = next((x for x in da["results"] if x["center_hz"] == r[0]), None)
            rb = next((x for x in db["results"] if x["center_hz"] == r[0]), None)
            if not (ra and rb):
                continue
            print("%-16s %-12.1f %-12.1f %-+10.1f %-12.1f %-12.1f"
                  % (name, ra["power_dbfs"], rb["power_dbfs"],
                     ra["power_dbfs"] - rb["power_dbfs"],
                     ra["peak_over_floor_db"], rb["peak_over_floor_db"]))
        print("\npower_A - power_B = gain difference (A=%s, B=%s)" % (a, b))
        return 0

    label = args.label or "run"
    results = []
    for center, name in REFS:
        r = measure(center)
        r["name"] = name
        results.append(r)
        print("%-16s power=%7.1f dBFS  floor=%7.1f  peak/floor=%5.1f dB"
              % (name, r["power_dbfs"], r["bin_floor_db"], r["peak_over_floor_db"]), flush=True)
    out = OUT_DIR / f"{label}.json"
    out.write_text(json.dumps({"label": label, "when_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "lna_gain_db": LNA_GAIN, "vga_gain_db": VGA_GAIN,
                               "rate_hz": RATE, "seconds": SECONDS, "results": results}, indent=2))
    print("saved", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
