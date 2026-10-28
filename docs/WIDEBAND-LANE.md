# Wideband lane: one capture, a whole band of channels

The capture lane listens to one frequency at a time. That is the wrong shape for a
band where the interesting things are far apart but all inside one HackRF window.
This lane takes one wide capture and decodes every channel in a band plan out of
it.

```
capture 144-148 MHz @ 5 MS/s  ->  DDC each channel to baseband
    ->  multimon-ng per channel  ->  episodes for whatever decodes
```

Runs on the PortaPack (`…22a7`) with its own flock, so the sweep is never paused
and the capture lane and this lane take turns on the radio instead of fighting.

## Bands

| Band | Span | Rate | Channels |
|---|---|---|---|
| `vhf-144` | 144–148 MHz | 5 MS/s | APRS 144.390, packet 145.010, 145.050 |
| `wx-162` | 162.400–162.550 | 2 MS/s | 7 NOAA weather channels, 25 kHz spacing |
| `pager-929` | 929.6–930.0 | 2 MS/s | 5 paging channels |

A 4 MHz band needs 5 MS/s, not 20: the extra bandwidth would cost 4× the RAM and
decode time for spectrum nobody asked about.

## How a pass works

1. Capture `RF_WIDEBAND_DWELL_SECONDS` (default 6 s) to `/tmp`, then delete the IQ
   — a 6 s / 5 MS/s capture is 60 MB and must never reach the 1 GiB artifact cap.
2. Welch PSD (8 × 64k FFTs) gives each channel an SNR against a **local** floor.
3. Channels below `RF_WIDEBAND_MIN_SNR` (default 8 dB) are skipped — this is what
   keeps a 7-channel weather band from costing 7 decodes every pass.
4. Surviving channels go through DDC → multimon-ng → rtl_433, and any decode
   creates or refreshes an episode with the audio saved as a small WAV.

Cost on LinuxGR: ~9.5 s wall for a 6 s weather pass including 4 channel decodes.

## Two bugs this lane found, both real

**1. `ddc_audio` shifted the wrong way (fixed).** It mixed by `center - target`
instead of `target - center`, so it demodulated the *mirror* frequency. Proven on a
real capture: WX7 at 162.550 with the LO at 162.475 appears at **+74.8 kHz**, and
only a shift in one direction brings it to DC. Every wideband channel on one side
of the tune was being demodulated from the wrong place.

The existing narrowband test could never catch this because the synthetic fixture
placed the signal on the same wrong side the code assumed — a test that agreed with
the bug. The fixture now puts a signal tuned above the LO *below* it in baseband,
matching hardware.

**2. The SNR floor was band-wide (fixed).** Using the median of the whole PSD as
the noise floor manufactured carriers: at 144.390 MHz the local floor sits ~15 dB
below the band median, so plain noise scored 13 dB. The floor is now measured in a
±50 kHz neighbourhood with the channel excluded, and a noise-only regression test
locks it (noise now scores 3.6 dB, not a carrier).

## Measured 2026-10-28

| Band | Result |
|---|---|
| `wx-162` | 5/7 channels above the gate; WX7 162.550 at **24.6 dB** (local transmitter), WX2 18.1, WX3 13.7 |
| `vhf-144` | 144.390 reports ~13 dB, **no decode** |
| `pager-929` | nothing above the gate (3.3–4.1 dB) |

The 144.390 reading is a **radio artifact, not APRS traffic**. Three things prove
it: the level moves 14.2 → 6.3 dB when the LO moves 1 MHz (a real signal would not),
the peak wanders around 144.385–144.395 rather than sitting on 144.390, and the
free RTL on the same antenna sees nothing there (−10.4 dB against a −10.7 dB floor).
So APRS is quiet right now, and the lane correctly creates no episode for it.

## Honest limits

- A carrier with no decode creates **no episode** — the DB only gains rows when a
  decoder with its own error detection produces a frame. That keeps spurs out of
  the dashboard, at the cost of not recording "I heard something here".
- The carrier report can still show LO-dependent radio spurs. Treat a carrier as a
  hint to investigate, not as evidence of a transmission.
- `pager-929` currently listens to 5 channels at 75 kHz spacing, not the full 3 MHz
  of 25 kHz channels — a full paging sweep needs a channel-stepping pass.

## Running it

```
python3 rf_wideband_lane.py --list-bands
python3 rf_wideband_lane.py --once --band wx-162
python3 rf_wideband_lane.py --loop
```

Env (`~/.config/rf-wideband-lane.env`, mode 600): `RF_WIDEBAND_DEVICE_SERIAL`
([REDACTED]), `RF_WIDEBAND_BANDS`, `RF_WIDEBAND_DWELL_SECONDS`,
`RF_WIDEBAND_MIN_SNR`, `RF_WIDEBAND_INTERVAL`.
