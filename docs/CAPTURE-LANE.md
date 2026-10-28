# Capture lane (second radio: PortaPack in HackRF mode)

Captures used to run inside the scout on the main HackRF, sharing its flock with
the always-on sweep. That made every capture a 4 s grab wedged into the sweep's
idle window, with the sweep paused while it ran. `rf-capture-lane` moves capture
onto the second radio, which changes what is possible:

- **the sweep keeps sweeping while a capture is in flight** - separate device,
  separate flock (`/tmp/rf-portapack-device.lock`), verified: `hackrf_influx`
  wrote its 5,000-point batch and finished `rc=0` during a 90 s capture, and the
  scout kept scanning on its 60 s cadence throughout;
- **dwell is long enough for content that does not fit in 4 s** - base 30 s,
  ceiling 90 s;
- **extend-on-progress**: after each chunk, a 2 s tail is FFT'd and the capture
  continues only while a peak sits >= 12 dB above the median. A signal that is
  still on the air gets listened to; one that has stopped does not.

## Device pinning

With two HackRFs attached, an unpinned `hackrf_transfer` takes whichever
enumerated first - which is how a capture lane and the sweep end up fighting over
one radio. `capture_hackrf(..., device_serial=...)` pins it; the lane reads the
serial from `/home/cmilkosk/.config/rf-capture-lane.env` (mode 600,
`cmilkosk`-owned). Sweep holds `...a6c3`, the PortaPack holds `...22a7`.

## Spur masking

The PortaPack leaks its own spurs around **89.0** and **101.5 MHz**, and both are
real FM broadcast channels here, so a capture on them would mostly be the radio
hearing itself. Episodes within `RF_SPUR_HALF_WIDTH_HZ` (150 kHz) are marked
`class='spur-masked'`, `decode_status='skipped'` and never captured on this
radio. Verified: 89.0 and 101.5 MHz raise `SpurMaskedError` before any radio is
touched; 89.4 MHz passes the mask.

## Which episodes it takes

`pick_queue()` prefers bands where the decode ladder can produce a frame, then
falls back to score:

```
144-148 MHz APRS/packet · 162-174 weather/EAS/paging · 315/433.92/915 MHz ISM ·
929-932 MHz paging
```

The queue is ordered by score and score tracks signal strength, so the loudest
entries are broadcast and radar - the two things that never decode. With one long
capture per pass, spend it where a frame is possible. Verified: after this
change the lane took a 930 MHz paging episode over a louder 232 MHz one.

## Decode

The lane decodes from full-rate IQ through the same `rf_decode.decode_iq_hits()`
path the scout uses, bounded to the first and last 20 s of the dwell: a 90 s
dwell is 180M complex samples and running the down-converter over all of it costs
more than the capture did.

## The limitation this exposed

A capture lane can only capture what the **sweep** flagged, and the sweep is a
poor scout for intermittent narrowband signals: an APRS beacon or a pager burst
is on the air for a fraction of a second and has to be lucky to land in a 60 s
snapshot. That is why live decodes are still zero. The fix is architectural and
already proven by the ADS-B lane: a **continuous decode lane** parked on the
frequency of interest (APRS 144.390 on the free RTL2), not a capture lane waiting
for the sweep to notice.

## Cost

A 90 s dwell writes ~2.3-2.4 MB of artifacts (spectrogram + three MP3s), against
~0.3 MB for a 4 s capture. At ~25 captures/hour that is ~60 MB/hour, so the 1 GiB
artifact cap now turns over in well under a day. Lower `RF_LANE_MAX_DWELL_SECONDS`
or `RF_LANE_MAX_PER_RUN` if the turnover matters more than dwell.

## Configuration

`/home/cmilkosk/.config/rf-capture-lane.env` (600): `RF_CAPTURE_DEVICE_SERIAL`,
`RF_LANE_DWELL_SECONDS` (30), `RF_LANE_MAX_DWELL_SECONDS` (90), `RF_LANE_MIN_SCORE`
(60), `RF_LANE_MAX_PER_RUN` (1), `RF_LANE_INTERVAL` (60), `RF_LANE_SPUR_MASK`.

The scout's capture budget is switched off by a drop-in
(`rf-signal-scout.service.d/10-captures-moved.conf`, `RF_SCOUT_MAX_CAPTURES=0`):
it keeps scanning and scoring, one engine owns the queue.

## Verified

| Check | Result |
|---|---|
| device pinning | sweep `...a6c3`, PortaPack `...22a7`, lane pinned |
| sweep during capture | influx wrote 5,000 points, `rc=0`, while a 90 s capture ran |
| extend-on-progress | ep1185 1490 MHz -> dwell 90.0 s; ep1428 930 MHz -> 90.0 s |
| spur mask | 89.0 / 101.5 MHz refused pre-radio; 89.4 MHz passes |
| priority queue | took 930 MHz paging over louder 232 MHz |
| rows | capture_id set, `decode_status='captured'`, 4 artifacts each (~2.4 MB) |
