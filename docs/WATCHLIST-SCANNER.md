# Watchlist Wideband Scanner (`rf-scanner`)

Design A for the watchlist: instead of retuning one channel at a time, take one
wide capture and measure every watchlist channel inside it in the same FFT pass,
then demodulate whatever breaks squelch. This replaces `rf-hopper`, which polled
one frequency per 15 s capture and therefore saw each channel ~8% of the time
(measured: 10 entries, 184 s per pass, 3.4 s of process overhead per hop).

Lane: `rf-scanner.service` (user `cmilkosk`, `Nice=5`), dongle **RTL2** on the
D3000N feed, 2.4 MS/s. `rf-hopper` is disabled at boot; do not run both - they
want the same libusb index.

## What one pass does

1. `rf_watchlist.list_entries(enabled_only=True)`, skipping ADS-B (readsb owns it).
2. Group entries into chunks that fit one capture (`RATE - 2*EDGE_HZ` ≈ 1.9 MHz),
   centre each chunk and **nudge the LO 300 kHz** when a watched channel would sit
   within 150 kHz of centre, because the R820T DC spike reads ~+58 dB and would
   otherwise be mistaken for a carrier (or bury a real one).
3. Capture `RF_SCANNER_DWELL_S` seconds, one `rtl_sdr` process per chunk.
4. Per channel: peak bin inside the channel width, compared to **p99.5 of the
   band** - the matched statistic (see below).
5. Anything ≥ `RF_SCANNER_SQUELCH_DB` is demodulated twice, AM and NFM, and the
   first mode that gates as voice (or decodes) wins.
6. Silero VAD on the demodulated audio; if speech ≥1% or a data protocol decoded,
   whisper large-v3 through the Frigate ASR gateway on linux5 writes a transcript.
7. `multimon-ng` runs on every candidate for data (APRS, POCSER/FLEX, EAS, ACARS,
   AIS, FT8).
8. Record an episode for **everything above squelch** - voice, data, or an
   unidentified carrier (`RF_SCANNER_KEEP_SILENT=1`). Nothing is silently dropped.

Measured 2026-11-09 (EST evening), 9 chunks, ~75 s per pass at 5 s dwell:

| Frequency | Result |
|---|---|
| 123.800 KTTN/PHL approach | AM voice 40.4% -> "Down to 25 miles per hour, 741 has the airfield in sight." |
| 162.550 NOAA WX | NFM voice 72-78% -> full marine forecast transcripts |
| 144.390 APRS | recorded as carrier; packets did not decode in the 8 s window |

## Two bugs this lane had to fix

**1. Demod order (`rf_v2_capture._demodulate`).** It discriminates at the full
2.4 MS/s and then boxcars 50 samples, which averages FM voice (noise-like at that
rate) to ~zero. NOAA on a +34 dB carrier gated at **0.0% speech** through that
path and **72.7%** through mix -> decimate -> channel filter -> discriminate
(`rf_scanner.demod_channel`). Use the second order; it matches `rf_decode.ddc_audio`.

**2. Peak vs median reference.** A narrow AM carrier is +34 dB in its own bin but
averages to +6 dB over a 25 kHz window - so a mean-based metric never opened the
squelch. Switching to the peak bin fixed detection but then reported **+12 dB on
pure noise**, because the max of ~1600 noise bins sits ~12 dB above the band
median; every channel tripped an 8 dB squelch. The reference must be the same
statistic: **p99.5 of the band**. Empty channels then read 0-2 dB and a real
carrier reads +10 to +30.

## Console visibility

`/api/episodes` only reads the capture lane's naming, so `record()` also writes
`audio-<mode>.wav`, `audio-<mode>.mp3`, `spectrum.png` and a `capture.json`
(`capture`/`features`/`audio`/`duration_seconds`) alongside the timestamped audit
copies. Verified: `/api/episodes/43081/{meta,audio/nfm,spectrum}` all 200.

## Tuning

`/home/cmilkosk/.config/rf-scanner.env` (0640, holds `RF_SCANNER_ASR_KEY`):

| Var | Default | Meaning |
|---|---|---|
| `RF_SCANNER_DWELL_S` | 5 | capture length per chunk; pass time ≈ chunks × (dwell + 3 s) |
| `RF_SCANNER_SQUELCH_DB` | 8 | dB over band p99.5 to demodulate |
| `RF_SCANNER_COOLDOWN_S` | 90 | don't re-record the same frequency inside this window |
| `RF_SCANNER_MAX_PER_CHUNK` | 3 | cap demod work per chunk |
| `RF_SCANNER_GAIN` | auto | set a dB value for steadier levels on a hot feed |
| `RF_SCANNER_ASR_ENABLE` | 1 | whisper transcription on voice/data |

## Hardware gotcha

RTL2 came back from a USB wedge deaf: `rtl_sdr` claimed the interface, allocated
buffers and delivered **0 bytes**. A `USBDEVFS_RESET` (ioctl 0x5514) on
`/dev/bus/usb/007/<devnum>` restored it, verified by 4.8 MB in 2 s and NOAA at
+34 dB over floor. If a capture silently produces nothing, reset the dongle before
suspecting the antenna. Index map is stable only while nothing re-enumerates:
0 = RTL3, 1 = RTL0 (ADS-B), 2 = RTL2 (this lane).
