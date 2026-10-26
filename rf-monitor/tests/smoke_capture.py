"""Smoke test: one real HackRF capture through the v2 capture path.

Run on linuxGR as cmilkosk:
    python3 tests/smoke_capture.py [freq_mhz]
Verifies flock coordination with the sweep, spectrogram, feature extraction and
all three audio demodulators, and prints the artifact sizes.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/cmilkosk/rf-monitor")

import rf_v2_capture as capture  # noqa: E402

FREQ_MHZ = float(sys.argv[1]) if len(sys.argv) > 1 else 100.5
OUT = Path("/tmp/rf-v2-test")

bundle = capture.capture_and_analyse(
    {"freq_hz": int(FREQ_MHZ * 1e6), "bandwidth_hz": 200_000}, OUT, keep_iq=False
)
features = bundle["features"]
report = {
    "freq_mhz": FREQ_MHZ,
    "capture_seconds": bundle["capture"]["duration_seconds"],
    "iq_bytes": bundle["capture"]["iq_bytes"],
    "iq_deleted": not (OUT / "capture.iq").exists(),
    "samples": bundle["samples"],
    "features": {
        k: features[k]
        for k in (
            "occupied_bandwidth_hz",
            "peak_frequency_hz",
            "traits",
            "spectral_flatness",
            "instantaneous_frequency_std_hz",
            "duty_cycle",
            "burstiness",
            "mean_power_dbfs",
        )
    },
    "audio": {
        mode: {"bytes": info.get("bytes"), "seconds": info.get("duration_seconds"), "rms": info.get("rms")}
        for mode, info in bundle["audio"].items()
    },
    "files": sorted(f"{p.name}={p.stat().st_size}" for p in OUT.iterdir()),
}
print(json.dumps(report, indent=1))
