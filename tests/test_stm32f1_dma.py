"""stm32f1.dma (DMA1, RM0008 ch. 13 layout; model not verified against RM0008): request mapping (Table 78), channel
configuration, CNDTR / circular reload, data packing (Table 79), arbitration, flags and IFCR clears, IRQ lines, transfer
error, USART1 TX on channel 4 through the shared stm32 `dma_request` API.  Driven through the bus on the real
STM32F103T6 chip YAML with a spin-loop firmware."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage
from chipem.periph.stm32._util import dma_request
from chipem.periph.stm32f1.dma import CHANNEL_OF, REQUESTS

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "chips" / "stm32f103t6.yaml"
FLASH = 0x08000000
RCC, DMA1, USART1 = 0x40021000, 0x40020000, 0x40013800
AHBENR, APB2ENR = 0x14, 0x18
ISR, IFCR = 0x0, 0x4
SRC, DST, DST2 = 0x20000400, 0x20000800, 0x20000C00

EN, TCIE, HTIE, TEIE, DIR, CIRC, PINC, MINC, MEM2MEM = 1, 2, 4, 8, 16, 32, 64, 128, 1 << 14
G, TC, HT, TE = 1, 2, 4, 8


def psize(n):
    return {1: 0, 2: 1, 4: 2}[n] << 8


def msize(n):
    return {1: 0, 2: 1, 4: 2}[n] << 10


def pl(n):
    return n << 12


def creg(c, off):
    """CCR (0), CNDTR (4), CPAR (8), CMAR (12) of channel c = 1..7."""
    return DMA1 + 0x08 + 0x14 * (c - 1) + off


CCR, CNDTR, CPAR, CMAR = 0, 4, 8, 12


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def spin_chip(desc) -> Chip:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20002800, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    wr(chip, RCC + AHBENR, rd(chip, RCC + AHBENR) | 1)              # DMA1EN
    return chip


def rd(chip, addr, n=4):
    return chip.bus.read(addr, n)


def wr(chip, addr, v, n=4):
    chip.bus.write(addr, n, v)


def asserted(chip, irq: int) -> bool:
    return (16 + irq) in chip.arch.asserted


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def setup(chip, c, ccr, n, par, mar):
    wr(chip, creg(c, CNDTR), n)
    wr(chip, creg(c, CPAR), par)
    wr(chip, creg(c, CMAR), mar)
    wr(chip, creg(c, CCR), ccr)


def flags(chip, c):
    return rd(chip, DMA1 + ISR) >> (4 * (c - 1)) & 0xF


def settle(chip):
    chip.run_cycles(20)


# ------------------------------------------------------------------------------------------------ tables / API
def test_request_table_rm0008():
    assert REQUESTS[1] == ("ADC1", "TIM2_CH3", "TIM4_CH1")
    assert REQUESTS[2] == ("SPI1_RX", "USART3_TX", "TIM1_CH1", "TIM2_UP", "TIM3_CH3")
    assert REQUESTS[3] == ("SPI1_TX", "USART3_RX", "TIM1_CH2", "TIM3_CH4", "TIM3_UP")
    assert set(REQUESTS[4]) == {"SPI2_RX", "I2S2_RX", "USART1_TX", "I2C2_TX", "TIM1_CH4", "TIM1_TRIG", "TIM1_COM",
                                "TIM4_CH2"}
    assert set(REQUESTS[5]) == {"SPI2_TX", "I2S2_TX", "USART1_RX", "I2C2_RX", "TIM1_UP", "TIM2_CH1", "TIM4_CH3"}
    assert REQUESTS[6] == ("USART2_RX", "I2C1_TX", "TIM1_CH3", "TIM3_CH1", "TIM3_TRIG")
    assert REQUESTS[7] == ("USART2_TX", "I2C1_RX", "TIM2_CH2", "TIM2_CH4", "TIM4_UP")
    assert sum(len(v) for v in REQUESTS.values()) == len(CHANNEL_OF)       # every request on exactly one channel


def test_request_api_routing_and_irq_numbers(desc):
    chip = spin_chip(desc)
    d = chip.peripherals["DMA1"]
    assert d.model == "stm32f1.dma" and d.stm32_dma_api
    assert dma_request(chip, "USART1_TX", True) and d.request_active("USART1_TX")
    assert dma_request(chip, "USART1_TX", False) is True and not d.request_active("USART1_TX")
    assert dma_request(chip, "DAC1", True) is False and dma_request(chip, "SDIO", True) is False
    assert [d.chans[c].irq for c in range(1, 8)] == list(range(11, 18))


# ------------------------------------------------------------------------------------------------ transfers
def test_mem2mem_copy_flags_and_en_stays_set(desc):
    chip = spin_chip(desc)
    chip.bus.write_bytes(SRC, bytes(range(1, 17)))
    setup(chip, 3, MEM2MEM | PINC | MINC | psize(4) | msize(4) | TCIE | EN, 4, SRC, DST)
    assert rd(chip, DST) == 0                                      # never inside the MMIO access
    settle(chip)
    assert chip.bus.read_bytes(DST, 16) == bytes(range(1, 17))
    assert rd(chip, creg(3, CNDTR)) == 0 and flags(chip, 3) == G | TC | HT
    assert rd(chip, creg(3, CCR)) & EN                             # the F1 keeps EN after the last item
    assert rd(chip, creg(3, CPAR)) == SRC and rd(chip, creg(3, CMAR)) == DST     # internal pointers only
    assert asserted(chip, 13)
    wr(chip, DMA1 + IFCR, TC << 8)                                 # CTCIF3
    assert flags(chip, 3) == G | HT and not asserted(chip, 13)     # HTIE = 0: HT alone raises nothing
    wr(chip, DMA1 + IFCR, G << 8)                                  # CGIF3 clears everything
    assert flags(chip, 3) == 0 and rd(chip, DMA1 + IFCR) == 0


def test_cndtr_cpar_cmar_protected_while_enabled(desc):
    chip = spin_chip(desc)
    setup(chip, 2, MINC | EN, 5, SRC, DST)                         # request-driven, no request
    wr(chip, creg(2, CNDTR), 9)
    wr(chip, creg(2, CPAR), 0x20000100)
    wr(chip, creg(2, CMAR), 0x20000200)
    assert (rd(chip, creg(2, CNDTR)), rd(chip, creg(2, CPAR)), rd(chip, creg(2, CMAR))) == (5, SRC, DST)
    wr(chip, creg(2, CCR), MINC | CIRC | EN)                       # config bits while enabled: kept
    assert rd(chip, creg(2, CCR)) == MINC | EN and any("EN = 1" in w for w in warns(chip))
    wr(chip, creg(2, CCR), MINC | TCIE | EN)                       # interrupt enables are free
    assert rd(chip, creg(2, CCR)) == MINC | TCIE | EN
    wr(chip, creg(2, CCR), 0)
    wr(chip, creg(2, CNDTR), 0x12345)
    assert rd(chip, creg(2, CNDTR)) == 0x2345                      # 16-bit counter


def test_cndtr_zero_transfers_nothing(desc):
    chip = spin_chip(desc)
    wr(chip, SRC, 0xAABBCCDD)
    setup(chip, 1, MEM2MEM | psize(4) | msize(4) | EN, 0, SRC, DST)
    settle(chip)
    assert rd(chip, DST) == 0 and flags(chip, 1) == 0


@pytest.mark.parametrize("src,dst,expect", [
    (1, 1, [0x11, 0x22]), (1, 2, [0x0011, 0x0022]), (1, 4, [0x11, 0x22]),
    (2, 1, [0x11, 0x33]), (2, 2, [0x2211, 0x4433]), (2, 4, [0x2211, 0x4433]),
    (4, 1, [0x11, 0x55]), (4, 2, [0x2211, 0x6655]), (4, 4, [0x44332211, 0x88776655])])
def test_packing_rm0008_table79(desc, src, dst, expect):
    """Source read with its own width, destination written zero-extended / truncated; each pointer steps by its own
    size."""
    chip = spin_chip(desc)
    chip.bus.write_bytes(SRC, bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]))
    chip.bus.write_bytes(DST, b"\xEE" * 16)
    setup(chip, 1, MEM2MEM | PINC | MINC | psize(src) | msize(dst) | EN, 2, SRC, DST)
    settle(chip)
    got = [rd(chip, DST + i * dst, dst) for i in range(2)]
    assert got == expect
    assert rd(chip, DST + 2 * dst, 1) == 0xEE                      # nothing beyond the two items


def test_mem_to_peripheral_direction(desc):
    """DIR = 1: read CMAR (MSIZE), write CPAR (PSIZE)."""
    chip = spin_chip(desc)
    chip.bus.write_bytes(SRC, bytes([1, 2, 3, 4]))
    setup(chip, 7, MEM2MEM | DIR | MINC | psize(2) | msize(1) | EN, 4, DST, SRC)
    settle(chip)
    assert rd(chip, DST, 2) == 4                                    # PINC = 0: the last byte, zero-extended
    assert rd(chip, DST + 2, 2) == 0


def test_circular_request_driven_reload_and_half_transfer(desc):
    """Channel 2 (TIM2_UP) P2M circular, N = 4: one item per request, HT at 2 left, TC + reload at 0, the memory
    pointer restarts from CMAR."""
    chip = spin_chip(desc)
    d = chip.peripherals["DMA1"]
    setup(chip, 2, CIRC | MINC | psize(4) | msize(4) | HTIE | TCIE | EN, 4, SRC, DST)
    ndt, fl = [], []
    for k in range(6):
        wr(chip, SRC, 100 + k)
        assert d.request("TIM2_UP", True)
        settle(chip)
        assert not d.request_active("TIM2_UP")                     # acknowledged by the transfer
        ndt.append(rd(chip, creg(2, CNDTR)))
        fl.append(flags(chip, 2))
        if fl[-1] & TC:
            wr(chip, DMA1 + IFCR, G << 4)
    assert ndt == [3, 2, 1, 4, 3, 2]
    assert [f & (HT | TC) for f in fl] == [0, HT, HT, HT | TC, 0, HT]
    assert [rd(chip, DST + 4 * i) for i in range(4)] == [104, 105, 102, 103]
    assert rd(chip, creg(2, CCR)) & EN and asserted(chip, 12)
    settle(chip)
    assert rd(chip, creg(2, CNDTR)) == 2                           # no request: nothing moves


def test_request_before_enable_and_no_more_after_completion(desc):
    chip = spin_chip(desc)
    d = chip.peripherals["DMA1"]
    wr(chip, SRC, 0x5A)
    d.request("USART2_RX", True)                                    # channel 6, not enabled yet: remembered
    settle(chip)
    setup(chip, 6, MINC | EN, 1, SRC, DST)
    settle(chip)
    assert rd(chip, DST, 1) == 0x5A and rd(chip, creg(6, CNDTR)) == 0 and flags(chip, 6) == G | TC
    d.request("USART2_RX", True)
    settle(chip)
    assert d.request_active("USART2_RX") and rd(chip, DST + 1, 1) == 0     # CNDTR = 0: not served
    wr(chip, creg(6, CCR), 0)                                       # disable, reprogram, enable: served
    wr(chip, creg(6, CNDTR), 1)
    wr(chip, creg(6, CMAR), DST + 1)
    wr(chip, creg(6, CCR), MINC | EN)
    settle(chip)
    assert rd(chip, DST + 1, 1) == 0x5A and not d.request_active("USART2_RX")


def test_disable_mid_transfer_and_resume(desc):
    chip = spin_chip(desc)
    d = chip.peripherals["DMA1"]
    for k in range(4):
        wr(chip, SRC + 4 * k, 10 + k)
    setup(chip, 5, PINC | MINC | psize(4) | msize(4) | EN, 4, SRC, DST)
    d.request("USART1_RX", True)
    settle(chip)
    wr(chip, creg(5, CCR), PINC | MINC | psize(4) | msize(4))       # EN = 0: stops, no flag
    assert rd(chip, creg(5, CNDTR)) == 3 and flags(chip, 5) == 0
    d.request("USART1_RX", True)
    settle(chip)
    assert rd(chip, creg(5, CNDTR)) == 3                            # disabled: nothing moves
    wr(chip, creg(5, CCR), PINC | MINC | psize(4) | msize(4) | EN)  # pointers restart from CPAR/CMAR
    settle(chip)
    assert rd(chip, creg(5, CNDTR)) == 2 and rd(chip, DST) == 10


def test_arbitration_priority_then_channel_number(desc):
    """Both M2M channels write the same destination word: the one served last wins.  PL first (very high before
    low), then the lower channel number on equal PL; a higher-priority M2M channel completes before a lower moves."""
    chip = spin_chip(desc)
    wr(chip, SRC, 0x33333333)
    wr(chip, SRC + 4, 0x55555555)
    setup(chip, 3, MEM2MEM | psize(4) | msize(4) | pl(0) | EN, 3, SRC, DST)
    setup(chip, 5, MEM2MEM | psize(4) | msize(4) | pl(3) | EN, 3, SRC + 4, DST)
    settle(chip)
    assert rd(chip, DST) == 0x33333333                              # ch5 (PL 3) first, ch3 (PL 0) last
    chip2 = spin_chip(desc)
    wr(chip2, SRC, 0x22222222)
    wr(chip2, SRC + 4, 0x66666666)
    setup(chip2, 6, MEM2MEM | psize(4) | msize(4) | pl(2) | EN, 2, SRC + 4, DST)
    setup(chip2, 2, MEM2MEM | psize(4) | msize(4) | pl(2) | EN, 2, SRC, DST)
    settle(chip2)
    assert rd(chip2, DST) == 0x66666666                             # equal PL: ch2 before ch6


def test_request_priority_between_channels(desc):
    chip = spin_chip(desc)
    d = chip.peripherals["DMA1"]
    wr(chip, SRC, 0x11)
    wr(chip, SRC + 4, 0x77)
    setup(chip, 1, pl(1) | EN, 1, SRC, DST)                         # ADC1, medium
    setup(chip, 7, pl(2) | EN, 1, SRC + 4, DST)                     # USART2_TX, high
    d.request("ADC1", True)
    d.request("USART2_TX", True)
    settle(chip)
    assert rd(chip, DST, 1) == 0x11                                 # ch7 first, ch1 last


def test_transfer_error_disables_channel(desc):
    chip = spin_chip(desc)
    setup(chip, 4, MEM2MEM | psize(4) | msize(4) | TEIE | EN, 4, 0x60000000, DST)    # nothing mapped there
    settle(chip)
    assert flags(chip, 4) == G | TE and rd(chip, creg(4, CCR)) & EN == 0
    assert rd(chip, creg(4, CNDTR)) == 4 and asserted(chip, 14)
    assert any("bus error" in w for w in warns(chip))
    wr(chip, DMA1 + IFCR, TE << 12)                                 # CTEIF4: line drops, GIF stays
    assert flags(chip, 4) == G and not asserted(chip, 14)


def test_irq_line_per_enable_bit(desc):
    chip = spin_chip(desc)
    chip.bus.write_bytes(SRC, bytes(8))
    setup(chip, 1, MEM2MEM | PINC | MINC | HTIE | EN, 8, SRC, DST)
    settle(chip)
    assert flags(chip, 1) == G | HT | TC and asserted(chip, 11)
    wr(chip, DMA1 + IFCR, HT)
    assert not asserted(chip, 11)                                   # TC set but TCIE = 0
    wr(chip, creg(1, CCR), MEM2MEM | PINC | MINC | TCIE | EN)       # enabling TCIE raises the line
    assert asserted(chip, 11)


def test_gate_off_access_warns(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + AHBENR, rd(chip, RCC + AHBENR) & ~1)
    wr(chip, creg(1, CNDTR), 3)
    assert any("DMA1" in w and "clock is disabled" in w for w in warns(chip))


# ------------------------------------------------------------------------------------------------ real peripheral
def test_usart1_tx_dma_channel4(desc):
    chip = spin_chip(desc)
    u = chip.peripherals["USART1"]
    wr(chip, RCC + APB2ENR, rd(chip, RCC + APB2ENR) | 1 << 14 | 1 << 2)    # USART1EN, IOPAEN
    wr(chip, USART1 + 0x08, 69)                                     # 8 MHz / 69 = 115.9 kBd
    wr(chip, USART1 + 0x0C, 1 << 13 | 1 << 3)                       # UE TE
    chip.run_for(u.char_ns())
    chip.bus.write_bytes(SRC, b"F1dma")
    setup(chip, 4, DIR | MINC | TCIE | EN, 5, USART1 + 4, SRC)
    wr(chip, USART1 + 0x14, 1 << 7)                                 # CR3.DMAT
    chip.run_for(6 * u.char_ns())
    assert bytes(u.output) == b"F1dma"
    assert flags(chip, 4) & TC and asserted(chip, 14) and rd(chip, creg(4, CNDTR)) == 0
