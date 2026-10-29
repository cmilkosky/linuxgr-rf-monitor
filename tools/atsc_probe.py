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
    python3 tools/atsc_probe.py --iq-file capture.cfile [--iq-rate 10.762] [--iq-sps 2.0]
    python3 tools/atsc_probe.py --loopback                  # synthetic signal, no station needed

Receive chain PROVEN 2026-10-29, not assumed. gr-dtv 3.10 ships no ATSC transmitter (its
`dvbs2_modulator_bc(MOD_8VSB)` is only an 8PSK symbol map, so composing one from the
gr-dtv sub-blocks yields a signal that looks plausible and never locks), so --loopback
builds the philburr/atsc reference modulator and feeds its output to `atsc_rx`:

    10.762237 MS/s, sps 2.0  -> LOCK, mpeg2video 1280x720 + ac3
    10.762237 MS/s, sps 1.0  -> streams, 0x0 dimensions (header not clean)
    21.524 MS/s (2x), sps 2.0 -> streams, 0x0; sps 1.0 -> nothing
     6.25 MS/s, sps 1.5 / 0.5809 / 0.5 -> nothing at all

Two consequences. The capture rate must be the ATSC symbol rate (~10.762 MS/s, inside the
HackRF's 2-20 MS/s range), NOT 6.25 MS/s: at 6.25 MS/s this chain cannot decode even a
perfect signal, so every earlier live "no lock" measured nothing about the station. And
pure noise still yields 0 bytes, so a lock means a real signal was decoded.

What the loopback proves, exactly: the receive chain locks, demaps, RS-decodes and emits a
playable MPEG-2 + AC-3 transport stream (1280x720, timestamp overlay readable). It does NOT
prove a clean link - every frame carries macroblock errors (162 damaged-macroblock lines
across ~90 frames in a 3 s decode, at t=0.5/1.0/1.5/2.0 s alike, so it is not just
acquisition). sps 1.0 gives a parseable header-less stream, sps 2.0 the best result, and
2x-oversampled input nothing at all, so this modulator and gr-dtv's receiver are not
perfectly matched. A live lock is therefore evidence of a station, not of picture quality.
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

SAMPLE_RATE_HZ = 6_250_000  # legacy: kept only so old callers still import
DEVICE_ENV = "RF_CAPTURE_DEVICE_SERIAL"
# Measured 2026-10-29 against a reference transmitter (philburr/atsc, a real ATSC 8VSB
# modulator): atsc_rx locks ONLY when the input rate matches the ATSC symbol rate.
#   10.762237 MS/s, sps 2.0 -> full 1280x720 mpeg2video + ac3
#   10.762237 MS/s, sps 1.0 -> streams but 0x0 (sequence header not clean)
#   21.524 MS/s (2x), sps 2.0 -> streams but 0x0; sps 1.0 -> nothing
#    6.25 MS/s, sps 1.5 / 0.5809 / 0.5 -> nothing at all
# So the old 6.25 MS/s + sps 1.5 setting could never decode, even on a perfect signal.
ATSC_SYMBOL_RATE = 4.5e6 / 286 * 684          # 10.762237 MS/s
CAPTURE_RATE_HZ = int(round(ATSC_SYMBOL_RATE))
CAPTURE_SPS = 2.0
REF_TX_DIR = Path(os.environ.get("ATSC_REF_TX_DIR", "/tmp/atsc-ref"))
LOOPBACK_TS_SECONDS = 1.0


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
        sample_rate_hz=CAPTURE_RATE_HZ,
        device_serial=device_serial(),
        lock_path=rf_v2_capture.PORTAPACK_LOCK_PATH,
    )
    iq = rf_v2_capture.read_iq(cu8)
    cu8.unlink(missing_ok=True)
    np.ascontiguousarray(iq, dtype=np.complex64).tofile(cf32)
    return cf32, cu8


def demodulate(cf32: Path, ts: Path, rate: float = CAPTURE_RATE_HZ,
               sps: float = CAPTURE_SPS) -> dict:
    """Run gr-dtv's ATSC receive pipeline over the capture."""
    from gnuradio import blocks, dtv, gr

    class Chain(gr.top_block):
        def __init__(self) -> None:
            gr.top_block.__init__(self, "atsc_rx")
            src = blocks.file_source(gr.sizeof_gr_complex, str(cf32), False)
            rx = dtv.atsc_rx(rate, sps)
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
         "-muxrate", "19393000",          # the modulator needs a 19.39 Mbit/s TS
         "-f", "mpegts", str(ts)],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0 or not ts.exists():
        raise RuntimeError(f"ffmpeg could not build a source TS: {proc.stderr[-300:]}")
    return {"source_ts_bytes": ts.stat().st_size}


def build_reference_tx() -> Path:
    """Compile the philburr/atsc reference modulator (real 8VSB: randomizer, RS(207,187),
    interleaver, trellis, field sync, RRC, pilot). gr-dtv 3.10 has no ATSC transmitter -
    its `MOD_8VSB` is just an 8PSK map - so a loopback needs this one. Cached in REF_TX_DIR."""
    binary = REF_TX_DIR / "atsc_encoder_file"
    if binary.exists():
        return binary
    src_dir = REF_TX_DIR / "atsc"
    REF_TX_DIR.mkdir(parents=True, exist_ok=True)
    if not (src_dir / ".git").exists():
        clone = subprocess.run(["git", "clone", "--depth", "1",
                                "https://github.com/philburr/atsc.git", str(src_dir)],
                               capture_output=True, text=True, timeout=300)
        if clone.returncode != 0:
            raise RuntimeError(f"could not clone philburr/atsc: {clone.stderr[-300:]}")
    harness = Path(__file__).resolve().parent / "atsc_ref_tx.cpp"
    if not harness.exists():
        raise RuntimeError(f"reference harness missing: {harness}")
    build = subprocess.run(
        ["g++", "-O2", "-std=c++17", "-mavx2", "-mfma", "-DHAVE_FMA=1",
         "-I", str(src_dir / "src"), str(harness), str(src_dir / "src/atsc/atsc.cpp"),
         "-o", str(binary)],
        capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0 or not binary.exists():
        raise RuntimeError(f"could not build reference modulator: {build.stderr[-400:]}")
    return binary


def transmit(ts_in: Path, cf32: Path) -> dict:
    """8-VSB modulate a TS to complex64 at the ATSC symbol rate - no radio involved."""
    binary = build_reference_tx()
    proc = subprocess.run([str(binary), str(ts_in), str(cf32)],
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not cf32.exists():
        raise RuntimeError(f"reference modulator failed: {proc.stderr[-300:]}")
    return {"tx_bytes": cf32.stat().st_size,
            "tx_rate_hz": CAPTURE_RATE_HZ,
            "tx_summary": (proc.stdout or "").strip()[-160:]}


def noise_control(cf32: Path, ts: Path, seed: int = 11) -> dict:
    """Negative control: the same receive chain over pure noise must NOT produce a stream."""
    rng = np.random.default_rng(seed)
    samples = int(CAPTURE_RATE_HZ * 1.0)
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
                        help="decode an existing complex64 capture instead of the radio")
    parser.add_argument("--iq-rate", type=float, default=CAPTURE_RATE_HZ / 1e6,
                        help="sample rate of --iq-file in MHz (default: ATSC symbol rate)")
    parser.add_argument("--iq-sps", type=float, default=CAPTURE_SPS,
                        help="samples per symbol for atsc_rx (default 2.0, measured)")
    parser.add_argument("--loopback", action="store_true",
                        help="prove the receive chain on a synthetic ATSC signal, no station needed")
    args = parser.parse_args(argv)
    if not args.loopback and not args.iq_file and args.freq is None:
        parser.error("--freq is required unless --loopback or --iq-file is given")

    workdir = Path(tempfile.mkdtemp(prefix="atsc-probe-"))
    report: dict = {"sample_rate_hz": CAPTURE_RATE_HZ, "sps": CAPTURE_SPS}
    try:
        if args.loopback:
            report.update(loopback(workdir, args.frame))
        else:
            rate = args.iq_rate * 1e6
            sps = args.iq_sps
            if args.iq_file:
                cf32 = args.iq_file.expanduser().resolve()
                if not cf32.exists():
                    parser.error(f"no such IQ file: {cf32}")
                report["iq_file"] = str(cf32)
            else:
                freq_hz = int(args.freq * 1e6)
                report.update({"freq_mhz": args.freq, "seconds": args.seconds})
                cf32, _ = capture(freq_hz, args.seconds, workdir)
            report["sample_rate_hz"] = rate
            report["sps"] = sps
            report["capture_bytes"] = cf32.stat().st_size
            ts = workdir / "mpeg.ts"
            report.update(demodulate(cf32, ts, rate=rate, sps=sps))
            report.update(probe_ts(ts, args.frame))
        if report.get("frame"):
            if args.loopback:
                tag = "loopback"
            elif args.iq_file:
                tag = args.iq_file.stem
            else:
                tag = "%.3fMHz" % args.freq
            dest = Path.cwd() / ("atsc-%s.png" % tag)
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
