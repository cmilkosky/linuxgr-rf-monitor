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
    python3 tools/atsc_probe.py --iq-file capture.cfile     # decode a saved 6.25 MS/s capture
    python3 tools/atsc_probe.py --loopback                  # synthetic signal, no station needed

Loopback status, 2026-10-28: gr-dtv 3.10 on LinuxGR ships the ATSC sub-blocks but no
`dtv.atsc_tx`, so the transmitter here is composed from its own file_atsc_tx.grc chain.
It runs and emits 6.25 MS/s IQ (RMS 0.38), but atsc_rx does not lock on it at any sps
tried (1.5, 0.5809, 2.0, 1.0, 0.5), and its spectrum is a DC spike 33 dB above a flat
plateau - i.e. not a valid 8-VSB signal. So the loopback does NOT yet prove the receive
chain; it is the harness for that test, not the proof. The noise control does behave
(pure noise -> 0 bytes out), so a locked station would still show up as real TS bytes.
Until a real capture or a correct transmitter settles it, treat the decoder as unproven.
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
# gr-dtv 3.10 ships the ATSC transmit blocks (pad, randomizer, RS, interleaver, trellis,
# field-sync mux) and MOD_8VSB in dvbs2_modulator_bc, but no monolithic `dtv.atsc_tx`.
# The chain below mirrors its own example, /usr/share/gnuradio/examples/dtv/file_atsc_tx.grc.
ATSC_SYMBOL_RATE = 4.5e6 / 286 * 684          # 10.762 MSym/s
LOOPBACK_TS_SECONDS = 1.0   # ~170 MB of IQ and ~1 min of demod - enough for field-sync lock


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


def make_source_ts(ts: Path, seconds: float) -> dict:
    """A known-good MPEG-TS: MPEG-2 video + AC-3 audio, like a real ATSC 1.0 stream."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
         "-t", f"{seconds}",
         "-c:v", "mpeg2video", "-b:v", "12M", "-minrate", "12M", "-maxrate", "12M",
         "-bufsize", "1M", "-g", "15",
         "-c:a", "ac3", "-b:a", "192k",
         "-f", "mpegts", str(ts)],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0 or not ts.exists():
        raise RuntimeError(f"ffmpeg could not build a source TS: {proc.stderr[-300:]}")
    return {"source_ts_bytes": ts.stat().st_size}


def transmit(ts_in: Path, cf32: Path) -> dict:
    """8-VSB modulate a TS to complex64 at SAMPLE_RATE_HZ - no radio involved."""
    from gnuradio import blocks, dtv, filter, gr
    from gnuradio.filter import firdes

    symbol_rate = ATSC_SYMBOL_RATE
    taps = firdes.root_raised_cosine(0.11, symbol_rate, symbol_rate / 2, 0.1152, 200)

    class Tx(gr.top_block):
        def __init__(self) -> None:
            gr.top_block.__init__(self, "atsc_tx")
            src = blocks.file_source(gr.sizeof_char, str(ts_in), False)
            chain = [
                dtv.atsc_pad(),
                dtv.atsc_randomizer(),
                dtv.atsc_rs_encoder(),
                dtv.atsc_interleaver(),
                dtv.atsc_trellis_encoder(),
                dtv.atsc_field_sync_mux(),
                blocks.vector_to_stream(gr.sizeof_char, 1024),
                blocks.keep_m_in_n(gr.sizeof_char, 832, 1024, 4),
                dtv.dvbs2_modulator_bc(dtv.FECFRAME_NORMAL, dtv.C1_4,
                                       dtv.MOD_8VSB, dtv.INTERPOLATION_OFF),
                filter.fft_filter_ccc(1, taps),
                # 10.762 MSym/s -> 6.25 MS/s, the rate atsc_rx is told to expect.
                filter.rational_resampler_ccc(625, 1076),
            ]
            sink = blocks.file_sink(gr.sizeof_gr_complex, str(cf32), False)
            self.connect(src, *chain, sink)

    Tx().run()
    return {"tx_bytes": cf32.stat().st_size if cf32.exists() else 0}


def noise_control(cf32: Path, ts: Path, seed: int = 11) -> dict:
    """Negative control: the same receive chain over pure noise must NOT produce a stream."""
    rng = np.random.default_rng(seed)
    samples = int(SAMPLE_RATE_HZ * 1.0)
    noise = (rng.normal(0, 0.05, samples) + 1j * rng.normal(0, 0.05, samples)).astype(np.complex64)
    noise.tofile(cf32)
    out = demodulate(cf32, ts)
    verdict = probe_ts(ts, False)
    return {"noise_ts_bytes": out["ts_bytes"], "noise_valid": verdict.get("valid", False),
            "noise_reason": verdict.get("reason")}


def loopback(workdir: Path, frame: bool) -> dict:
    """Prove the receive chain end to end without a station: build, modulate, demodulate."""
    source = workdir / "source.ts"
    cf32 = workdir / "loopback.cfile"
    ts = workdir / "loopback.ts"
    report: dict = {"mode": "loopback", "symbol_rate_hz": round(ATSC_SYMBOL_RATE, 1)}
    report.update(make_source_ts(source, LOOPBACK_TS_SECONDS))
    report.update(transmit(source, cf32))
    report.update(demodulate(cf32, ts))
    report.update(probe_ts(ts, frame))
    report.update(noise_control(workdir / "noise.cfile", workdir / "noise.ts"))
    return report


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
    parser.add_argument("--freq", type=float, help="channel centre in MHz")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--frame", action="store_true", help="extract one frame if it locks")
    parser.add_argument("--keep", action="store_true", help="keep the working files")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--iq-file", type=Path,
                        help="decode an existing complex64 capture at 6.25 MS/s instead of the radio")
    parser.add_argument("--loopback", action="store_true",
                        help="prove the receive chain on a synthetic ATSC signal, no station needed")
    args = parser.parse_args(argv)
    if not args.loopback and not args.iq_file and args.freq is None:
        parser.error("--freq is required unless --loopback or --iq-file is given")

    workdir = Path(tempfile.mkdtemp(prefix="atsc-probe-"))
    report: dict = {"sample_rate_hz": SAMPLE_RATE_HZ, "sps": SPS}
    try:
        if args.loopback:
            report.update(loopback(workdir, args.frame))
        else:
            if args.iq_file:
                cf32 = args.iq_file.expanduser().resolve()
                if not cf32.exists():
                    parser.error(f"no such IQ file: {cf32}")
                report["iq_file"] = str(cf32)
            else:
                freq_hz = int(args.freq * 1e6)
                report.update({"freq_mhz": args.freq, "seconds": args.seconds})
                cf32, _ = capture(freq_hz, args.seconds, workdir)
            report["capture_bytes"] = cf32.stat().st_size
            ts = workdir / "mpeg.ts"
            report.update(demodulate(cf32, ts))
            report.update(probe_ts(ts, args.frame))
        if report.get("frame"):
            dest = Path.cwd() / ("atsc-%s.png" % (
                "loopback" if args.loopback else "%.3fMHz" % args.freq))
            shutil.copy(report["frame"], dest)
            report["frame"] = str(dest)
    finally:
        if args.keep:
            report["workdir"] = str(workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    if args.json:
        print(json.dumps(report, indent=2))
    elif args.loopback:
        print("loopback: %s" % ("source TS %d B -> 8-VSB %.1f MSym/s -> %.1f MB IQ" % (
            report.get("source_ts_bytes", 0), ATSC_SYMBOL_RATE / 1e6,
            report.get("tx_bytes", 0) / 1e6)))
        print("  decoded TS: %d bytes" % report.get("ts_bytes", 0))
        if report.get("valid"):
            print("  LOCKED: %d stream(s)" % len(report["streams"]))
            for stream in report["streams"]:
                print("  stream %s: %s %s %sx%s" % (
                    stream.get("index"), stream.get("codec_type"), stream.get("codec_name"),
                    stream.get("width"), stream.get("height")))
            if report.get("frame"):
                print("  frame:", report["frame"])
        else:
            print("  NO LOCK: %s (ts_bytes=%s)" % (report.get("reason"), report.get("ts_bytes")))
        print("  negative control (noise): %s%s" % (
            "correctly found no stream" if not report.get("noise_valid") else "LOCKED ON NOISE - chain is lying",
            "" if report.get("noise_valid") else " (%d B out)" % report.get("noise_ts_bytes", 0)))
    else:
        label = report.get("iq_file") or "%.3f MHz" % (report.get("freq_mhz") or 0)
        print("%s  %s" % (label, "%.1f MB" % (report.get("capture_bytes", 0) / 1e6)))
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
