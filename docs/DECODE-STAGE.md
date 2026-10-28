# RF Monitor decode stage (v2.1)

The scout used to stop at `decode_status='captured'`: it wrote a spectrogram and
three audio files, then nothing ever wrote `decode_type`, so the dashboard's
VOICE / MUSIC / ISM / AIRCRAFT-APRS-PAGER / SATELLITE sections were empty by
construction. This is the missing half.

## What runs

`rf_decode.py`, three ways in:

| Entry | Use |
|---|---|
| `decode_iq_hits(iq, rate, center, target)` | live, straight after capture from full-rate IQ |
| `decode_audio_file(path, freq_hz)` | offline, from an audio artifact |
| `decode_episode(episode, artifacts, ...)` | the ladder + DB write, used by both |

Ladder:

| Content | Decoder | Where |
|---|---|---|
| APRS / AX.25 1200-9600 Bd | `multimon-ng` (AFSK1200, AFSK2400, UFSK1200, HAPN4800, FSK9600) | audio |
| POCSAG 512/1200/2400, FLEX | `multimon-ng` | audio |
| NOAA EAS / SAME | `multimon-ng` (EAS) | audio |
| ISM sensors 315/345/433.92/868/915 MHz | `rtl_433` | IQ (cu8) |

Protocols that fire on noise (MORSE_CW, DTMF) are deliberately **not** in the
ladder: a decoded row in the dashboard is a claim about content, so only
decoders with their own error detection (AX.25 FCS, POCSAG/FLEX BCH, SAME
header) are allowed to write one.

Every attempt lands in `decode_attempts` (decoder, status, timing) whether or
not it produced a frame, so a re-run can skip work and an operator can see what
was tried. Only a real frame changes `decode_type`; everything else keeps
`decode_status='captured'`.

## The bug this exposed

`rf_v2_capture._demodulate()` FM-discriminated the raw IQ and low-passed at
3.5 kHz (NFM) around the **tune** frequency. But the capture deliberately tunes
off-centre to dodge the DC spike - +250 kHz narrow, +600 kHz wide - so the audio
passband landed a quarter-megahertz away from the signal it was named after.
Every audio artifact v2 ever wrote, and every audio player in the dashboard, was
of the wrong frequency.

Fixed: `_demodulate(..., offset_hz=)` mixes the offset out first, and
`capture_and_analyse` passes `meta["lo_offset_hz"]`. The decode stage does not
depend on it - it does its own down-conversion (`ddc_audio`) from IQ at full
rate - but the artifacts and the players are now correct.

## Consequence for the historical episodes

The 1341 episodes captured before this fix have LO-centred audio, so their
artifacts cannot be decoded: at best they would decode whatever happened to sit
250-600 kHz from the episode frequency and attribute it to the wrong signal.
They are **not** backfilled. Decoding starts with the next capture. To revisit a
specific old episode, clear its `capture_id` (and set `decode_status='none'`) and
let the scout re-capture it with the fixed path.

## Tests

`tests/test_decode.py` (needs `multimon-ng` and `gen_packets` from direwolf):

1. an APRS frame encoded by **direwolf's `gen_packets`** decodes back with its payload
2. white noise decodes to nothing
3. the live IQ path decodes a reference frame placed +250 kHz off-centre, with noise
4. the parser keeps only checksummed protocols and folds multimon's untagged
   APRS payload line into the frame it belongs to

A hand-rolled AFSK modulator was tried first and thrown away: it satisfied a
local correlator but neither multimon-ng nor direwolf would decode it, so it was
testing the generator, not the decoder. `gen_packets` is an independent reference
encoder and is what the test uses.

Live smoke: `RF_CAPTURE_SECONDS=30 python3 tests/smoke_live_decode.py 144.39`.

## Operational notes

- `multimon-ng -A` (APRS/TNC2 mode) forces AFSK1200 only and silently disables
  the other demodulators - do not add it to the multi-protocol ladder.
- multimon-ng prints the AX.25 header tagged and the info field on an untagged
  following line; the parser folds continuation lines or the payload is lost.
- Live decode costs ~0.3-1 s per capture (DDC + multimon), inside the scout's
  existing capture budget.
