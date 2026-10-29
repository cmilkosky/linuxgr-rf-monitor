"""External decoders: band gating, and direwolf proven on a synthetic APRS packet.

The APRS test builds a real AX.25 UI frame, bit-stuffs it, NRZI-encodes it,
AFSK1200-modulates it, FM-modulates that onto IQ at 144.390 MHz, and runs the
production path (rf_decode_ext.decode_external -> ddc -> direwolf). If direwolf
prints the frame back, the wiring, demodulation and parser are all correct.

Run: python3 tests/test_decode_ext.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rf_decode_ext as ext  # noqa: E402

RATE = 48_000
BAUD = 1200
MARK, SPACE = 1200.0, 2200.0


def _crc16(data: bytes) -> int:
    """AX.25 FCS = CRC-16/X.25: reflected (0x8408), init 0xFFFF, ones-complemented.

    The MSB-first 0x1021 variant is the intuitive one and it is wrong here - a
    frame carrying it is silently rejected by both direwolf and multimon-ng.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _ax25(dest: str, src: str, info: bytes) -> bytes:
    def addr(call: str, last: bool) -> bytes:
        base, _, ssid = call.partition("-")
        out = bytes(ord(c) << 1 for c in base.ljust(6)[:6])
        return out + bytes([0x60 | ((int(ssid or 0) & 0x0F) << 1) | (1 if last else 0)])

    body = addr(dest, False) + addr(src, True) + bytes([0x03, 0xF0]) + info
    fcs = _crc16(body)
    return body + bytes([fcs & 0xFF, fcs >> 8])


def _bits(data: bytes) -> list[int]:
    raw = []
    for byte in data:
        raw.extend((byte >> shift) & 1 for shift in range(0, 8))  # AX.25 is LSB-first
    stuffed, run = [], 0
    for bit in raw:
        stuffed.append(bit)
        run = run + 1 if bit else 0
        if run == 5:
            stuffed.append(0)
            run = 0
    return stuffed


def _afsk(data: bytes) -> np.ndarray:
    # Flags are NOT bit-stuffed: a flag is 01111110, six 1s, so stuffing after
    # five 1s would destroy it. Only the frame body between flags is stuffed.
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    bits = flag * 48 + _bits(data) + flag * 4
    level, phase, out = 1, 0.0, []
    sps = RATE // BAUD
    for bit in bits:
        if bit == 0:                              # NRZI: 0 = transition
            level ^= 1
        step = 2 * np.pi * (MARK if level else SPACE) / RATE
        for _ in range(sps):
            phase += step
            out.append(np.sin(phase))
    return np.asarray(out, dtype=np.float32)


def test_gating():
    assert [c.name for c in ext.matching_candidates(144_390_000, 2_000_000, 144_390_000)] \
        == ["direwolf/aprs"], "APRS dial must be selected for a 144.390 capture"
    assert ext.matching_candidates(100_000_000, 2_000_000, 100_000_000) == [], \
        "nothing should match at 100 MHz"
    names = [c.name for c in ext.matching_candidates(131_550_000, 2_000_000, 131_550_000)]
    assert "acarsdec/131.550" in names, names
    names = [c.name for c in ext.matching_candidates(162_000_000, 2_000_000, 162_000_000)]
    assert "ais-catcher" in names, names
    print("PASS gating: aprs/acars/ais selected only on their dials")


def test_aprs_end_to_end():
    if not shutil.which("direwolf"):
        print("SKIP direwolf not installed")
        return
    info = b">rf-monitor loopback test"
    audio = _afsk(_ax25("APRS", "K1ABC-9", info))
    deviation = 3000.0
    iq = np.exp(1j * 2 * np.pi * deviation * np.cumsum(audio) / RATE).astype(np.complex64)
    results = ext.decode_external(iq, RATE, 144_390_000, 144_390_000, None)
    print("   raw results:", [(n, s, d) for n, s, d, _ in results])
    aprs = [h for name, _, _, hits in results if name == "direwolf/aprs" for h in hits]
    assert aprs, "direwolf decoded nothing from a synthetic APRS packet"
    assert "K1ABC-9" in aprs[0]["line"], aprs[0]["line"]
    print(f"PASS aprs: {aprs[0]['line']}")


def test_silence_is_clean():
    """Every other decoder must run on noise and report no_frame, not crash."""
    noise = (np.random.randn(200_000) + 1j * np.random.randn(200_000)).astype(np.complex64) * 0.01
    for freq in (131_550_000, 162_000_000, 144_174_000):
        for name, status, detail, hits in ext.decode_external(noise, 96_000, freq, freq, None):
            assert status in ("no_frame", "ok"), (name, status, detail)
            print(f"   {name:18s} {status:9s} {detail}")
    print("PASS silence: no crashes, honest no_frame")


if __name__ == "__main__":
    test_gating()
    test_aprs_end_to_end()
    test_silence_is_clean()
