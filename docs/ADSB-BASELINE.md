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

## Gain A/B: max gain is right, the chain is gain-starved

Hypothesis tested: strong local FM was desensing the R820T at max gain, so a lower
gain setting would hear *more*. Tested by running a second `dump1090-mutability`
on the idle RTL2 (device index 1, same antenna feed) at **38.6 dB** for ~6 minutes
while the lane kept running at max gain, then comparing both JSON snapshots at the
same instant:

| Instance | Aircraft | Max range | Messages |
|---|---|---|---|
| lane, gain = max | **39** | 72.4 km | 474,566 (cumulative) |
| test, gain = 38.6 dB | 29 | 72.4 km | 20,588 (~99 msg/s) |

Lower gain tracked **26% fewer aircraft for identical range**. So the front end is
not overloaded — it is short of signal, and turning gain down only loses the weak
ones. The FM-desense hypothesis is **rejected**.

What that implies for spending: the wins available are *ahead* of the receiver, not
in the tuner setting or the port count.

1. **A 1090 MHz LNA + bandpass at the antenna** — sets the system noise figure at
   the mast instead of the RTL's ~5–6 dB, worth roughly 4–5 dB ≈ 1.6–1.8× range
   (38 nm → ~60–65 nm). Keep the RTL gain low-ish once an LNA is in line.
2. **A dedicated 1090 MHz antenna** — the D3000N is broadband and low-gain at
   1090; a collinear is worth ~5–6 dBi, another ~2× range on its own.
3. **Not** more multicoupler ports: that adds loss to a chain that is already
   short of signal.



## Re-measuring

After any change, re-run the same two queries and compare max/p90 range and
aircraft count. A full-day baseline (24 h) is the right unit; the window above is
2.2 h, so expect the daily max to be somewhat higher simply from more time in the
sample.

## 2026-11-02: dedicated FlightAware 1090 antenna on a third RTL (AFTER)

Chain changed to: **FlightAware dual-band 1090/978 omni (6 dBi, N-female, passive,
no LNA) → 32 ft N-male→SMA-male cable → Nooelec NESDR SMArt v5 (USB serial
`RTL0`, hub port 7-1.1.3) → readsb, gain 49.6**. The D3000N/MCA204M feed stays on
RTL3 as a spare. Antenna was *not* in its final position (indoors/marginal spot).

Simultaneous A/B, both receivers running at the same instant, 2026-11-02 EST:

| Same instant | New (1090 ant, RTL0) | Old (D3000N, RTL3) |
|---|---|---|
| 20:34:35 aircraft | **30** (24 positioned) | 8 (6) |
| max / p90 / median range | **96.4 / 75.7 / 40.9 km** | 24.6 / 24.0 / 20.0 km |
| message rate | **166.6 msg/s** | 13.8 msg/s |
| 20:40:35 aircraft | **30** (26) | 8 (7) |
| max / p90 / median range | **126.7 / 61.4 / 36.7 km** | 37.0 / 31.5 / 23.8 km |
| message rate | **161.7 msg/s** | 19.3 msg/s |

Set relationship both samples: the new radio heard **every** aircraft the old chain
heard (8/8) plus 22 more, never fewer. 126.7 km already exceeds the 71–80 km
ceiling of the D3000N chain. Production cut over at 21:01 EST and read 25–26
aircraft / max 112.7 km / 140.9 msg/s a minute later.

Remaining lever, unchanged from the analysis above: the 32 ft of cable at 1090 MHz
costs several dB ahead of a gain-starved RTL, so a mast-mounted 1090 LNA is still
the next real win. The antenna is dual-band 1090/978, so UAT 978 reception is also
available with a second dongle.

## Pin by serial, never by index

Adding the third dongle re-ordered libusb indices (0=RTL3, 1=RTL0, 2=RTL2) and
silently moved `rf-hopper` (`RF_HOPPER_DEVICE=1`) onto the new radio — it spent its
dwells on a 1090 antenna hearing nothing. readsb takes `--device <index|serial>`,
so the lane is pinned `--device RTL0`; `rtl_sdr` only takes an index, so the
hopper's env index must be re-checked after any USB add/remove/re-plug.

