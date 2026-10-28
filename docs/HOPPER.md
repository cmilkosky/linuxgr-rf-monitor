# Hopper lane: one parked radio, every watchlist frequency

The watchlist records intent. The hopper is what serves it.

RTL2 hears ~2.4 MHz at a time, and the watchlist spans 144.390 MHz to 930.0 MHz —
786 MHz apart. No single tuning serves that list, so the radio **hops**: dwell on one
frequency, decode, retune, next.

```
rtl_sdr (unsigned cu8, 2.4 MS/s, tuned +100 kHz)
    -> channel power vs local floor      (quiet channel: skip the decode)
    -> DDC the watched frequency to baseband -> FM discriminator
    -> multimon-ng (AFSK1200, POCSAG, FLEX, ...)
    -> episode, but ONLY when something really decodes
```

## It is not a carrier detector

A carrier with no decode produces **no episode**. The sweep already logs carriers, and a
pager carrier that cannot be demodulated is not evidence of a message. Only validated
decodes (AX.25 FCS, POCSAG BCH FEC) become episodes — the same rule the decode stage
uses.

ADS-B entries are skipped on purpose: readsb owns that radio and tar1090 needs the whole
2 MHz around 1090 MHz continuously.

## Radio assignment and adoption

Entries recorded while no parked radio was free carry no radio and a note saying so. The
hopper **adopts** them: when it can actually serve a frequency, it writes `radio=rtl-2`
on the entry. That is the difference between intent and a lane — the watchlist no longer
claims a service it cannot deliver.

Current state: 1090 ADS-B → `rtl-3`, 930.0 pager → `rtl-2`, 144.390 APRS → `rtl-2` (adopted).

## Two measurement traps, both measured

**1. RTL cu8 is unsigned; HackRF cu8 is signed.** `rf_v2_capture.read_iq` assumes signed
bytes, correct for `hackrf_transfer` and wrong for an RTL. Reading an RTL capture with it
folds the spectrum and manufactures a full-scale DC term, so silence looks like a
carrier. `rf_hopper.read_rtl_iq` subtracts 127.5 and is pinned by a test (0 → −1.0,
255 → +1.0, mid-scale → silence).

**2. `rf_wideband_lane.channel_snr` must not be reused for a single dwell.** It compares
the single **max** bin in the channel against the neighbourhood median. That is fine for
gating a wideband pass, where a real carrier is 20–60 dB up — but at 2.4 MS/s the channel
spans ~330 noise bins and the maximum of 330 exponentially distributed bins sits ~10 dB
above the median for free. Measured 2026-10-28 it reported **15.9 dB "SNR" at 144.390 MHz
on pure noise**, while `rtl_power` on the same device read +0.2 dB and a mean-based read
of the same capture gave −0.2 dB. `rf_hopper.channel_power_snr` averages the channel bins
instead; the same dwells now read 0.7 / 1.6 dB, matching `rtl_power`.

A carrier is still not evidence: a HackRF "carrier" that moves when the LO moves is an
internal spur, and an RTL "carrier" that survives a mean-based measurement is worth
verifying with `rtl_power` before believing it.

## Config

`/home/cmilkosk/.config/rf-hopper.env` (mode 600):

| Knob | Default | Meaning |
|---|---|---|
| `RF_HOPPER_RADIO` | `rtl-2` | watchlist radio this lane serves |
| `RF_HOPPER_DEVICE` | `1` | RTL device index (0 = RTL3/ADS-B, 1 = RTL2) |
| `RF_HOPPER_DWELL_SECONDS` | `15` | time parked on each frequency |
| `RF_HOPPER_INTERVAL` | `5` | pause between cycles |
| `RF_HOPPER_SAMPLE_RATE` | `2400000` | capture rate |

One 15 s dwell per frequency; a full two-frequency cycle costs ~35 s.

## Ops

```
systemctl status rf-hopper
journalctl -u rf-hopper -n 20 -o cat
sudo -u cmilkosk python3 /home/cmilkosk/rf-monitor/rf_hopper.py --list
sudo -u cmilkosk python3 /home/cmilkosk/rf-monitor/rf_hopper.py --once
python3 tests/test_hopper.py        # 4 checks, FAILURES: 0
```

## Honest status 2026-10-28

The lane is proven end-to-end on hardware: captures, measures, DDCs, decodes, records
episodes, adopts unassigned entries, 0 restarts. **No live decode has been captured
yet** — both frequencies read ~1 dB over their local floor, which agrees with the
independent `rtl_power` cross-check that found nothing on the air at 144.390 or 930.0.
An APRS or pager burst that is actually transmitted will produce an episode; the lane
being quiet is a statement about the air, not about the lane.
