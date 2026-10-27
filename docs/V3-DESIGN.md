# RF Monitor v3 — design decisions from the 2026-10-27 review

Answers to Chris's six questions, with the measurements they rest on. v2 (signal
episodes + Signals dashboard + 7-day rollup) is built and committed; this file is
the brief for v3.

## Measured baseline (2026-10-27 14:00 EDT, linuxGR)

| Fact | Value |
|---|---|
| Sweep command | `hackrf_sweep -f 25:3000 -w 500000 -1` (from `HACKRF_SWEEP_ARGS`) |
| Sweep coverage | 25 – 3000 MHz, 6556 bins, **~455 kHz median step** |
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

## Resolution: how small should the steps be?

Measured on linuxGR with `-f 25:3000`, timed under the device flock:

| Bin width | Bins | Sweep time | Points/day | vs today |
|---|---|---|---|---|
| 1 MHz | 2,975 | 494 ms | 4.28 M | 0.5x |
| **500 kHz (applied)** | **5,950** | **456 ms** | **8.57 M** | **1.0x** |
| 250 kHz | 11,900 | 461 ms | 17.1 M | 2x |
| 100 kHz | 29,750 | 457 ms | 42.8 M | 5x |
| 20 kHz | 148,750 | 554 ms | 214 M | 25x |

**Sweep time is flat.** Going from 1 MHz to 20 kHz bins costs 60 ms, because the
HackRF's sweep time is set by how many 20 MHz hops it makes, not by the FFT
resolution inside each hop. Resolution is therefore free in *time* and expensive
only in *data*.

What finer bins actually buy — and one thing they do not:

- FFT bins are contiguous, so there are no "gaps" between 100.0 and 101.0 MHz to
  go looking for. A 12.5 kHz APRS burst already lands in a bin at 1 MHz
  resolution; it is not missed because it fell between steps.
- Finer bins lower the noise per bin, so narrowband signals gain SNR — halving
  the bin width is worth about **3 dB**. Measured median floor over the new
  25–3000 span is **-63.1 dB** vs **-60.0 dB** over the old 1–6000 span; the band
  changed too, so treat that as consistent with the 3 dB, not as a clean A/B.
- Finer bins separate adjacent channels and let the sweep estimate bandwidth
  rather than merely flag a peak.

Why not go to 100 kHz: 42.8 M points/day is 5x today, which at 30-day retention
is 1.28 billion points and would push the 168 h query from 88 s to roughly 440 s.
That is not viable without shortening raw retention to ~7 days. 500 kHz costs
exactly what today costs and doubles resolution, so that is where it stays.

Detail below ~100 kHz is the capture radio's job: it sees 2.4–5 MHz
instantaneously at sub-kHz resolution, which no sweep can match.

## Scanning method: is more scanning the answer?

The sweep observes each 20 MHz segment for about **3.4 ms once per 60 s**
(0.5 s of work spread over ~149 hops). A signal has to be on air during that
window to be seen. That is fine for continuous carriers and poor for bursts:
APRS beacons last ~0.5 s, pager bursts 1–2 s, ADS-B frames 120 us.

So more sweeping helps only linearly and is the wrong lever. The right
architecture is:

- **sweep = discovery** — finds persistent, strong, unexplained carriers
- **continuous lanes = burst capture** — a receiver parked on a known channel,
  decoding 24/7, never missing a burst

### Should the RTL-SDRs share the scan load?

No. An RTL-SDR is 2.4 MS/s, so it sees 2.4 MHz at a time; covering 25–1766 MHz
takes ~725 retunes at 15–20 ms each, i.e. 11–15 s per sweep — slower and coarser
than the HackRF's 0.5 s. They are bad scanners and good **channel monitors**.

Dedicate each dongle to a lane instead:

| Dongle | Slice | Decoder |
|---|---|---|
| RTL2 | 143.2 – 145.6 MHz | `direwolf` (APRS 144.39) |
| RTL3 | 1088 – 1091 MHz | `readsb` / `dump1090` (ADS-B 1090) |
| spare | 162.4 – 162.6 MHz | NOAA weather, or a trunked control channel |

### Trunked audio (EMT / fire / police)

Still entirely doable, and it fits the same model. `trunk-recorder` (or `op25`)
follows a P25 / Motorola trunked system: one dongle holds the control channel,
one dongle per simultaneous voice call. Each decoded call becomes an episode —
talkgroup, agency, duration, and a transcript — so a fire call shows up in the
Signals feed next to the RF discoveries.

Two honest caveats: it needs the control-channel frequency and system type, which
are found by scanning for a continuous carrier in 769–775 / 851–869 MHz; and many
police systems are P25 AES-encrypted, where you will see the call grant but not
the audio. Fire and EMS are frequently in the clear.

## PortaPack H2+ as the second HackRF — measured 2026-10-27

Plugged into linuxGR and inspected. It needs **no firmware update**: the Mayhem
firmware (v2.1.0, API 1.08) already exposes HackRF mode, and both devices answer
`hackrf_info`:

| Role | Serial | Firmware | Board |
|---|---|---|---|
| main HackRF One | `0000000000000000f75461dc2696a6c3` | 2023.01.1 (API 1.07) | 4 (r9) |
| PortaPack H2+ | `000000000000000016bc62dc2e4c22a7` | v2.1.0 (API 1.08) | 4 (r9) |

What works:

- enumerates as `1d50:6089 HackRF One SDR`
- streams **20 MS/s** (40 MB in 1 s, correct for int8 I/Q)
- sweeps 25–3000 MHz in **447 ms**, identical to the main radio's 446 ms
- tunes accurately — on 103.300 MHz the carrier landed **6.85 kHz** off centre

What does not work: **its spectrum disagrees with the main radio**, by far more
than a shared passive splitter can explain.

| Frequency | main | PortaPack | delta |
|---|---|---|---|
| 100 MHz | -69.8 | -59.1 | +10.7 |
| 101 MHz | -69.7 | **-24.4** | **+45.3** |
| 103 MHz | -68.8 | -40.7 | +28.1 |
| 104 MHz | -76.3 | -54.9 | +21.4 |
| 109 MHz | -59.5 | **-93.4** | **-33.9** |

A 4-way passive splitter gives every port the same signals at roughly equal
level. It cannot produce +45 dB at one frequency and -34 dB at another. So one of
these is true, and it has to be settled physically before the radio is used:

1. the PortaPack is not actually on the shared antenna (SMA not seated, or on a
   different feed) — most likely, given it was swapped in for a removed RTL
2. the PortaPack front end is unfiltered and generating intermodulation products
   from strong out-of-band signals
3. the splitter port it landed on is damaged — note an RTL on USB path
   `7-1.1.4` was flapping (repeated connect/disconnect) just before the swap

**Verdict:** usable as a capture radio (tuning and streaming are verified), not
yet usable as a scanner or as a correlated second radio. Re-run the comparison
after confirming the physical feed.

The collector is now pinned with `-d 0000000000000000f75461dc2696a6c3` so device
enumeration order can never move the sweep onto the other radio.

### Antenna budget

The 1-in/4-out splitter is now **full**: main HackRF, PortaPack, RTL2, RTL3. Any
further radio needs a larger splitter, an active multicoupler, or a switched
arrangement.

## Low-noise amplifier: is it worth it?

Yes, but not as a blanket wideband amplifier, and only with protection. The
numbers that decide it:

- **HackRF One maximum input power is -5 dBm** and exceeding it causes permanent
  damage (official HackRF docs). It can survive +10 dBm only with the front-end
  RX amp disabled, and a software slip can re-enable it.
- A 25 dB LNA in front of the HackRF therefore means the antenna signal must stay
  below **-30 dBm**. A nearby FM station at a discone can be -20 to -10 dBm.
- The HackRF is 8-bit with no front-end filter, so it overloads long before a
  12-16 bit SDR would.

Where an LNA genuinely pays: the 4-way passive splitter costs ~6-7 dB per port.
An LNA mounted **at the antenna, before the splitter**, recovers that loss for
every radio at once and sets the system noise figure at the mast instead of the
shack. That is the strongest argument in favour, and it is an argument about
splitter loss, not about sensitivity per se.

Options, from cheapest to best-matched:

| Option | Coverage | Gain | Price | Notes |
|---|---|---|---|---|
| RTL-SDR Blog Wideband LNA | 20 MHz – 2 GHz | ~20 dB flat | ~$30 | community default, bias-tee powered, stops at 2 GHz |
| Nooelec VeGA | wideband | adjustable | ~$50 | adjustable gain is safer for a scanner |
| Mini-Circuits ZX60-P103LN+ | 50 – 3000 MHz | 25.2 dB @50 MHz → 6.9 dB @3 GHz | ~$150-200 | NF 0.4–1.0 dB, survives +21/+26 dBm input, SMA, 5 V/95 mA |

Required alongside any of them: an **FM broadcast band-stop filter** (the
RTL-SDR Blog unit is the one the community rates above Nooelec) to stop 88–108
MHz overload, and a **switched attenuator or limiter** between LNA and radio.
Without those two, an LNA makes the HackRF both deafer and more fragile.

A passive splitter plus a mast LNA is still lossy. The cleaner answer for four
radios is an **active multicoupler** (Stridsberg and similar), which amplifies
and splits in one box with low port-to-port isolation loss — that replaces the
passive splitter rather than adding to it.

## Open decisions for Chris

1. ~~Sweep to `-f 25:3000 -w 500000`~~ — **applied 2026-10-27 16:28 UTC**, verified
   span 25.227–3004.773 MHz at 455 kHz median step.
2. Second radio: PortaPack H2+ in HackRF mode (Chris to enable), or start with the
   two RTL-SDRs as continuous decode lanes.
3. Which RTL lanes to run first — APRS, ADS-B, or a trunked control channel?
4. Confirm the noise gate thresholds once measured on real captures.
5. Keep the 15 – 25 rows/day target as the acceptance test for the gate.
