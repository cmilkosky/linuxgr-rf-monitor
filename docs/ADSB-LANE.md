# ADS-B lane (1090 MHz)

First of the RTL decode lanes: a dedicated RTL-SDR parked on 1090 MHz, decoding
the Mode-S / ADS-B downlink 24/7 and writing sightings into the episode table so
aircraft show up in the dashboard's **AIRCRAFT / APRS / PAGER TEXT** section.

## Pieces

| Unit | Runs as | Role |
|---|---|---|
| `rf-adsb-dump1090.service` | `cmilkosk` | `dump1090-mutability --device-index 0 --write-json .../signals/adsb` |
| `rf-adsb-lane.service` | `cmilkosk` | `rf_adsb_lane.py --loop --interval 5` |

Both run as `cmilkosk` on purpose: the RTL device nodes are `root:plugdev` and
`cmilkosk` is in `plugdev`, so the dongles open without touching udev. The
packaged `dump1090-mutability.service` runs as user `dump1090` (group `nogroup`)
and can only fail with `usb_open error -3` - it is disabled, along with the
`lighttpd` instance the package installs for its web map (that map is optional;
re-enable `lighttpd` if you want it back on :80).

## Rows

One row per aircraft **sighting**, keyed on ICAO hex:

- an aircraft still heard inside `REVISIT_MINUTES` (10) updates its row -
  `last_seen`, `sightings`, summary, best `peak_snr_db`;
- after a gap, the same hex starts a new row (a plane back 40 minutes later is a
  different observation);
- `score=0` and `decode_status='decoded'`, so the capture queue
  (`decode_status='none' AND score >= 45`) never tries to capture an aircraft -
  there is no IQ worth keeping for a packet that already decoded;
- `source='adsb-lane'`, `class='known'`, `band='1090 MHz ADS-B'`,
  `known_emitter=<hex>`, raw record in `features_json`;
- pruned after `--retention-days` (7) on a rolling basis, so a busy sky does not
  grow the DB without bound.

Summary format: `GJS4384 (a6eb1e) · 6,625 ft · 277 kt · trk 066° · sqwk 3666 · 40.361,-74.724`

## Verified

- Reception by hand: 20 aircraft / 4,039 messages in 45 s.
- Lane live: rows appearing within a minute, summaries updating in place
  (GJS4384 6,800 -> 6,625 ft between two API reads).
- `/api/signals/sections` -> `text: 17` rows, all `decode_type='aircraft'`.
- Capture queue unaffected by aircraft rows.

## Device note

`--device-index 0` is RTL3 (Nooelec NESDR SMArt v5). The RTLs are healthy - an
earlier "RTL chain broken" reading came from sampling frequencies with no signal
on them (100.5 / 162.55 / 145.0 MHz). On stations the sweep actually sees
(103.3 MHz) the demodulated audio shows 2.4x modulation spread, and ADS-B
decodes at 20+ aircraft per 45 s. If a dongle is missing, the index shifts -
pin by serial in a newer decoder build if that becomes a problem.
