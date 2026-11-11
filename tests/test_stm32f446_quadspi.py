"""STM32F446 QUADSPI (chipem/periph/stm32/quadspi.py) against the quad-SPI NOR flash device
(chipem/devices/qspi_flash.py): indirect read/write with exact phase timing, FIFO threshold / stall, every SR flag
(TEF, TCF, FTF, SMF, TOF, BUSY, FLEVEL), automatic status polling (APMS, AND/OR match), abort, write protection while
BUSY, memory-mapped mode (direct array mapping and per-access path), pins, QE gating, dummy-cycle mismatch, IRQ 92 and
the DMA request.  Clock in these tests: HSI 16 MHz, PRESCALER 0 -> QSPI CLK 16 MHz (62.5 ns)."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.devices.qspi_flash import QspiNorFlash
from chipem.firmware import FirmwareImage

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
FLASH = 0x08000000
RCC = 0x40023800
Q = 0xA0001000
CR, DCR, SR, FCR, DLR, CCR, AR, ABR, DR, PSMKR, PSMAR, PIR, LPTR = range(0, 0x34, 4)
TEF, TCF, FTF, SMF, TOF, BUSY = 1, 2, 4, 8, 16, 32
T = 62.5


def ccr(instr, imode=1, admode=0, adsize=2, abmode=0, absize=0, dcyc=0, dmode=0, fmode=0):
    return (instr | imode << 8 | admode << 10 | adsize << 12 | abmode << 14 | absize << 16 | dcyc << 18 | dmode << 24
            | fmode << 26)


@pytest.fixture(scope="module")
def descs():
    return {fn: load_chip(ROOT / "chips" / f"{fn}.yaml") for fn in ("stm32f446re", "stm32f446ze")}


def make(descs, fn="stm32f446re", mux=True, **flash_kw):
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    c = Chip(descs[fn])
    c.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    c.reset()
    kw = {"t_pp_us": 100, "t_se_us": 1000, "t_w_us": 50}
    kw.update(flash_kw)
    dev = QspiNorFlash(**kw)
    dev.mem[0:8] = b"ABCDEFGH"
    c.peripherals["QUADSPI"].attach_qspi_flash(dev, 1)
    w = c.bus.write
    w(RCC + 0x30, 4, 0x3F)                                       # GPIOA..F
    w(RCC + 0x38, 4, 2)                                          # QSPIEN
    if mux:
        pins = ([("B", 2, 9), ("B", 6, 10), ("C", 9, 9), ("C", 10, 9), ("A", 1, 9)] if fn == "stm32f446re" else
                [("B", 2, 9), ("B", 6, 10), ("F", 8, 10), ("F", 9, 10), ("F", 7, 9), ("F", 6, 9)])
        for port, pin, af in pins:
            base = 0x40020000 + 0x400 * "ABCDEF".index(port)
            w(base, 4, c.bus.read(base, 4) | (2 << 2 * pin))
            off = 0x20 + 4 * (pin // 8)
            w(base + off, 4, c.bus.read(base + off, 4) | (af << 4 * (pin % 8)))
    w(Q + CR, 4, (3 << 8) | 1)                                   # FTHRES 4, EN, PRESCALER 0
    w(Q + DCR, 4, 23 << 16)                                      # 16 MB
    return c, dev


def r(c, off):
    return c.bus.read(Q + off, 4)


def w(c, off, v, n=4):
    c.bus.write(Q + off, n, v)


def warns(c):
    return [e.fields.get("msg", "") for e in c.log if e.kind == "warn"]


def until(c, cond, step=50.0, limit=1e7):
    t0 = c.now_ns()
    while not cond():
        c.run_for(step)
        assert c.now_ns() - t0 < limit
    return c.now_ns() - t0


def test_indirect_read_timing_ftf_tcf_busy(descs):
    c, _ = make(descs)
    w(c, DLR, 3)
    w(c, CCR, ccr(0x03, admode=1, dmode=1, fmode=1))
    t0 = c.now_ns()
    w(c, AR, 0)                                                  # address phase: starts the command
    assert r(c, SR) & BUSY and not r(c, SR) & TCF
    # instruction 8 + address 24 + data 32 clocks = 64 x 62.5 ns
    c.run_for(64 * T - 300)
    assert not r(c, SR) & TCF
    c.run_for(400)
    sr = r(c, SR)
    assert sr & TCF and sr & FTF and sr & BUSY and (sr >> 8) & 0x3F == 4
    assert c.bus.read(Q + DR, 4) == int.from_bytes(b"ABCD", "little")
    sr = r(c, SR)
    assert not sr & (BUSY | FTF) and (sr >> 8) & 0x3F == 0
    w(c, FCR, TCF)
    assert not r(c, SR) & TCF and r(c, FCR) == 0
    assert c.now_ns() - t0 >= 64 * T


def test_fifo_full_stalls_the_clock(descs):
    c, dev = make(descs)
    dev.mem[0:64] = bytes(range(64))
    w(c, DLR, 39)
    w(c, CCR, ccr(0x03, admode=1, dmode=1, fmode=1))
    w(c, AR, 0)
    c.run_for(100_000)
    sr = r(c, SR)
    assert (sr >> 8) & 0x3F == 32 and not sr & TCF and sr & BUSY       # 32-byte FIFO full: clock stopped
    assert bytes(c.bus.read(Q + DR, 1) for _ in range(8)) == bytes(range(8))
    c.run_for(8 * 8 * T - 100)
    assert not r(c, SR) & TCF                                            # the 8 bytes need 8 x 8 clocks again
    c.run_for(200)
    assert r(c, SR) & TCF
    assert bytes(c.bus.read(Q + DR, 1) for _ in range(32)) == bytes(range(8, 40))


def test_program_and_status_polling_apms(descs):
    c, dev = make(descs)
    w(c, CR, r(c, CR) | (1 << 19))                                   # SMIE
    w(c, CCR, ccr(0x06))                                             # WREN: starts on the CCR write
    c.run_for(1000)
    assert r(c, SR) & TCF and dev.wel
    w(c, FCR, TCF)
    w(c, DLR, 3)
    w(c, CCR, ccr(0x02, admode=1, dmode=1, fmode=0))
    w(c, AR, 0x100)
    assert not r(c, SR) & BUSY and r(c, SR) & FTF                   # write mode: starts on the first DR write
    w(c, DR, 0x44434241)
    assert r(c, SR) & BUSY
    until(c, lambda: r(c, SR) & TCF)
    t_end = c.now_ns()
    w(c, FCR, TCF)
    assert bytes(dev.mem[0x100:0x104]) == b"ABCD" and dev.busy(c.now_ns())
    w(c, PSMKR, 1)
    w(c, PSMAR, 0)
    w(c, PIR, 16)
    w(c, CR, r(c, CR) | (1 << 22))                                   # APMS
    w(c, DLR, 0)
    w(c, CCR, ccr(0x05, dmode=1, fmode=2))
    assert r(c, SR) & BUSY and not r(c, SR) & SMF
    c.run_for(5000)
    assert c.bus.read(Q + DR, 1) & 1 and not r(c, SR) & SMF           # last status read: WIP
    dt = until(c, lambda: r(c, SR) & SMF, step=2000)
    period = (16 + 16) * T                                           # 8 + 8 clocks + max(PIR, CSHT + 1)
    assert 100_000 - 3000 < c.now_ns() - t_end < 100_000 + period + 2000
    assert not r(c, SR) & BUSY and c.bus.read(Q + DR, 1) == 0
    assert c.peripherals["QUADSPI"]._line                           # SMF & SMIE: IRQ 92 (the spin loop never
    assert dt > 0                                                    # enables interrupts, so it stays pending)
    w(c, FCR, SMF)
    assert not r(c, SR) & SMF


def test_polling_without_apms_or_mode_and_abort(descs):
    c, dev = make(descs)
    dev.sr2 = 0x02
    w(c, PSMKR, 0x0200)                                              # byte 1 (second status byte) bit 1
    w(c, PSMAR, 0x0200)
    w(c, CR, r(c, CR) | (1 << 23))                                   # PMM = OR
    w(c, DLR, 1)
    w(c, CCR, ccr(0x35, dmode=1, fmode=2))                           # RDSR2 twice: 0x02, 0x02
    until(c, lambda: r(c, SR) & SMF)
    assert r(c, SR) & BUSY                                           # APMS = 0: polling goes on
    assert c.bus.read(Q + DR, 2) == 0x0202
    w(c, CCR, ccr(0x05))                                             # protected while BUSY
    assert any("CCR written while BUSY" in m for m in warns(c))
    w(c, CR, r(c, CR) | 2)                                           # ABORT
    sr = r(c, SR)
    assert sr & TCF and not sr & BUSY and not r(c, CR) & 2


def test_tef_outside_fsize(descs):
    c, _ = make(descs)
    w(c, DCR, 9 << 16)                                               # 1 KB flash
    w(c, DLR, 0)
    w(c, CCR, ccr(0x03, admode=1, dmode=1, fmode=1))
    w(c, AR, 0x1000)
    sr = r(c, SR)
    assert sr & TEF and not sr & BUSY
    w(c, CR, r(c, CR) | (1 << 16))                                   # TEIE -> IRQ 92 line
    assert c.peripherals["QUADSPI"]._line


def test_memory_mapped_direct_and_shifted(descs):
    c, dev = make(descs)
    w(c, CCR, ccr(0x0B, admode=1, dcyc=8, dmode=1, fmode=3))
    q = c.peripherals["QUADSPI"]
    assert q.mm == "fast"
    assert c.bus.read(0x90000000, 4) == int.from_bytes(b"ABCD", "little")
    dev.mem[4] = 0x5A
    assert c.bus.read(0x90000004, 1) == 0x5A                         # the flash array itself is mapped
    w(c, CR, r(c, CR) | 2)                                           # abort, then a new FMODE
    w(c, CCR, ccr(0x0B, admode=1, dcyc=7, dmode=1, fmode=3))         # one dummy clock short
    assert q.mm == "slow"
    v = c.bus.read(0x90000000, 1)
    assert v == ((ord("A") >> 1) | 0x80)                             # sampled one bit early, MSB pulled up
    assert any("shifted by -1 bits" in m for m in warns(c))


def test_memory_mapped_timeout_tof(descs):
    c, _ = make(descs)
    w(c, LPTR, 100)
    w(c, CR, r(c, CR) | (1 << 3) | (1 << 20))                        # TCEN, TOIE
    w(c, CCR, ccr(0x03, admode=1, dmode=1, fmode=3))
    assert c.peripherals["QUADSPI"].mm == "slow"
    assert not r(c, SR) & BUSY                                       # RM0390 12.3.7: BUSY rises with the first access
    assert c.bus.read(0x90000001, 1) == ord("B")
    assert r(c, SR) & BUSY
    # RM0390 12.5.13: TIMEOUT counts from the FIFO being full of prefetched data (32 bytes x 8 clocks, 1 line)
    c.run_for((32 * 8 + 100) * T - 200)
    assert not r(c, SR) & TOF and r(c, SR) & BUSY
    c.run_for(400)
    sr = r(c, SR)
    assert sr & TOF and not sr & BUSY and c.peripherals["QUADSPI"]._line     # 12.3.14: BUSY falls on the timeout


def test_pins_not_muxed(descs):
    c, _ = make(descs, mux=False)
    w(c, DLR, 0)
    w(c, CCR, ccr(0x9F, dmode=1, fmode=1))
    c.run_for(2000)
    assert c.bus.read(Q + DR, 1) == 0xFF
    assert any("not muxed" in m for m in warns(c))


def test_quad_needs_qe_and_wrong_lines(descs):
    c, dev = make(descs, fn="stm32f446ze")
    w(c, DLR, 3)
    w(c, CCR, ccr(0x6B, admode=1, dcyc=8, dmode=3, fmode=1))
    w(c, AR, 0)
    until(c, lambda: r(c, SR) & TCF)
    assert c.bus.read(Q + DR, 4) == 0xFFFFFFFF and any("QE = 0" in m for m in warns(c))
    w(c, CR, r(c, CR) | 2)
    w(c, FCR, TCF)
    dev.sr2 = 0x02
    w(c, CCR, ccr(0x6B, admode=1, dcyc=8, dmode=3, fmode=1))
    t0 = c.now_ns()
    w(c, AR, 0)
    until(c, lambda: r(c, SR) & TCF, step=20)
    # 8 + 24 address + 8 dummy + 4 bytes x 2 clocks = 48 clocks
    assert 48 * T <= c.now_ns() - t0 < 48 * T + 100
    assert c.bus.read(Q + DR, 4) == int.from_bytes(b"ABCD", "little")
    w(c, CR, r(c, CR) | 2)
    w(c, FCR, TCF)
    w(c, CCR, ccr(0xEB, admode=1, abmode=3, dcyc=4, dmode=3, fmode=1))   # 0xEB needs the address on 4 lines
    w(c, AR, 0)
    until(c, lambda: r(c, SR) & TCF)
    assert c.bus.read(Q + DR, 4) == 0xFFFFFFFF and any("address on 1 line(s), needs 4" in m for m in warns(c))


def test_dma_request_follows_ftf(descs):
    c, _ = make(descs)
    d2 = c.peripherals["DMA2"]
    w(c, CR, r(c, CR) | 4)                                           # DMAEN
    w(c, DLR, 7)
    w(c, CCR, ccr(0x03, admode=1, dmode=1, fmode=1))
    w(c, AR, 0)
    assert not d2.levels.get("QUADSPI")
    until(c, lambda: r(c, SR) & FTF)
    assert d2.levels.get("QUADSPI")
    c.bus.read(Q + DR, 4)
    assert r(c, SR) & FTF or not d2.levels.get("QUADSPI")


def test_reset_through_rcc(descs):
    c, _ = make(descs)
    w(c, CCR, ccr(0x0B, admode=1, dcyc=8, dmode=1, fmode=3))
    assert c.peripherals["QUADSPI"].mm == "fast"
    c.bus.write(RCC + 0x18, 4, 2)                                    # AHB3RSTR.QSPIRST
    assert c.peripherals["QUADSPI"].mm is None and r(c, CR) == 0


# ---------------------------------------------------------------------------------------- RM0390 regressions
def test_sr_reset_value_and_ftf_needs_en(descs):
    """RM0390 12.5.3: QUADSPI_SR resets to 0 (FTF of an idle write-mode FIFO only once EN = 1)."""
    c, _ = make(descs)
    w(c, CR, 0)
    assert r(c, SR) == 0
    w(c, CR, (3 << 8) | 1)
    assert r(c, SR) & FTF                                            # FMODE 00, empty FIFO: free bytes >= threshold


def test_read_restarts_with_four_free_bytes(descs):
    """RM0390 12.3.5: with a full FIFO the flash read stops and restarts only once 4 bytes are vacant."""
    c, dev = make(descs)
    dev.mem[0:64] = bytes(range(64))
    w(c, DLR, 39)
    w(c, CCR, ccr(0x03, admode=1, dmode=1, fmode=1))
    w(c, AR, 0)
    c.run_for(100_000)
    assert (r(c, SR) >> 8) & 0x3F == 32
    assert c.bus.read(Q + DR, 1) == 0
    c.run_for(20 * 8 * T)
    assert (r(c, SR) >> 8) & 0x3F == 31                              # 1 byte free: still stopped
    assert c.bus.read(Q + DR, 2) == 0x0201 and c.bus.read(Q + DR, 1) == 3
    c.run_for(4 * 8 * T + 50)
    assert (r(c, SR) >> 8) & 0x3F == 32                              # 4 free: 4 more bytes read


def test_polling_ftf_per_status_read(descs):
    """RM0390 12.3.6 / 12.5.3: in automatic polling FTF is set after every status read and cleared by a DR read;
    FLEVEL stays 0."""
    c, dev = make(descs)
    dev.sr2 = 0x00
    w(c, PSMKR, 0x02)
    w(c, PSMAR, 0x02)                                                # never matches
    w(c, PIR, 16)
    w(c, DLR, 0)
    w(c, CR, r(c, CR) | (1 << 18))                                   # FTIE
    w(c, CCR, ccr(0x35, dmode=1, fmode=2))
    q = c.peripherals["QUADSPI"]
    assert not r(c, SR) & FTF and not q._line
    c.run_for(16 * T + 100)                                          # first read: 8 + 8 clocks
    sr = r(c, SR)
    assert sr & FTF and sr & BUSY and (sr >> 8) & 0x3F == 0 and q._line
    assert c.bus.read(Q + DR, 1) == 0
    assert not r(c, SR) & FTF and not q._line
    c.run_for(32 * T)                                                # next read 16 + 16 clocks later
    assert r(c, SR) & FTF and q._line


def test_tef_address_plus_length_and_polling(descs):
    """RM0390 12.3.13: TEF when address + data length exceed FSIZE (indirect) and for a wrong address in polling."""
    c, _ = make(descs)
    w(c, DCR, 9 << 16)                                               # 1 KB flash
    w(c, DLR, 15)
    w(c, CCR, ccr(0x03, admode=1, dmode=1, fmode=1))
    w(c, AR, 0x3F8)                                                  # 0x3F8 + 16 > 0x400
    assert r(c, SR) & TEF and not r(c, SR) & BUSY
    w(c, FCR, TEF)
    w(c, DLR, 7)
    w(c, AR, 0x3F8)                                                  # exactly up to the end: fine
    assert not r(c, SR) & TEF and r(c, SR) & BUSY
    w(c, CR, r(c, CR) | 2)
    w(c, FCR, TCF)
    w(c, DLR, 0)
    w(c, CCR, ccr(0x05, admode=1, dmode=1, fmode=2))
    w(c, AR, 0x400)
    assert r(c, SR) & TEF and not r(c, SR) & BUSY


def test_ar_ignored_in_memory_mapped_mode(descs):
    """RM0390 12.5.7: AR writes are ignored when FMODE = 11."""
    c, _ = make(descs)
    w(c, AR, 0x10)
    w(c, CCR, ccr(0x0B, admode=1, dcyc=8, dmode=1, fmode=3))
    w(c, AR, 0x1234)
    assert r(c, AR) == 0x10


def test_write_mode_extra_bytes_flushed(descs):
    """RM0390 12.3.5: bytes written beyond DL are flushed from the FIFO when TCF is set."""
    c, dev = make(descs)
    w(c, CCR, ccr(0x06))
    c.run_for(1000)
    w(c, FCR, TCF)
    w(c, DLR, 1)                                                     # 2 bytes
    w(c, CCR, ccr(0x02, admode=1, dmode=1, fmode=0))
    w(c, AR, 0x200)
    w(c, DR, 0x44434241)                                             # 4 bytes into the FIFO
    until(c, lambda: r(c, SR) & TCF)
    assert (r(c, SR) >> 8) & 0x3F == 0 and not r(c, SR) & BUSY
    assert bytes(dev.mem[0x200:0x204]) == b"AB\xff\xff"


def make_dfm(descs):
    c, d1 = make(descs, fn="stm32f446ze")
    d2 = QspiNorFlash(t_pp_us=100, t_se_us=1000, t_w_us=50)
    c.peripherals["QUADSPI"].attach_qspi_flash(d2, 2)
    wb = c.bus.write
    for port, pin, af in [("E", 7, 10), ("E", 8, 10), ("E", 9, 10), ("E", 10, 10)]:
        base = 0x40020000 + 0x400 * "ABCDEFGH".index(port)
        wb(base, 4, c.bus.read(base, 4) | (2 << 2 * pin))
        off = 0x20 + 4 * (pin // 8)
        wb(base + off, 4, c.bus.read(base + off, 4) | (af << 4 * (pin % 8)))
    w(c, CR, r(c, CR) | (1 << 6))                                    # DFM
    w(c, DCR, 24 << 16)                                              # 32 MB = 2 x 16 MB
    return c, d1, d2


def test_dual_flash_mode(descs):
    """RM0390 12.3.4 / 12.5.5 / 12.5.7: DFM interleaves even bytes (flash 1) and odd bytes (flash 2) at address / 2,
    8 data lines; DL[0] reads 1, ADDRESS[0] reads 0; status bytes interleave too; writes are split."""
    c, d1, d2 = make_dfm(descs)
    d1.mem[0:4] = b"ACEG"
    d2.mem[0:4] = b"BDFH"
    w(c, DLR, 6)
    assert r(c, DLR) == 7
    w(c, CCR, ccr(0x03, admode=1, dmode=1, fmode=1))
    t0 = c.now_ns()
    w(c, AR, 1)
    assert r(c, AR) == 0
    # 8 instruction + 24 address clocks + 8 bytes x 4 clocks (two flashes x 1 line)
    until(c, lambda: r(c, SR) & TCF, step=20)
    assert 64 * T <= c.now_ns() - t0 < 64 * T + 100
    assert c.bus.read(Q + DR, 4) == int.from_bytes(b"ABCD", "little")
    assert c.bus.read(Q + DR, 4) == int.from_bytes(b"EFGH", "little")
    w(c, FCR, TCF)
    w(c, CCR, ccr(0x06))                                             # WREN to both flashes
    c.run_for(1000)
    assert d1.wel and d2.wel
    w(c, FCR, TCF)
    w(c, DLR, 3)
    w(c, CCR, ccr(0x02, admode=1, dmode=1, fmode=0))
    w(c, AR, 0x100)
    w(c, DR, int.from_bytes(b"wxyz", "little"))
    until(c, lambda: r(c, SR) & TCF)
    assert bytes(d1.mem[0x80:0x82]) == b"wy" and bytes(d2.mem[0x80:0x82]) == b"xz"
    w(c, FCR, TCF)
    d2.sr2 = 0x02
    w(c, DLR, 1)
    w(c, CCR, ccr(0x35, dmode=1, fmode=1))                           # RDSR2 of both: flash 1 byte, flash 2 byte
    until(c, lambda: r(c, SR) & TCF)
    assert c.bus.read(Q + DR, 2) == 0x0200


def test_ddr_prescaler_rule_warns(descs):
    """RM0390 12.5.1: DDR mode needs PRESCALER >= 1; RM0390 12.3.9: SSHIFT must be 0 with DDRM."""
    c, _ = make(descs)
    w(c, CR, r(c, CR) | (1 << 4))
    w(c, DLR, 0)
    w(c, CCR, ccr(0x03, admode=1, dmode=1, fmode=1) | (1 << 31))
    w(c, AR, 0)
    m = warns(c)
    assert any("PRESCALER = 0" in x for x in m) and any("SSHIFT = 1 in DDR" in x for x in m)


def test_sioo_skips_the_instruction_phase(descs):
    """RM0390 12.3.12: with SIOO the instruction is sent only with the first command after a CCR write."""
    c, _ = make(descs)
    w(c, DLR, 3)
    w(c, CCR, ccr(0x03, admode=1, dmode=1, fmode=1) | (1 << 28))
    t0 = c.now_ns()
    w(c, AR, 0)
    until(c, lambda: r(c, SR) & TCF, step=20)
    assert 64 * T <= c.now_ns() - t0 < 64 * T + 100                  # first command: 8 + 24 + 32 clocks
    c.bus.read(Q + DR, 4)
    w(c, FCR, TCF)
    t0 = c.now_ns()
    w(c, AR, 4)
    until(c, lambda: r(c, SR) & TCF, step=20)
    assert 56 * T <= c.now_ns() - t0 < 56 * T + 100                  # no instruction phase: 24 + 32 clocks
    assert c.bus.read(Q + DR, 4) == int.from_bytes(b"EFGH", "little")
