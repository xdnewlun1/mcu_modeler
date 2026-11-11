"""stm32.dma (RM0090 ch. 10): request mapping (Tables 42/43), stream configuration, arbitration, FIFO packing,
circular and double-buffer modes, flags/IRQs, errors, and the USART DMA requests (CR3.DMAT/DMAR), driven through the
bus on the real STM32F405ZG chip YAML with a spin-loop firmware."""
from __future__ import annotations

import struct

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage
from chipem.periph.stm32._util import dma_request
from chipem.periph.stm32.dma import REQUESTS, _fifo_cfg_ok

from engine_util import asm
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "chips" / "stm32f405zg.yaml"
FLASH = 0x08000000
RCC, GPIOA = 0x40023800, 0x40020000
USART1, USART2 = 0x40011000, 0x40004400
DMA1, DMA2 = 0x40026000, 0x40026400
LISR, HISR, LIFCR, HIFCR = 0x0, 0x4, 0x8, 0xC
RAM = 0x20001000

EN, DMEIE, TEIE, HTIE, TCIE = 1, 2, 4, 8, 16
P2M, M2P, M2M = 0 << 6, 1 << 6, 2 << 6
CIRC, PINC, MINC, PINCOS, DBM, CT = 1 << 8, 1 << 9, 1 << 10, 1 << 15, 1 << 18, 1 << 19
TCIF, HTIF, TEIF, DMEIF, FEIF = 0x20, 0x10, 0x08, 0x04, 0x01


def psize(n):
    return {1: 0, 2: 1, 4: 2}[n] << 11


def msize(n):
    return {1: 0, 2: 1, 4: 2}[n] << 13


def pl(n):
    return n << 16


def chsel(n):
    return n << 25


def sreg(base, s, off):
    return base + 0x10 + 0x18 * s + off


CR, NDTR, PAR, M0AR, M1AR, FCR = 0x0, 0x4, 0x8, 0xC, 0x10, 0x14


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def spin_chip(desc, **kw) -> Chip:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc, **kw)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    wr(chip, RCC + 0x30, rd(chip, RCC + 0x30) | 3 << 21 | 1)      # DMA1EN DMA2EN GPIOAEN
    wr(chip, RCC + 0x44, rd(chip, RCC + 0x44) | 1 << 4)           # USART1EN
    wr(chip, RCC + 0x40, rd(chip, RCC + 0x40) | 1 << 17)          # USART2EN
    return chip


def rd(chip, addr, n=4):
    return chip.bus.read(addr, n)


def wr(chip, addr, v, n=4):
    chip.bus.write(addr, n, v)


def asserted(chip, irq: int) -> bool:
    return (16 + irq) in chip.arch.asserted


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def setup(chip, base, s, cr, ndtr, par, m0, m1=0, fcr=None):
    wr(chip, sreg(base, s, NDTR), ndtr)
    wr(chip, sreg(base, s, PAR), par)
    wr(chip, sreg(base, s, M0AR), m0)
    wr(chip, sreg(base, s, M1AR), m1)
    if fcr is not None:
        wr(chip, sreg(base, s, FCR), fcr)
    wr(chip, sreg(base, s, CR), cr)


def flags(chip, base, s):
    reg = rd(chip, base + (LISR if s < 4 else HISR))
    return reg >> (0, 6, 16, 22)[s & 3] & 0x3D


def settle(chip):
    chip.run_cycles(20)


# ------------------------------------------------------------------------------------------------ tables / API
def test_request_tables_rm0090():
    t1, t2 = REQUESTS[1], REQUESTS[2]
    # Table 42 (DMA1): spot checks [stream][channel]
    assert t1[5][4] == ("USART2_RX",) and t1[6][4] == ("USART2_TX",)
    assert t1[1][4] == ("USART3_RX",) and t1[3][4] == ("USART3_TX",) and t1[4][7] == ("USART3_TX",)
    assert t1[2][4] == ("UART4_RX",) and t1[4][4] == ("UART4_TX",) and t1[0][4] == ("UART5_RX",)
    assert t1[2][5] == ("TIM3_CH4", "TIM3_UP") and t1[4][5] == ("TIM3_CH1", "TIM3_TRIG") and t1[5][5] == ("TIM3_CH2",)
    assert t1[1][3] == ("TIM2_UP", "TIM2_CH3") and t1[1][7] == ("TIM6_UP",) and t1[5][7] == ("DAC1",)
    assert t1[0][0] == ("SPI3_RX",) and t1[3][0] == ("SPI2_RX",) and t1[4][0] == ("SPI2_TX",)
    assert t1[1][0] == () and t1[6][0] == ()
    # F42x/F43x-only requests (Table 43/44 note 1; never raised on an F405/F407)
    assert t1[6][5] == ("UART8_RX",) and t1[1][5] == ("UART7_TX",) and t1[3][5] == ("UART7_RX",)
    assert t2[1][0] == ("SAI1_A",) and t2[4][1] == ("SAI1_B",) and t2[3][2] == ("SPI5_RX",) and t2[1][4] == ("SPI4_TX",)
    # Table 43 (DMA2)
    assert t2[7][4] == ("USART1_TX",) and t2[2][4] == ("USART1_RX",) and t2[5][4] == ("USART1_RX",)
    assert t2[1][5] == ("USART6_RX",) and t2[2][5] == ("USART6_RX",) and t2[6][5] == ("USART6_TX",)
    assert t2[7][5] == ("USART6_TX",)
    assert t2[0][0] == ("ADC1",) and t2[4][0] == ("ADC1",) and t2[2][1] == ("ADC2",) and t2[0][2] == ("ADC3",)
    assert t2[0][3] == ("SPI1_RX",) and t2[3][3] == ("SPI1_TX",) and t2[5][6] == ("TIM1_UP",)
    assert t2[4][6] == ("TIM1_CH4", "TIM1_TRIG", "TIM1_COM") and t2[1][7] == ("TIM8_UP",)
    for t in (t1, t2):
        assert len(t) == 8 and all(len(row) == 8 for row in t)


def test_request_api_routing(desc):
    chip = spin_chip(desc)
    d1, d2 = chip.peripherals["DMA1"], chip.peripherals["DMA2"]
    assert d1.model == d2.model == "stm32.dma" and (d1.controller, d2.controller) == (1, 2)
    assert dma_request(chip, "USART1_TX", True) and d2.request_active("USART1_TX")
    assert not d1.request_active("USART1_TX")
    assert dma_request(chip, "USART2_TX", True) and d1.request_active("USART2_TX")
    assert dma_request(chip, "TIM3_UP", True) and dma_request(chip, "ADC1", True)
    assert not dma_request(chip, "NOSUCH_RX", True)                   # unmapped: ignored
    assert not d2.request("USART2_TX", True)                         # DMA2 has no USART2
    dma_request(chip, "USART1_TX", False)
    assert not d2.request_active("USART1_TX")


def test_fifo_threshold_table48():
    # (msize, mburst, fth): byte INCR8 needs 1/2 or full; half INCR4 1/2 or full; word INCR4 only full
    assert _fifo_cfg_ok(1, 1, 0) and _fifo_cfg_ok(1, 2, 1) and not _fifo_cfg_ok(1, 2, 0)
    assert not _fifo_cfg_ok(1, 3, 2) and _fifo_cfg_ok(1, 3, 3)
    assert not _fifo_cfg_ok(2, 1, 0) and _fifo_cfg_ok(2, 1, 1) and not _fifo_cfg_ok(2, 1, 2)
    assert _fifo_cfg_ok(4, 1, 3) and not _fifo_cfg_ok(4, 1, 1) and not _fifo_cfg_ok(4, 2, 3)
    assert all(_fifo_cfg_ok(m, 0, f) for m in (1, 2, 4) for f in range(4))


def test_registers_reset_and_ifcr(desc):
    chip = spin_chip(desc)
    for s in range(8):
        assert rd(chip, sreg(DMA2, s, FCR)) == 0x21                      # FS = empty, FTH = 1/2
        assert rd(chip, sreg(DMA2, s, CR)) == 0
    assert rd(chip, DMA2 + LIFCR) == 0


# ------------------------------------------------------------------------------------------------ M2M
def test_m2m_runs_after_the_write_with_tc_irq(desc):
    chip = spin_chip(desc)
    src, dst = RAM, RAM + 0x100
    chip.bus.write_bytes(src, bytes(range(1, 33)))
    setup(chip, DMA2, 0, M2M | PINC | MINC | psize(4) | msize(4) | TCIE | HTIE | EN, 8, src, dst)
    # nothing happens inside the MMIO write: the transfer is scheduled for "after the store"
    assert rd(chip, sreg(DMA2, 0, CR)) & EN and rd(chip, sreg(DMA2, 0, NDTR)) == 8
    assert chip.bus.read_bytes(dst, 32) == bytes(32)
    assert rd(chip, sreg(DMA2, 0, FCR)) & 4                           # DMDIS forced in M2M
    settle(chip)
    assert chip.bus.read_bytes(dst, 32) == bytes(range(1, 33))
    assert rd(chip, sreg(DMA2, 0, CR)) & EN == 0 and rd(chip, sreg(DMA2, 0, NDTR)) == 0
    assert rd(chip, sreg(DMA2, 0, PAR)) == src and rd(chip, sreg(DMA2, 0, M0AR)) == dst    # registers not advanced
    assert flags(chip, DMA2, 0) == TCIF | HTIF
    assert asserted(chip, 56)                                         # DMA2_Stream0 line (TCIE)
    wr(chip, DMA2 + LIFCR, TCIF | HTIF)
    assert flags(chip, DMA2, 0) == 0 and not asserted(chip, 56)


def test_m2m_packing_byte_to_word_and_stream_flags_in_hisr(desc):
    chip = spin_chip(desc)
    src, dst = RAM, RAM + 0x100
    chip.bus.write_bytes(src, bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xAA]))
    # 10 bytes -> 2 words + a 2-byte remainder flushed at the end; MSIZE word, threshold full
    setup(chip, DMA2, 6, M2M | PINC | MINC | psize(1) | msize(4) | EN, 10, src, dst, fcr=0x7)
    settle(chip)
    assert rd(chip, dst) == 0x44332211 and rd(chip, dst + 4) == 0x88776655 and rd(chip, dst + 8, 2) == 0xAA99
    assert flags(chip, DMA2, 6) == TCIF | HTIF
    assert rd(chip, DMA2 + HISR) == (TCIF | HTIF) << 16
    assert rd(chip, sreg(DMA2, 6, FCR)) >> 3 & 7 == 4                 # FS: empty
    wr(chip, DMA2 + HIFCR, (TCIF | HTIF) << 16)
    assert rd(chip, DMA2 + HISR) == 0


def test_m2m_word_to_halfword_unpacking_and_pincos_ignored_in_direct(desc):
    chip = spin_chip(desc)
    src, dst = RAM, RAM + 0x100
    chip.bus.write_bytes(src, struct.pack("<II", 0xA1A2A3A4, 0xB1B2B3B4))
    setup(chip, DMA2, 1, M2M | PINC | MINC | psize(4) | msize(2) | EN, 2, src, dst, fcr=0x5)
    settle(chip)
    assert struct.unpack("<HHHH", chip.bus.read_bytes(dst, 8)) == (0xA3A4, 0xA1A2, 0xB3B4, 0xB1B2)


def test_dma1_cannot_do_m2m(desc):
    chip = spin_chip(desc)
    setup(chip, DMA1, 0, M2M | PINC | MINC | TEIE | EN, 4, RAM, RAM + 0x100)
    settle(chip)
    assert flags(chip, DMA1, 0) == TEIF and rd(chip, sreg(DMA1, 0, CR)) & EN == 0
    assert asserted(chip, 11)                                         # DMA1_Stream0
    assert any("only possible on DMA2" in w for w in warns(chip))


def test_arbitration_priority_then_stream_number(desc):
    chip = spin_chip(desc)
    order: list[int] = []
    dma = chip.peripherals["DMA2"]
    orig = dma._item
    dma._item = lambda s: (order.append(s), orig(s))[1]
    chip.bus.write_bytes(RAM, bytes(range(16)))
    # stream 1 PL low, stream 2 PL very high, stream 3 PL very high -> 2 first (tie: lower number), then 3, then 1
    for s, p in ((1, 0), (3, 3), (2, 3)):
        setup(chip, DMA2, s, M2M | PINC | MINC | pl(p) | EN, 2, RAM, RAM + 0x100 * s)
    settle(chip)
    assert order == [2, 2, 3, 3, 1, 1]
    assert all(flags(chip, DMA2, s) & TCIF for s in (1, 2, 3))


def test_bus_error_sets_teif_and_disables(desc):
    chip = spin_chip(desc)
    setup(chip, DMA2, 1, M2M | PINC | MINC | TEIE | EN, 4, 0x30000000, RAM)          # unmapped source
    settle(chip)
    assert flags(chip, DMA2, 1) == TEIF and rd(chip, sreg(DMA2, 1, CR)) & EN == 0
    assert any("bus error at 0x30000000" in w for w in warns(chip))
    wr(chip, DMA2 + LIFCR, TEIF << 6)
    # the CCM data RAM is CPU-only (RM0090 2.3): transfer error
    # (FIFO mode, 4 bytes below the 1/2 threshold: HTIF at NDTR 2, the fault at the final flush)
    setup(chip, DMA2, 1, M2M | PINC | MINC | EN, 4, RAM, 0x10000000)
    settle(chip)
    assert flags(chip, DMA2, 1) == TEIF | HTIF and rd(chip, sreg(DMA2, 1, CR)) & EN == 0
    assert any("0x10000000 (not reachable by the DMA" in w for w in warns(chip))


def test_fifo_config_error_feif(desc):
    chip = spin_chip(desc)
    # MSIZE word, MBURST INCR4, FTH 1/2: forbidden (Table 48) -> FEIF, stream stays disabled
    setup(chip, DMA2, 0, M2M | PINC | MINC | psize(4) | msize(4) | 1 << 23 | EN, 4, RAM, RAM + 0x100,
          fcr=0x4 | 1 | 0x80)
    assert flags(chip, DMA2, 0) == FEIF and rd(chip, sreg(DMA2, 0, CR)) & EN == 0
    assert asserted(chip, 56)                                         # FEIE


def test_protected_registers_while_enabled(desc):
    chip = spin_chip(desc)
    dma = chip.peripherals["DMA2"]
    setup(chip, DMA2, 7, M2P | MINC | chsel(4) | psize(1) | EN, 5, USART1 + 4, RAM)
    wr(chip, sreg(DMA2, 7, NDTR), 99)
    wr(chip, sreg(DMA2, 7, PAR), 0x1234)
    wr(chip, sreg(DMA2, 7, M0AR), 0x5678)
    wr(chip, sreg(DMA2, 7, CR), M2M | chsel(1) | TCIE | EN)             # only EN/IE bits change
    assert rd(chip, sreg(DMA2, 7, NDTR)) == 5 and rd(chip, sreg(DMA2, 7, PAR)) == USART1 + 4
    assert rd(chip, sreg(DMA2, 7, M0AR)) == RAM
    assert rd(chip, sreg(DMA2, 7, CR)) == M2P | MINC | chsel(4) | TCIE | EN
    assert dma.stream_request(7) == ("USART1_TX",)
    # clearing EN: stops at once, TCIF, NDTR keeps the remaining count
    wr(chip, sreg(DMA2, 7, CR), M2P | MINC | chsel(4))
    assert rd(chip, sreg(DMA2, 7, CR)) & EN == 0 and flags(chip, DMA2, 7) == TCIF
    assert rd(chip, sreg(DMA2, 7, NDTR)) == 5


# ------------------------------------------------------------------------------------------------ peripheral requests
def _usart(chip, base=USART1):
    wr(chip, base + 0x08, 139)
    wr(chip, base + 0x0C, 1 << 13 | 1 << 3 | 1 << 2)                   # UE TE RE


def test_usart1_tx_dma_one_byte_per_txe(desc):
    chip = spin_chip(desc)
    u = chip.peripherals["USART1"]
    _usart(chip)
    chip.run_for(u.char_ns())                                          # TE: idle preamble (RM0090 30.3.2)
    wr(chip, USART1, 0x3FF & ~0x40)                                    # clear TC by writing 0 (multibuffer)
    chip.bus.write_bytes(RAM, b"DMA!")
    setup(chip, DMA2, 7, M2P | MINC | chsel(4) | TCIE | EN, 4, USART1 + 4, RAM)
    settle(chip)
    assert bytes(u.output) == b""                                     # no request yet (CR3.DMAT = 0)
    wr(chip, USART1 + 0x14, 1 << 7)                                    # CR3.DMAT
    settle(chip)
    # one item per TXE: 'D' goes straight to the shift register (TXE stays 1), 'M' waits in TDR (RM0090 30.3.2)
    assert bytes(u.output) == b"D" and rd(chip, sreg(DMA2, 7, NDTR)) == 2
    chip.run_for(u.char_ns() + 500)
    assert bytes(u.output) == b"DM"
    chip.run_for(2 * u.char_ns() + 200)
    assert bytes(u.output) == b"DMA!"
    assert flags(chip, DMA2, 7) == TCIF | HTIF and rd(chip, sreg(DMA2, 7, CR)) & EN == 0
    tx = [e.t_ns for e in chip.log if e.kind == "uart" and e.fields.get("dir") == "tx"]
    gaps = [b - a for a, b in zip(tx, tx[1:])]
    assert all(g == pytest.approx(u.char_ns(), abs=200) for g in gaps)   # paced at the character time
    assert not rd(chip, USART1) & 0x40                                 # TC: last byte still shifting
    chip.run_for(u.char_ns())
    assert rd(chip, USART1) & 0xC0 == 0xC0


def test_usart1_rx_dma_and_stream_enabled_after_request(desc):
    chip = spin_chip(desc)
    u = chip.peripherals["USART1"]
    wr(chip, GPIOA + 0x24, 7 << 8)
    wr(chip, GPIOA + 0x00, rd(chip, GPIOA) | 2 << 20)                  # PA10 = USART1_RX
    _usart(chip)
    wr(chip, USART1 + 0x14, 1 << 6)                                    # CR3.DMAR
    u.feed(b"x")
    chip.run_for(u.char_ns() + 500)
    assert rd(chip, USART1) & 0x20                                    # RXNE, request pending, no stream yet
    # stream 5 channel 4 = USART1_RX (stream 2 would work too); enabled later: picks up the pending request
    setup(chip, DMA2, 5, P2M | MINC | chsel(4) | EN, 4, USART1 + 4, RAM)
    settle(chip)
    assert not rd(chip, USART1) & 0x20 and rd(chip, RAM, 1) == ord("x")
    u.feed(b"yzw")
    chip.run_for(4 * u.char_ns())
    assert chip.bus.read_bytes(RAM, 4) == b"xyzw"
    assert flags(chip, DMA2, 5) == TCIF | HTIF and not u.rx_dropped


def test_usart2_dma1_circular_rx_with_half_and_full_irqs(desc):
    chip = spin_chip(desc)
    u = chip.peripherals["USART2"]
    gpioa = chip.peripherals["GPIOA"]
    wr(chip, GPIOA + 0x20, 7 << 12)                                    # AFRL: PA3 AF7 = USART2_RX
    wr(chip, GPIOA + 0x00, rd(chip, GPIOA) | 2 << 6)
    assert gpioa is not None
    _usart(chip, USART2)
    wr(chip, USART2 + 0x14, 1 << 6)
    setup(chip, DMA1, 5, P2M | MINC | CIRC | chsel(4) | HTIE | TCIE | EN, 4, USART2 + 4, RAM)
    u.feed(b"abcdef")
    chip.run_for(2 * u.char_ns() + 500)
    assert flags(chip, DMA1, 5) == HTIF and asserted(chip, 16)         # DMA1_Stream5
    wr(chip, DMA1 + HIFCR, HTIF << 6)
    chip.run_for(4 * u.char_ns())
    assert chip.bus.read_bytes(RAM, 4) == b"efcd"                      # wrapped around
    assert rd(chip, sreg(DMA1, 5, CR)) & EN and rd(chip, sreg(DMA1, 5, NDTR)) == 2
    assert flags(chip, DMA1, 5) & TCIF


def test_double_buffer_switches_ct_and_buffers(desc):
    chip = spin_chip(desc)
    dma = chip.peripherals["DMA2"]
    a, b = RAM, RAM + 0x40
    setup(chip, DMA2, 1, P2M | MINC | DBM | chsel(5) | psize(1) | EN, 2, 0x40011404, a, b)
    assert rd(chip, sreg(DMA2, 1, CR)) & CIRC                          # forced by DBM
    seq = iter(b"0123456")
    usart6 = chip.peripherals["USART6"]
    usart6._rx_byte = 0
    for i in range(5):
        usart6._rx_byte = next(seq)
        dma.request("USART6_RX", True)
        settle(chip)
        if i == 1:
            assert rd(chip, sreg(DMA2, 1, CR)) & CT                     # buffer 0 full -> M1AR
            # writing the buffer in use (M1AR while CT = 1) is a transfer error; M0AR may be updated
            wr(chip, sreg(DMA2, 1, M0AR), a + 0x80)
            assert rd(chip, sreg(DMA2, 1, M0AR)) == a + 0x80
            wr(chip, DMA2 + LIFCR, 0x3D << 6)
    assert chip.bus.read_bytes(a, 2) == b"01" and chip.bus.read_bytes(b, 2) == b"23"
    assert chip.bus.read_bytes(a + 0x80, 1) == b"4" and not rd(chip, sreg(DMA2, 1, CR)) & CT
    wr(chip, sreg(DMA2, 1, M0AR), 0)                                   # in use now (CT = 0): TEIF, disabled
    assert flags(chip, DMA2, 1) & TEIF and not rd(chip, sreg(DMA2, 1, CR)) & EN
    assert rd(chip, sreg(DMA2, 1, M0AR)) == a + 0x80


def test_pulse_request_is_acknowledged_once(desc):
    """A peripheral that only pulses its request (timer update) gets exactly one item per pulse."""
    chip = spin_chip(desc)
    chip.bus.write_bytes(RAM, struct.pack("<III", 100, 200, 300))
    # DMA1 stream 2 channel 5 = TIM3_CH4/TIM3_UP: memory -> a fixed "peripheral" word (RAM here), word size
    dst = RAM + 0x200
    setup(chip, DMA1, 2, M2P | MINC | chsel(5) | psize(4) | EN, 3, dst, RAM)
    for want in (100, 200):
        dma_request(chip, "TIM3_UP", True)
        settle(chip)
        assert rd(chip, dst) == want
    settle(chip)
    assert rd(chip, sreg(DMA1, 2, NDTR)) == 1                         # no extra item without a new pulse
    dma_request(chip, "TIM3_CH4", True)                                # the ORed request of the same line
    settle(chip)
    assert rd(chip, dst) == 300 and flags(chip, DMA1, 2) & TCIF


def test_p2m_fifo_threshold_holds_data_until_threshold(desc):
    chip = spin_chip(desc)
    u6 = chip.peripherals["USART6"]
    dma = chip.peripherals["DMA2"]
    # byte peripheral -> word memory, FTH 1/4 (4 bytes): memory written every 4 bytes, the rest at disable
    setup(chip, DMA2, 2, P2M | MINC | chsel(5) | psize(1) | msize(4) | EN, 16, 0x40011404, RAM, fcr=0x4 | 0)
    for i, c in enumerate(b"ABCDEF"):
        u6._rx_byte = c
        dma.request("USART6_RX", True)
        settle(chip)
        if i == 2:
            assert rd(chip, RAM) == 0 and rd(chip, sreg(DMA2, 2, FCR)) >> 3 & 7 == 0     # 3 bytes in the FIFO
    assert chip.bus.read_bytes(RAM, 4) == b"ABCD" and rd(chip, RAM + 4) == 0
    wr(chip, sreg(DMA2, 2, CR), P2M | MINC | chsel(5) | psize(1) | msize(4))           # disable: flush
    assert chip.bus.read_bytes(RAM, 6) == b"ABCDEF" and flags(chip, DMA2, 2) == TCIF
    assert rd(chip, sreg(DMA2, 2, NDTR)) == 10


def test_rcc_gate_warn_and_ahb1rstr_reset(desc):
    chip = spin_chip(desc)
    setup(chip, DMA2, 3, M2P | chsel(4) | EN, 5, USART1 + 4, RAM)
    dma_request(chip, "USART1_TX", False)
    wr(chip, RCC + 0x10, 1 << 22)                                      # AHB1RSTR DMA2RST
    wr(chip, RCC + 0x10, 0)
    assert rd(chip, sreg(DMA2, 3, CR)) == 0 and rd(chip, sreg(DMA2, 3, NDTR)) == 0
    wr(chip, RCC + 0x30, rd(chip, RCC + 0x30) & ~(1 << 22))
    wr(chip, sreg(DMA2, 3, NDTR), 1)
    assert any("DMA2 accessed while its clock is disabled" in w for w in warns(chip))


def test_chip_reset_deasserts_irq_and_cancels_service(desc):
    chip = spin_chip(desc)
    setup(chip, DMA2, 0, M2M | PINC | MINC | TCIE | EN, 4, RAM, RAM + 0x100)
    chip.peripherals["DMA2"].reset()
    settle(chip)
    assert rd(chip, RAM + 0x100) == 0 and flags(chip, DMA2, 0) == 0


# ------------------------------------------------------------------------------------------------ RM0090 Rev 22 checks
def test_flush_remainder_written_with_msize(desc):
    """RM0090 10.3.12 "FIFO flush": fewer bytes than MSIZE left in the FIFO are still written with the MSIZE width."""
    chip = spin_chip(desc)
    u6 = chip.peripherals["USART6"]
    dma = chip.peripherals["DMA2"]
    chip.bus.write_bytes(RAM, b"\xff" * 8)
    setup(chip, DMA2, 2, P2M | MINC | chsel(5) | psize(1) | msize(4) | EN, 16, 0x40011404, RAM, fcr=0x4 | 0)
    for c in b"ABCDEF":
        u6._rx_byte = c
        dma.request("USART6_RX", True)
        settle(chip)
    wr(chip, sreg(DMA2, 2, CR), P2M | MINC | chsel(5) | psize(1) | msize(4))           # disable: flush
    assert chip.bus.read_bytes(RAM, 8) == b"ABCDEF\x00\x00"                             # one word, not a halfword
    assert flags(chip, DMA2, 2) == TCIF and rd(chip, sreg(DMA2, 2, NDTR)) == 10


def test_ndtr_reloaded_on_reenable(desc):
    """RM0090 10.5.6: after a normal-mode transfer NDTR stays 0 and is reloaded with the programmed value by EN."""
    chip = spin_chip(desc)
    src, dst = RAM, RAM + 0x100
    chip.bus.write_bytes(src, bytes(range(1, 17)))
    setup(chip, DMA2, 0, M2M | PINC | MINC | psize(4) | msize(4) | EN, 4, src, dst)
    settle(chip)
    assert rd(chip, sreg(DMA2, 0, NDTR)) == 0 and chip.bus.read_bytes(dst, 16) == bytes(range(1, 17))
    wr(chip, DMA2 + LIFCR, 0x3D)
    chip.bus.write_bytes(dst, bytes(16))
    wr(chip, sreg(DMA2, 0, CR), M2M | PINC | MINC | psize(4) | msize(4) | EN)
    assert rd(chip, sreg(DMA2, 0, NDTR)) == 4
    settle(chip)
    assert chip.bus.read_bytes(dst, 16) == bytes(range(1, 17)) and flags(chip, DMA2, 0) & TCIF


def test_pfctrl_forced_off_in_m2m_and_pincos_with_pburst(desc):
    """RM0090 10.5.5: PFCTRL forced to 0 in M2M; PINCOS forced low when PBURST != 00; CIRC forced 0 with PFCTRL."""
    chip = spin_chip(desc)
    setup(chip, DMA2, 0, M2M | PINC | MINC | psize(4) | msize(4) | 1 << 5 | PINCOS | 1 << 21 | EN, 4, RAM,
          RAM + 0x100, fcr=0x7)
    cr = rd(chip, sreg(DMA2, 0, CR))
    assert not cr & 1 << 5 and not cr & PINCOS
    settle(chip)
    assert rd(chip, sreg(DMA2, 0, NDTR)) == 0                        # DMA flow control: NDTR used
    setup(chip, DMA2, 2, P2M | MINC | chsel(5) | psize(1) | msize(1) | 1 << 5 | CIRC | EN, 3, 0x40011404, RAM)
    assert rd(chip, sreg(DMA2, 2, NDTR)) == 0xFFFF and not rd(chip, sreg(DMA2, 2, CR)) & CIRC
