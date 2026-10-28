# ATSC probe: can the HackRF decode US TV?

US television is **ATSC 1.0**: 8-VSB, 6 MHz wide, ~19.39 Mbit/s transport stream.
GNU Radio 3.10 ships the entire receive chain — `dtv.atsc_rx` bundles FPLL, sync,
equalizer, Viterbi, Reed–Solomon, derandomizer and depad — so this is a wiring job, not a
DSP project.

```
tools/atsc_probe.py
    HackRF capture 6.25 MS/s (cu8, signed)  ->  complex64
        -> dtv.atsc_rx(6.25e6, sps=1.5)     -> MPEG-TS bytes
        -> ffprobe / ffmpeg                 -> streams, one frame
```

`6.25e6` and `sps=1.5` are not guesses: they are what GNU Radio's own
`examples/dtv/file_atsc_rx.grc` uses.

**Why the HackRF and not an RTL:** an RTL-SDR tops out near 2.4 MS/s and 8-VSB needs the
full 6 MHz. This is the one job here a parked dongle genuinely cannot do.

## Run

```
sudo -u cmilkosk bash -lc 'cd /home/cmilkosk/rf-monitor && \
  set -a && . $HOME/.config/rf-capture-lane.env && set +a && \
  python3 tools/atsc_probe.py --freq 569 --seconds 10 --frame'
```

Exit code 0 = locked and a TS was produced; 1 = no lock. A 10 s capture at 6.25 MS/s
costs 500 MB of complex64 and is deleted afterwards.

## The trap that cost the most time: numpy 2 breaks GNU Radio

`from gnuradio import blocks` failed as `cmilkosk` with a bare
`ImportError: initialization failed`, while working as `hermes`. Two false leads came
first — a stale `~/.gnuradio/prefs/vmcircbuf_default_factory` (innocent) and locale
(innocent).

The real cause, found by strace: **`cmilkosk` resolves numpy 2.2.6 from
`~/.local/lib/python3.10/site-packages`, while GNU Radio 3.10's C extensions are built
against numpy 1.x** (hermes gets 1.21.5 from dist-packages and works). The underlying
error pybind swallows is `numpy.core.multiarray failed to import`.

`PYTHONNOUSERSITE=1` must be set *before the interpreter starts*, so the probe re-execs
itself once with it set. Do not try to fix this by importing numpy first — the flag is
read at startup.

## Result 2026-10-28

| Channel | Capture | Result |
|---|---|---|
| 569 MHz (UHF ch30) | 10 s / 500 MB | **no lock, 0 bytes of transport stream** |

That matches the independent measurements: a 20 s `rtl_power` scan put ch30 at only
+4.7 dB over the band median with narrow peaks rather than a flat 6 MHz plateau, and a
16 MS/s capture of 58–74 MHz showed a broad hump with no pilot spike at any channel edge.

**Not yet proven:** the receive chain has not been validated against a known-good ATSC
signal. "No lock" and "my parameters are wrong" look identical from the outside. The
loopback that settles it is GNU Radio's `atsc_tx` (a transmit flowgraph assembled from
`atsc_randomizer → rs_encoder → interleaver → trellis_encoder → field_sync_mux → RRC`),
fed a synthetic MPEG-TS from ffmpeg, recorded to a file and run back through this probe.
Until that runs, treat the probe as *executed and honest*, not *proven*.
