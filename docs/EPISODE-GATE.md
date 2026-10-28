# Episode gate — why the Signals list was a noise log, and what changed

Measured 2026-10-28 on LinuxGR (192.168.202.112), `signals.db`, window = previous 24 h.

| Measurement | Value |
|---|---|
| Sweep episodes created in 24 h | **1,872** (78/hour) |
| Design target | 15–25/day |
| Seen once or twice, never again | **874** |
| Peak SNR 6–10 dB | 1,172 |
| Labeled "Radar / FMCW sweep candidate" | **593** |
| ADS-B aircraft episodes | 1,203 (39% of every row) |

Two separate mechanisms produced this, and neither was a threshold that was merely set
too low.

## Multiplier 1 — a single sweep was allowed to become an episode

A 60 s sweep of 5,950 bins always produces peaks. At 6 dB over a local floor, half of
them were seen exactly once and never again. Persistence was never required.

**Fix** (`rf_signal_scout.confirm_blobs`): a blob must be detected in `CONFIRM_SWEEPS`
consecutive sweeps (default 2) before it becomes an episode. Detections that are not yet
confirmed live in `scout-pending.json` and are matched by frequency within `MATCH_HZ`.
A blob at or above `CONFIRM_SNR_BYPASS` (default 25 dB) skips the window — at that strength
one sweep *is* evidence. Pending entries older than `RF_SCOUT_PENDING_MAX_AGE` (600 s) are
dropped, so a sighting hours apart cannot confirm itself.

Measured effect: 15–40% of detections per sweep are held back as pending
(`confirm: 17 detected -> 10 confirmed, 7 pending`).

## Multiplier 2 — the stale close minted a duplicate every 15 minutes

`close_stale()` closes an episode after 15 min without a sighting. When a real emitter
came back, `upsert_blob` found no **open** match and inserted a **new row**. That, not
transient blips, is where most of the 1,872/day came from — the journal showed
`new=0 updated=8` per cycle, i.e. the same handful of bands recycling forever.

**Fix** (`signals_store.match_recent_episode`): if no open episode matches, reopen any
episode seen within `RF_SIGNALS_REOPEN_HOURS` (default 6) that overlaps spectrally
(≥25% of the smaller span). One row per emitter per day instead of one per 15-minute
window. Rows older than the window still get a fresh episode, so a genuinely new
appearance is not lost.

Measured effect, 5 minutes after deploy: **0 episodes created, 4 episodes reopened.**
Those four are rows that would have been duplicates.

## The radar label was a bug, not a feature

`classify_signal` gave the "Radar / FMCW sweep candidate" reference `+10` for *any*
frequency that merely had features, and the radar bandwidth range added another `+25`.
Any 500 kHz–80 MHz blob anywhere scored 35+ with **zero chirp evidence** — that is how 593
rows/day were labeled radar.

**Fix**: the branch now requires the `chirp_like` trait, which only comes from measured
instantaneous-frequency spread (`inst_freq_std > max(50 kHz, 20% of occupied bandwidth)`).
Wide-but-static blobs fall through to an honest label instead. 0 radar labels since deploy.

## ADS-B left the Signals page

1,203 aircraft episodes/day were 39% of every row and buried the actual signals. The
Signals page, its CSV export, and its headline counts now exclude `source='adsb-lane'`;
aircraft stay on `/adsb`. The strip says so, so the numbers are not silently inconsistent.

Effect: **187 signals today** in the list where the same page previously showed 3,075 rows.

## Wideband channel metric — measured, not guessed

`channel_snr` compared the **best single bin** to the neighbourhood median. The number of
bins in a channel hands out free SNR: 329 noise bins at 2.4 MS/s read 15.9 dB on pure
noise. But the obvious fix — the mean over the whole ±6 kHz window — breaks the opposite
way: a narrowband carrier's empty skirts dilute it.

One 162.5 MHz capture, 2 MS/s, 4 s, both the real station and the empty channels in the
same PSD:

| channel | peak | mean (whole window) | mean top 1 kHz |
|---|---|---|---|
| 162.400 (noise) | 4.9 | 0.5 | **3.1** |
| 162.500 (noise) | 5.2 | −0.3 | **2.6** |
| 162.550 (WX7) | 27.0 | 7.6 | **16.2** |

The full-window mean puts WX7 (7.6 dB) *under* the 8 dB gate. The top-1 kHz mean gives
13 dB of separation and averaging 33 bins stops one hot bin reading as a carrier.

**Fix**: the gate uses the mean power of the top `RF_WIDEBAND_TOP_BW_HZ` (default 1,000 Hz)
of the channel window. `peak_snr_db` is still reported and still stored as `peak_snr_db`,
so episode history stays comparable across the change — only the *decision* changed.

Live check after deploy: WX7 `gate=16.1 peak=27.3 carrier=True`; every empty channel
`gate` 1.5–5.4 dB `carrier=False`; noise fixture 2.9 dB.

## Verification

```
python3 tests/test_scout_gate.py     # 6 checks - persistence, burst bypass, expiry
python3 tests/test_store_reopen.py   # 5 checks - one row per emitter, window honoured
python3 tests/test_wideband.py       # 5 checks - packet still carriers, noise does not
python3 tests/test_hopper.py         # 4 checks
python3 tests/test_decode.py         # 6 checks
```

## Spans only ever grew — 54.1 MHz from 48 bins

Found 2026-10-28 from the operator's question about a "48.5 h" duration sitting under a
"TODAY" heading. Two separate problems, only one of them the duration.

**Duration was lifetime, not today.** The section filters `last_seen >= local midnight`,
so a station heard all week shows its full lifetime. Renamed to
`STRONGEST SIGNALS (ACTIVE TODAY, BY SNR)` and the strip states that duration counts from
first heard. The number is wall-clock from first to last sighting, so a signal seen at
09:00 and again at 17:00 reports 8 h even if it was only on air for two minutes.

**The bandwidth was fabricated by the refresh.** `upsert_blob` updated with
`low_hz=MIN(low_hz,?), high_hz=MAX(high_hz,?)` — a span that can only grow. Episode 1
reached 18.0–72.1 MHz (54.1 MHz) while `n_bins` was 48, and 48 contiguous bins at the
sweep's ~450 kHz step cannot exceed ~22 MHz. Seven overlapping rows covered 18–113 MHz.

Two compounding faults:

1. The span never shrank, so a row became a union of everything ever detected nearby.
2. Matching used overlap against the *smaller* span, so a 1 MHz signal inside a 54 MHz row
   scored 100% overlap and was absorbed. 14,431 sightings across 2,820 sweeps is ~5 blobs
   per sweep eating the same row. The 6 h reopen window made it worse by keeping the row
   alive longer.

**Fixes**: the span follows the current blob (shrinkable, `n_bins` too), and matching
requires comparable widths (`smaller/larger >= 0.4`), so a narrow signal inside a wide
band is its own emitter. Legacy rows violating `bandwidth_hz <= n_bins * 1e6 + 1e6` are
hidden by `SPAN_INVARIANT` rather than deleted — the display stops asserting something
physically impossible while the history stays.

**Verified**: 13 rows refreshed after the fix all have spans consistent with their bin
counts (1.45 MHz / 2 bins, 8.73 MHz / 18 bins, 2.36 MHz / 4 bins). The strongest list now
reads 2437 MHz Wi-Fi at 16.0 MHz, 909 MHz at 1.91 MHz, 51 MHz at 8.7 MHz; the 40 MHz /
54.1 MHz artifacts are gone from the page. 8 legacy rows are hidden.

## Still open

- **The daily rate is not yet confirmed.** 0 creations in the first 5 minutes is not a
  day. Re-read `select count(*) from episodes where created_at > <deploy> and
  source='hackrf_sweep'` after 24 h before claiming the 15–25/day target is met — the
  honest expectation is a few dozen, not 25, because this RF environment genuinely has
  persistent emitters.
- `rf_hopper.channel_power_snr` still averages the whole ±6 kHz window. It does not gate
  on SNR (no decode → no episode), so it only under-reports; the same top-1 kHz treatment
  would make its numbers comparable.
