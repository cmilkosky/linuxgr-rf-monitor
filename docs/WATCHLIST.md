# Watchlist: from "found a signal" to "listen to it regularly"

The tier that turns discovery into coverage. Without it the third tier never
fills up and every frequency worth watching is a manual edit somewhere.

## The loop

```
sweep finds a signal  ->  capture lane decodes it  ->  operator looks at the row
      ->  "yes, watch this"  ->  promote  ->  a radio is assigned to it
```

`/watchlist` in the console, plus an **Add to watchlist** button on every signal's
detail page. `rf_watchlist.py` holds the intent; the page shows which radio can
serve it and says plainly when none can.

## Radio model on LinuxGR

| Radio | Kind | Duty | Can be parked? |
|---|---|---|---|
| `hackrf-sweep` | HackRF | scan | no — always scanning |
| `hackrf-portapack` | HackRF | capture | no — capture / wideband work |
| `rtl-3` | RTL | parked | yes — ADS-B 1090 |
| `rtl-2` | RTL | parked | yes — free |

A `parked` entry needs a radio with `duty='parked'`. When none is free the entry
is **still recorded**, with `radio` NULL and a note saying why: intent kept, lane
not pretended. That matters because the honest state of the system is more useful
than a row that claims a lane exists.

## Decoder is guessed, and overridable

`guess_decoder()` maps the frequency to the ladder: 1080–1100 MHz → `adsb`,
144–148 → `aprs`, 162–174 → `weather`, 929–932 → `pager`, 315 / 433.92 / 915 →
`ism`, anything else → `auto` (classify on capture).

## Verified 2026-10-28

| Action | Result |
|---|---|
| Seed | ADS-B 1090 MHz → `adsb`, radio `rtl-3`, enabled |
| Promote episode 1428 (930 MHz) | `pager`, radio **rtl-2** assigned automatically |
| Manual add 144.390 APRS | recorded with **no radio** — rtl-2 was just taken |
| `/watchlist` page | renders entries, radios, free/in-use state |

The third line is the important one: adding a fourth parked frequency when only
two parked radios exist does not fail and does not lie — it records the intent
and shows the constraint.

## What this does *not* do yet

The watchlist stores intent and assignment; it does not yet **start** a lane.
Execution arrives with the APRS parked lane (the decoder leg is already built and
tested). Until then, an entry with a radio assigned is a promise the operator can
see, not a running process.
