# RF Monitor v3 — design decisions from the 2026-10-27 review

Answers to Chris's six questions, with the measurements they rest on. v2 (signal
episodes + Signals dashboard + 7-day rollup) is built and committed; this file is
the brief for v3.

## Measured baseline (2026-10-27 14:00 EDT, linuxGR)

| Fact | Value |
|---|---|
| Sweep command | `hackrf_sweep -f 1:6000 -w 1000000 -1` (from `HACKRF_SWEEP_ARGS`) |
| Sweep coverage | 1 – 6000 MHz, 6000 bins, **1 MHz resolution** |
| Sweep duty | 0.79 – 0.82 s active per 60 s interval |
| Episodes recorded | 482 (427 captured) |
| Episode rate | 11 – 20 per hour ≈ 380/day (target was 15 – 25/day) |
| Episode bandwidth | mean 2.2 MHz, max 53 MHz, only 8 wider than 20 MHz |
| Storage | ~310 kB per captured episode, 136 MB total |

### Episodes against the antenna's actual range

The Diamond D3000N is specified 25 – 3000 MHz. We scan 1 – 6000 MHz, so:

| Range | Bins | Episodes |
|---|---|---|
| below 25 MHz (outside antenna) | 24 | 18 |
| 25 – 3000 MHz (inside antenna) | 2975 | 332 |
| above 3000 MHz (outside antenna) | 3000 | 132 |

**3024 of 6000 bins — 50.4 % of every sweep — are outside the antenna's range**,
and 150 of 482 episodes (31 %) come from those dead zones.

## Q1 — Are we scanning known bands or everything?

Everything. The sweep is continuous across 1 – 6000 MHz; there is no band
whitelist driving collection. `KNOWN_BANDS` in `rf_signal_scout.py` is used only
to *label* a blob and to raise the score it needs to earn a row
(`MIN_SCORE_KNOWN`). Known emitters still appear — tagged — they just have to be
louder to rank. That matches the row rule: unexplained + new + strongest, not
"only what's on a list".

## Q2 — Can we tell noise from signal, and drop the noise?

Not from the sweep, and that is the important part. At 1 MHz bins, a 12.5 kHz
APRS burst and a 200 kHz FM station both occupy 1 – 3 bins, so the measured
"bandwidth" is the resolution floor, not the signal. Sweep data can only rank
*where to look*; it cannot classify.

Classification has to happen on the capture, where the radio delivers 2 – 5 MS/s.
There the discriminators are real:

- occupied bandwidth as a fraction of capture bandwidth (narrowband vs hiss)
- spectral flatness / kurtosis across the band (noise is flat, signals are peaked)
- time-domain envelope: bursty vs continuous, duty cycle over 30 s
- frequency stability (a carrier stays put; noise wanders)
- **decode success — the only proof.** A valid APRS/ADS-B/POCSAG frame cannot be
  produced by noise.

Proposed gate (all must pass before an episode is kept or a decoder is tried):
narrowband, SNR above the measured noise floor, stable in frequency, and
reproducible on a second capture. Everything else is dropped or collapsed into a
per-band noise floor rather than being listed as a signal.

## Q3 — Is scanning above 3000 MHz useless?

Effectively yes, with the D3000N. The HackRF tunes to 6 GHz, but the antenna stops
at 3 GHz, so above 3 GHz you are measuring the receiver's own noise and whatever
the antenna happens to pick up off-resonance. It is not worth the sweep time.

Same at the bottom: the antenna starts at 25 MHz, so the 18 episodes below 25 MHz
are not real signals either.

Recommendation: change `HACKRF_SWEEP_ARGS` to `-f 25:3000 -w 500000`. Halving the
span to 2975 bins lets the bin width drop to 500 kHz in the same 0.8 s, which
doubles frequency resolution — directly improving the one thing the sweep is
actually good for: deciding where to point the capture radio.

## Q4 — Second SDR: one scans, one samples?

Yes, and it is the right architecture. The HackRF One is half-duplex: it cannot
sweep and stream at the same time. Today the two jobs time-share the device with
`flock`, which is why a capture only ever gets the 59 s idle window and is capped
at a few seconds.

A second HackRF One is the clean answer:

- identical tuning (1 MHz – 6 GHz), identical tooling, no second driver stack
- both fed from the existing 1-in/4-out splitter, so the same antenna
- the sweep never pauses, and capture gets a radio it can hold for as long as it
  needs

Splitter loss is ~3.5 – 7 dB per port; acceptable, and it affects both radios
equally so comparisons stay valid.

Cheaper partial option: the two RTL-SDRs already on the box (24 – 1766 MHz,
8-bit, 2.4 MS/s) can carry all narrowband decoding below 1.766 GHz — APRS,
ADS-B, POCSAG, rtl_433, ACARS. A second HackRF is then only needed for
1.7 – 3 GHz work. If the budget is one radio, the RTLs cover most decoders.

## Q5 — Audio only when it is audio; decode data into its own format

Correct, and v2 is wrong here today: every capture writes three MP3s
(AM / NFM / WFM) whether or not the content is audio. That is wasted space and
useless for data.

The rule for v3: classify first, then store the artifact the content actually has.

| Content | Artifact stored | Tool |
|---|---|---|
| voice | audio + transcript | demod → `POST http://192.168.202.59:10301/transcribe` |
| music | audio + station ID / RDS | `redsea` |
| Morse / CW | decoded text | `multimon-ng` |
| APRS | packet text + position | `direwolf` |
| ADS-B | aircraft table (callsign, alt, speed, heading) | `readsb` / `dump1090` |
| pager (POCSAG/FLEX) | message text + capcode | `multimon-ng` |
| ACARS | message text | `acarsdec` |
| ISM / TPMS sensors | JSON readings | `rtl_433` |
| satellite (APT/LRPT/HRIT) | image, even partial | `satdump` |
| unknown data | symbol rate, modulation guess, constellation, raw hex | custom |

So clicking an APRS row prints the decoded packet; clicking ADS-B shows what was
received; clicking a satellite pass shows the image. Audio players only appear
for episodes whose content is genuinely audio.

## Q6 — The capture radio can dwell as long as it needs

Yes, and this is what the second radio unlocks. With a dedicated capture radio
there is no 4 s ceiling; the scheduler can hold a candidate for as long as a
decoder needs:

- **default dwell 30 s** — enough for APRS beacons, pager bursts, ACARS blocks,
  ISM sensor repeats
- **extend while progress is being made** — each successful frame resets the
  budget, so a busy APRS channel can hold the radio for minutes
- **satellite passes are scheduled, not discovered** — NOAA/Meteor passes are
  predicted from TLEs, and the radio is booked for the ~10 – 15 min window
- **hard cap and queue** — the radio is single-channel, so long dwells block
  other candidates. That is exactly why the Q2 gate matters: never spend dwell
  time on something that failed the noise gate.

The queue needs a priority: a scheduled satellite pass outranks opportunistic
capture, and a candidate that has already failed one decode attempt drops to the
back.

## Open decisions for Chris

1. Approve changing the sweep to `-f 25:3000 -w 500000` (halves the span, doubles
   resolution, drops the 31 % of episodes from outside the antenna).
2. Second HackRF One, or start with the two RTL-SDRs for narrowband decode?
3. Confirm the noise gate thresholds once measured on real captures.
4. Keep the 15 – 25 rows/day target as the acceptance test for the gate.
