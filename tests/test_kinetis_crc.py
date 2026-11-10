"""Kinetis CRC module (chipem/periph/kinetis/crc.py) on the K22F description, driven through the bus.
K22P100M120SF5V2RM chapter 32."""
from __future__ import annotations

import binascii
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.periph.kinetis.crc import transpose

K22 = Path(__file__).resolve().parent.parent / "chips" / "mk22fn1m0vll12.yaml"
SIM_SCGC6 = 0x4004803C
CRC = 0x40032000
DATA, GPOLY, CTRL = CRC + 0, CRC + 4, CRC + 8
TCRC, WAS, FXOR = 1 << 24, 1 << 25, 1 << 26


def TOT(x):
    return x << 30


def TOTR(x):
    return x << 28


@pytest.fixture(scope="module")
def desc():
    return load_chip(K22)


@pytest.fixture
def chip(desc):
    c = Chip(desc)
    c.reset()
    c.bus.write(SIM_SCGC6, 4, c.bus.read(SIM_SCGC6, 4) | (1 << 18))
    return c


def w(chip, a, v, size=4):
    chip.bus.write(a, size, v)


def r(chip, a, size=4):
    return chip.bus.read(a, size)


def crc16_ccitt(data: bytes, crc: int = 0xFFFF, poly: int = 0x1021) -> int:
    """Hand-written MSB-first CRC-16 (CCITT-FALSE with the defaults)."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def start(chip, ctrl: int, poly: int, seed: int) -> None:
    w(chip, CTRL, ctrl)                     # TCRC first: GPOLY[HIGH] is writable only in 32-bit mode
    w(chip, GPOLY, poly)
    w(chip, CTRL, ctrl | WAS)
    w(chip, DATA, seed)
    w(chip, CTRL, ctrl)


def feed_words(chip, data: bytes) -> None:
    """Little-endian 32-bit words, then the remaining bytes through DATALL (8-bit)."""
    i = 0
    while i + 4 <= len(data):
        w(chip, DATA, int.from_bytes(data[i:i + 4], "little"))
        i += 4
    for b in data[i:]:
        w(chip, DATA, b, 1)


def test_reset_values(chip):
    assert r(chip, DATA) == 0xFFFFFFFF                      # 32.2.1 reset
    assert r(chip, GPOLY) == 0x00001021
    assert r(chip, CTRL) == 0


def test_crc32_ieee_rm_setting_matches_binascii(chip):
    """CRC-32 (IEEE): poly 0x04C11DB7, seed 0xFFFFFFFF, TOT=TOTR=10, FXOR, TCRC=1: "123456789" -> 0xCBF43926."""
    start(chip, TCRC | TOT(2) | TOTR(2) | FXOR, 0x04C11DB7, 0xFFFFFFFF)
    feed_words(chip, b"123456789")
    assert r(chip, DATA) == 0xCBF43926 == binascii.crc32(b"123456789")
    assert chip.peripherals["CRC"].bytes_in == 9
    for msg in (b"", b"a", b"hello world", bytes(range(256)) * 3, b"\xff" * 7):
        start(chip, TCRC | TOT(2) | TOTR(2) | FXOR, 0x04C11DB7, 0xFFFFFFFF)
        feed_words(chip, msg)
        assert r(chip, DATA) == binascii.crc32(msg), msg


def test_crc32_bytewise_and_halfword_writes(chip):
    """A 32-bit write processes 4 bytes, a 16-bit write 2, an 8-bit write 1 (any lane), MSB of the access first."""
    msg = b"The quick brown fox!"
    start(chip, TCRC | TOT(2) | TOTR(2) | FXOR, 0x04C11DB7, 0xFFFFFFFF)
    for b in msg:
        w(chip, DATA + 3, b, 1)                              # DATAHU: one byte, too
    assert r(chip, DATA) == binascii.crc32(msg)
    start(chip, TCRC | TOT(2) | TOTR(2) | FXOR, 0x04C11DB7, 0xFFFFFFFF)
    for i in range(0, len(msg), 2):
        w(chip, DATA, int.from_bytes(msg[i:i + 2], "little"), 2)    # DATAL
    assert r(chip, DATA) == binascii.crc32(msg)


def test_crc16_ccitt_false(chip):
    """16-bit mode, poly 0x1021, seed 0xFFFF in DATA[LU:LL]; TOT=11 makes little-endian word writes enter the engine
    first byte first. "123456789" -> 0x29B1. Byte writes need no transpose."""
    start(chip, TOT(3), 0x1021, 0xFFFF)
    feed_words(chip, b"123456789")
    assert r(chip, DATA) & 0xFFFF == 0x29B1 == crc16_ccitt(b"123456789")
    assert r(chip, DATA, 2) == 0x29B1                       # DATAL
    assert r(chip, DATA) >> 16 == 0xFFFF                    # upper half not used in 16-bit mode (reset value)
    start(chip, 0, 0x1021, 0xFFFF)
    for b in b"123456789":
        w(chip, DATA, b, 1)
    assert r(chip, DATA, 2) == 0x29B1
    # TOT=00 with a 32-bit write: the word's MSB (the 4th string byte in little-endian) goes first
    start(chip, 0, 0x1021, 0xFFFF)
    w(chip, DATA, int.from_bytes(b"1234", "little"))
    assert r(chip, DATA, 2) == crc16_ccitt(b"4321")


def test_crc16_kermit_reflected_with_transpose(chip):
    """CRC-16/KERMIT (reflected in/out, poly 0x1021, seed 0): TOT=10 and TOTR=10; the 16-bit result is moved to
    DATA[31:16] by the read transpose (32.3.3.1 note)."""
    def kermit(data):
        crc = 0
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        return crc
    start(chip, TOT(2) | TOTR(2), 0x1021, 0)
    feed_words(chip, b"123456789")
    v = r(chip, DATA)
    assert v >> 16 == kermit(b"123456789") == 0x2189


def test_seed_protocol_and_reseed(chip):
    """WAS=1: DATA writes are seeds (not processed); clearing WAS starts the data phase; re-asserting WAS
    re-initialises."""
    start(chip, TCRC, 0x04C11DB7, 0x12345678)
    assert r(chip, DATA) == 0x12345678                      # seed visible, nothing computed
    w(chip, CTRL, TCRC | WAS)
    w(chip, DATA, 0xA5, 1)                                  # 8-bit seed write replaces only DATA[7:0]
    assert r(chip, DATA) == 0x123456A5
    w(chip, CTRL, TCRC)
    w(chip, DATA, 0)                                        # 4 zero bytes through the engine
    crc = 0x123456A5
    for _ in range(32):
        crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    assert r(chip, DATA) == crc


def test_read_transposes_and_fxor(chip):
    start(chip, TCRC, 0x04C11DB7, 0x12345678)
    raw = 0x12345678
    for totr in range(4):
        w(chip, CTRL, TCRC | TOTR(totr))
        assert r(chip, DATA) == transpose(raw, totr)
        w(chip, CTRL, TCRC | TOTR(totr) | FXOR)
        assert r(chip, DATA) == transpose(raw, totr) ^ 0xFFFFFFFF
    assert transpose(0x12345678, 1) == 0x482C6A1E           # bits in bytes
    assert transpose(0x12345678, 2) == 0x1E6A2C48           # bits and bytes
    assert transpose(0x12345678, 3) == 0x78563412           # bytes only
    w(chip, CTRL, TCRC)
    assert r(chip, DATA + 2, 1) == 0x34 and r(chip, DATA + 1, 1) == 0x56   # byte views of the result


def test_gpoly_high_not_writable_in_16bit_mode_and_ctrl_reserved_bits(chip):
    w(chip, CTRL, 0)
    w(chip, GPOLY, 0x04C11DB7)
    assert r(chip, GPOLY) == 0x00001DB7                     # 32.2.2: HIGH ignored with TCRC=0
    w(chip, CTRL, TCRC)
    w(chip, GPOLY, 0x04C11DB7)
    assert r(chip, GPOLY) == 0x04C11DB7
    w(chip, CTRL, 0xFFFFFFFF)
    assert r(chip, CTRL) == 0xF7000000                      # bit 27 and 23-0 reserved
    assert r(chip, CRC + 0xB, 1) == 0xF7                    # CTRLHU view


def test_clock_gate_warns_once(desc):
    c = Chip(desc)
    c.reset()
    c.bus.read(DATA, 4)
    c.bus.write(DATA, 4, 1)
    warns = [e.fields["msg"] for e in c.log if e.kind == "warn" and "CRC" in e.fields.get("msg", "")]
    assert len(warns) == 1 and "SIM_SCGC6 bit 18" in warns[0]
