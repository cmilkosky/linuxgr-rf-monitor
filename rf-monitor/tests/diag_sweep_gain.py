"""Measure hackrf_sweep noise floor vs gain, and check front-end overload.

Takes the same flock the collector uses, so the live sweep is not disturbed
beyond one pass at a time. Prints min / median / max dB per gain setting.
"""

import fcntl
import subprocess

import numpy as np

LOCK = "/tmp/hackrf-monitor-device.lock"


def sweep(lna, vga, amp, fmin, fmax, width=1_000_000):
    cmd = ["hackrf_sweep", "-f", f"{fmin}:{fmax}", "-w", str(width), "-1",
           "-l", str(lna), "-g", str(vga), "-a", str(amp)]
    with open(LOCK, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    values = []
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) > 7 and parts[0][:2] == "20":
            values.extend(float(x) for x in parts[6:] if x.strip())
    return np.array(values) if values else np.array([np.nan])


print(f"{'gain':>12} {'band':>12} {'n':>5} {'min':>7} {'p50':>7} {'max':>7} {'span':>6}")
for lna, vga, amp in [(16, 16, 0), (8, 8, 0), (0, 0, 0), (16, 16, 1), (32, 24, 0), (8, 16, 0), (40, 32, 0)]:
    for fmin, fmax in [(88, 108), (300, 320)]:
        v = sweep(lna, vga, amp, fmin, fmax)
        p50 = float(np.nanmedian(v))
        print(f"lna={lna:2d}/vga={vga:2d}/{amp} {fmin:3d}-{fmax:3d}MHz {v.size:5d} "
              f"{np.nanmin(v):7.1f} {p50:7.1f} {np.nanmax(v):7.1f} {np.nanmax(v)-p50:6.1f}")
