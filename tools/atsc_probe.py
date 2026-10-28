#!/usr/bin/env python3
"""ATSC probe: capture a 6 MHz TV channel and try to demodulate it to MPEG-TS.

US television is ATSC 1.0: 8-VSB, 6 MHz wide, ~19.39 Mbit/s transport stream. GNU
Radio ships the whole receive chain (`dtv.atsc_rx`: FPLL, sync, equalizer, Viterbi,
Reed-Solomon, derandomizer, depad), and its own example `file_atsc_rx.grc` runs it at
6.25 MS/s with sps 1.5 - the values used here.

Why the HackRF and not an RTL: an RTL-SDR tops out near 2.4 MS/s, and 8-VSB needs the
full 6 MHz. This is the one job on this box an RTL genuinely cannot do.

What this does NOT do is guess. If the channel is noise, the pipeline emits nothing and
the probe says so; a TV picture only appears if there is a real station to lock onto.

    python3 tools/atsc_probe.py --freq 569 --seconds 10
    python3 tools/atsc_probe.py --freq 66 --seconds 10 --frame
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

# GNU Radio 3.10's C extensions are built against numpy 1.x, and this host has
# numpy 2.2.6 in cmilkosk's user site. With it active, `from gnuradio import blocks`
# dies as "ImportError: initialization failed" - the real error underneath is
# "numpy.core.multiarray failed to import". User-site has to be disabled before the
# interpreter starts, so re-exec once instead of trying to fix it after import.
if os.environ.get("PYTHONNOUSERSITE") != "1":
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])

_HERE = Path(__file__).resolve()
for _candidate in (_HERE.parents[1] / "rf-monitor", _HERE.parents[1], _HERE.parent):
    if (_candidate / "rf_v2_capture.py").exists():
        sys.path.insert(0, str(_candidate))
        break

import rf_v2_capture  # noqa: E402

SAMPLE_RATE_HZ = 6_250_000  # what gr-dtv's file_atsc_rx.grc expects
SPS = 1.5
DEVICE_ENV = "RF_CAPTURE_DEVICE_SERIAL"


def device_serial() -> str:
    serial = os.environ.get(DEVICE_ENV)
    if not serial:
        raise SystemExit(f"{DEVICE_ENV} is not set (source the capture lane env file)")
    return serial


def capture(freq_hz: int, seconds: float, workdir: Path) -> tuple[Path, Path]:
    """HackRF capture -> complex64 file gr-dtv can read."""
    cu8 = workdir / "atsc.cu8"
    cf32 = workdir / "atsc.cfile"
    rf_v2_capture.capture_hackrf(
        freq_hz, cu8,
        duration_seconds=seconds,
        sample_rate_hz=SAMPLE_RATE_HZ,
        device_serial=device_serial(),
        lock_path=rf_v2_capture.PORTAPACK_LOCK_PATH,
    )
    iq = rf_v2_capture.read_iq(cu8)
    cu8.unlink(missing_ok=True)
    np.ascontiguousarray(iq, dtype=np.complex64).tofile(cf32)
    return cf32, cu8


def demodulate(cf32: Path, ts: Path) -> dict:
    """Run gr-dtv's ATSC receive pipeline over the capture."""
    from gnuradio import blocks, dtv, gr

    class Chain(gr.top_block):
        def __init__(self) -> None:
            gr.top_block.__init__(self, "atsc_rx")
            src = blocks.file_source(gr.sizeof_gr_complex, str(cf32), False)
            rx = dtv.atsc_rx(SAMPLE_RATE_HZ, SPS)
            sink = blocks.file_sink(gr.sizeof_char, str(ts), False)
            self.connect(src, rx, sink)

    Chain().run()
    return {"ts_bytes": ts.stat().st_size if ts.exists() else 0}


def probe_ts(ts: Path, frame: bool) -> dict:
    """Ask ffprobe whether anything came out, and optionally pull one frame."""
    if not ts.exists() or ts.stat().st_size < 188 * 4:
        return {"valid": False, "reason": "no transport stream produced (no lock)"}
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=index,codec_name,codec_type,width,height",
         "-of", "json", str(ts)],
        capture_output=True, text=True, timeout=120,
    )
    try:
        info = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {"valid": False, "reason": "ffprobe could not parse output", "stderr": out.stderr[-200:]}
    streams = info.get("streams", [])
    result = {"valid": bool(streams), "streams": streams,
              "reason": None if streams else "ffprobe found no streams"}
    if frame and streams:
        png = ts.with_suffix(".png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(ts),
                        "-frames:v", "1", "-f", "image2", str(png)],
                       capture_output=True, timeout=180)
        result["frame"] = str(png) if png.exists() else None
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ATSC 8-VSB probe")
    parser.add_argument("--freq", type=float, required=True, help="channel centre in MHz")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--frame", action="store_true", help="extract one frame if it locks")
    parser.add_argument("--keep", action="store_true", help="keep the working files")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    freq_hz = int(args.freq * 1e6)
    workdir = Path(tempfile.mkdtemp(prefix="atsc-probe-"))
    report: dict = {"freq_mhz": args.freq, "seconds": args.seconds,
                    "sample_rate_hz": SAMPLE_RATE_HZ, "sps": SPS}
    try:
        cf32, _ = capture(freq_hz, args.seconds, workdir)
        report["capture_bytes"] = cf32.stat().st_size
        ts = workdir / "mpeg.ts"
        report.update(demodulate(cf32, ts))
        report.update(probe_ts(ts, args.frame))
        if report.get("frame"):
            dest = Path.cwd() / f"atsc-{args.freq:.3f}MHz.png"
            shutil.copy(report["frame"], dest)
            report["frame"] = str(dest)
    finally:
        if args.keep:
            report["workdir"] = str(workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("%.3f MHz  %5.1f s capture  %s" % (
            args.freq, args.seconds, "%.1f MB" % (report.get("capture_bytes", 0) / 1e6)))
        if report.get("valid"):
            print("  LOCKED: transport stream %d bytes" % report["ts_bytes"])
            for stream in report["streams"]:
                print("  stream %s: %s %s %sx%s" % (
                    stream.get("index"), stream.get("codec_type"), stream.get("codec_name"),
                    stream.get("width"), stream.get("height")))
            if report.get("frame"):
                print("  frame:", report["frame"])
        else:
            print("  NO LOCK: %s (ts_bytes=%s)" % (report.get("reason"), report.get("ts_bytes")))
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    sys.exit(main())
