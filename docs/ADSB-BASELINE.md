# ADS-B baseline (before any splitter / port / gain change)

Recorded 2026-10-28, so a future change to the antenna split, the multicoupler,
or the tuner gain has a before/after number instead of an impression.

## Chain at time of measurement

Diamond D3000N (25–3000 MHz) → **Stridsberg MCA204M** (1-in/4-out, active,
near-unity gain, P1dB +17 dBm) → Nooelec NESDR SMArt v5 (R820T), dedicated port,
no LNA, `dump1090-mutability` at **default gain = max** (no `--gain` passed).

## Numbers

| Metric | Value |
|---|---|
| Window | 2026-10-28 11:20:22Z → 13:33:10Z (2.21 h) |
| Episode rows / distinct aircraft | 426 / **418** |
| Snapshot aircraft tracked | 45 (19 with position) |
| Cumulative messages at snapshot | 433,812 |
| Max range | **71.2 km (38.5 nm)** |
| p90 range | 69.7 km (37.6 nm) |
| Median range | 24.1 km (13.0 nm) |
| Aircraft beyond 50 km | 2 |
| Aircraft beyond 75 km | **0** |
| Highest altitude seen | 38,000 ft |

Method: great-circle distance from the site coordinates (read from Home
Assistant, not printed here) to each aircraft's last known position, over all 430
`adsb-lane` rows in `signals.db`. Two independent samples — a live
`aircraft.json` snapshot and the full row set — agreed to within 0.4 km.

## What it means

The physical radio horizon for an aircraft at 38,000 ft with a 10 m receiver is
**456 km (246 nm)**. Observed max is 71 km, i.e. **~16% of the horizon**, and no
aircraft was heard beyond 75 km across 418 tracks. This is a *range* limit, not a
decode limit — the decoder is fine, the front end is not hearing what it could.

Most likely cause, in order:

1. **FM desense.** Local FM is strong: 94.0 MHz measured at 42.6 dB SNR in the
   sweep, 98.0 at 38.2, 102.0 at 37.5. The R820T has no preselection, and at max
   gain a −30 dBm FM band can raise the noise floor at 1090 MHz. Cheap fixes: a
   tuner gain setting that is *lower* than max, and/or an FM bandstop filter
   (~$20–30) ahead of the RTL.
2. **Multicoupler above 1 GHz.** The MCA204M's published specs relax above
   ~1 GHz; a 1090 MHz bandpass or an LNA at the antenna would both help.
3. **Antenna/cable.** D3000N gain at 1090 MHz is modest, and cable loss at 1 GHz
   is ~6–7 dB per 100 ft of LMR-400.

Consequence for hardware decisions: adding 3–4 dB of split loss or noise figure
(an 8-port multicoupler) would take an already short range down by another
~25–30%. Fix range first, then consider ports.

## Re-measuring

After any change, re-run the same two queries and compare max/p90 range and
aircraft count. A full-day baseline (24 h) is the right unit; the window above is
2.2 h, so expect the daily max to be somewhat higher simply from more time in the
sample.
