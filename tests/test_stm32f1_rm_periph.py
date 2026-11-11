"""STM32F1 data-path models checked against RM0008 Rev 21: one regression test per discrepancy found (or assumption
settled) when verifying stm32f1.dma (ch. 13), stm32f1.adc (ch. 11), stm32f1.usb (ch. 23), stm32f1.can (ch. 24), the F1
differences of the reused stm32.tim / usart / spi / i2s / i2c, and the vector table (10.1.2).  Each docstring cites the
RM0008 section.  Parts the F103T6 lacks (DMA2, ADC3, TIM8, SPI2 with I2S) are added to a copy of its description."""
from __future__ import annotations

import copy
import struct
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import PeripheralDesc, load_chip
from chipem.firmware import FirmwareImage
from chipem.periph.stm32._util import dma_request
from chipem.periph.stm32f1.adc import TRIGGERS_ADC3, JTRIGGERS_ADC3
from chipem.usbbus import Transaction, UsbBus

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "chips" / "stm32f103t6.yaml"
FLASH = 0x08000000
RCC, AFIO = 0x40021000, 0x40010000
AHBENR, APB2ENR, APB1ENR = 0x14, 0x18, 0x1C
DMA1, DMA2 = 0x40020000, 0x40020400
ADC1, ADC2, ADC3 = 0x40012400, 0x40012800, 0x40013C00
TIM2, TIM3, TIM8 = 0x40000000, 0x40000400, 0x40013400
USART1, SPI1, SPI2, I2C1, CAN = 0x40013800, 0x40013000, 0x40003800, 0x40005400, 0x40006400
USB = 0x40005C00
SRC, DST = 0x20000400, 0x20000800

# DMA
EN, TCIE, HTIE, TEIE, DIR, CIRC, PINC, MINC, MEM2MEM = 1, 2, 4, 8, 16, 32, 64, 128, 1 << 14
G, TC, HT, TE = 1, 2, 4, 8
# ADC
SR, CR1, CR2, SMPR2, SQR1, SQR3, JSQR, JDR1, DR = 0x00, 0x04, 0x08, 0x10, 0x2C, 0x34, 0x38, 0x3C, 0x4C
AWD, EOC, JEOC, JSTRT, STRT = 1, 2, 4, 8, 16
SCAN, JAUTO, DISCEN = 1 << 8, 1 << 10, 1 << 11
ADON, CONT, CAL, DMA, ALIGN = 1, 2, 4, 1 << 8, 1 << 11
JEXTTRIG, EXTTRIG, JSWSTART, SWSTART, TSVREFE = 1 << 15, 1 << 20, 1 << 21, 1 << 22, 1 << 23
SW = EXTTRIG | 7 << 17
JSW = JEXTTRIG | 7 << 12
K = 2                                   # core cycles per ADCCLK (8 MHz HSI, PCLK2 / 2)
LAT, JLAT = 2 * K, 3 * K
CONV0 = 14 * K
# USB
CNTR, ISTR, FNR, DADDR, BTABLE = USB + 0x40, USB + 0x44, USB + 0x48, USB + 0x4C, USB + 0x50
CTR_RX, DTOG_RX, SETUP, CTR_TX, DTOG_TX = 0x8000, 0x4000, 0x0800, 0x0080, 0x0040


def rd(chip, addr, n=4):
    return chip.bus.read(addr, n)


def wr(chip, addr, v, n=4):
    chip.bus.write(addr, n, v)


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def code(v):
    return max(0, min(int(v / 3.3 * 4096), 4095))


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def big_desc(base_desc):
    """The F103T6 description plus the high-density blocks these tests need (DMA2, ADC3, TIM8, SPI2 with I2S on
    SYSCLK) and their vectors (RM0008 Table 63)."""
    d = copy.deepcopy(base_desc)
    d.peripherals += [
        PeripheralDesc("DMA2", "stm32f1.dma", DMA2, 0x400, {}),
        PeripheralDesc("ADC3", "stm32f1.adc", ADC3, 0x400, {}),
        PeripheralDesc("TIM8", "stm32.tim", TIM8, 0x400, {"reserved_zero": True}),
        PeripheralDesc("SPI2", "stm32.spi", SPI2, 0x400, {"i2s": True, "i2s_clock": "sysclk", "reserved_zero": True}),
    ]
    d.irqs.update({36: "SPI2", 47: "ADC3", 56: "DMA2_Channel1", 57: "DMA2_Channel2", 58: "DMA2_Channel3",
                   59: "DMA2_Channel4_5"})
    return d


def spin_chip(desc) -> Chip:
    c = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(c))
    img[0:8] = struct.pack("<II", 0x20002800, (FLASH + 0x200) | 1)
    img[0x200:] = c
    chip = Chip(desc)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    wr(chip, RCC + AHBENR, rd(chip, RCC + AHBENR) | 3)                            # DMA1, DMA2
    wr(chip, RCC + APB2ENR, 1 << 0 | 1 << 2 | 1 << 3 | 1 << 9 | 1 << 10 | 1 << 12 | 1 << 14 | 1 << 15)
    wr(chip, RCC + APB1ENR, 1 << 0 | 1 << 1 | 1 << 14 | 1 << 21 | 1 << 23 | 1 << 25)
    return chip


def ch(base, c, off):
    """CCR (0), CNDTR (4), CPAR (8), CMAR (12) of channel c."""
    return base + 0x08 + 0x14 * (c - 1) + off


def asserted(chip, n) -> bool:
    return (16 + n) in chip.arch.asserted


# ================================================================================================ DMA (ch. 13)
def test_dma_gif_is_a_stored_flag(desc):
    """RM0008 13.4.1/13.4.2: GIFx is set by hardware with TE/HT/TC and cleared only by writing CGIFx; CTCIFx clears
    TCIFx alone (settles the 'GIF is a stored flag' assumption)."""
    chip = spin_chip(desc)
    chip.bus.write(SRC, 4, 0x11223344)
    wr(chip, ch(DMA1, 1, 4), 1)
    wr(chip, ch(DMA1, 1, 8), SRC)
    wr(chip, ch(DMA1, 1, 12), DST)
    wr(chip, ch(DMA1, 1, 0), MEM2MEM | 2 << 8 | 2 << 10 | EN)
    chip.run_cycles(5)
    assert rd(chip, DMA1) & 0xF == G | TC
    wr(chip, DMA1 + 4, TC)                                   # CTCIF1
    assert rd(chip, DMA1) & 0xF == G                         # GIF stays
    wr(chip, DMA1 + 4, G)                                    # CGIF1
    assert rd(chip, DMA1) & 0xF == 0


def test_dma_address_low_bits_ignored(desc):
    """RM0008 13.4.5/13.4.6: with PSIZE/MSIZE = 16 bits PA[0]/MA[0] are ignored, with 32 bits PA[1:0]/MA[1:0]: the
    access is aligned to the data size."""
    chip = spin_chip(desc)
    chip.bus.write(SRC, 4, 0xA1B2C3D4)
    wr(chip, ch(DMA1, 2, 4), 1)
    wr(chip, ch(DMA1, 2, 8), SRC + 3)                        # 32-bit source at an odd address -> SRC
    wr(chip, ch(DMA1, 2, 12), DST + 1)                       # 16-bit destination -> DST
    wr(chip, ch(DMA1, 2, 0), MEM2MEM | 2 << 8 | 1 << 10 | EN)
    chip.run_cycles(5)
    assert rd(chip, DST, 2) == 0xC3D4 and rd(chip, DST + 2, 2) == 0


def test_dma_byte_write_to_apb_is_replicated(desc):
    """RM0008 13.3.4: an AHB byte/half-word DMA write to an APB register becomes a 32-bit write with the data on
    all byte lanes (0xB0 -> 0xB0B0B0B0); memory writes keep their size."""
    chip = spin_chip(desc)
    chip.bus.write(SRC, 4, 0x000000AB)
    wr(chip, ch(DMA1, 3, 4), 1)
    wr(chip, ch(DMA1, 3, 8), TIM3 + 0x28)                    # TIM3_PSC (16 bits)
    wr(chip, ch(DMA1, 3, 12), SRC)
    wr(chip, ch(DMA1, 3, 0), MEM2MEM | DIR | 0 << 8 | 0 << 10 | EN)     # 8-bit memory -> 8-bit "peripheral"
    chip.run_cycles(5)
    assert rd(chip, TIM3 + 0x28) == 0xABAB
    chip.bus.write(DST, 4, 0)
    wr(chip, ch(DMA1, 3, 0), 0)
    wr(chip, ch(DMA1, 3, 4), 1)
    wr(chip, ch(DMA1, 3, 8), DST + 1)                        # SRAM destination: a plain byte write
    wr(chip, ch(DMA1, 3, 0), MEM2MEM | DIR | EN)
    chip.run_cycles(5)
    assert rd(chip, DST) == 0x0000AB00


def test_dma2_request_table_and_shared_ch4_5_vector(desc):
    """RM0008 13.3.7 Table 79 (DMA2 requests), 13.3.6 note (high/XL density: DMA2 Channel4 and Channel5 share one
    vector), 13.4 note (no channel 6/7 on DMA2: their registers and ISR/IFCR bits are reserved)."""
    chip = spin_chip(big_desc(desc))
    d2 = chip.peripherals["DMA2"]
    assert d2.nch == 5 and d2.channel_of["SPI3_RX"] == 1 and d2.channel_of["ADC3"] == 5
    assert d2.channel_of["UART4_RX"] == 3 and d2.channel_of["SDIO"] == 4 and d2.channel_of["DAC2"] == 4
    assert not dma_request(chip, "TIM5_CH9")
    wr(chip, ch(DMA2, 6, 0), 0xFFFF)
    assert rd(chip, ch(DMA2, 6, 0)) == 0
    for c in (4, 5):
        chip.bus.write(SRC, 4, c)
        wr(chip, ch(DMA2, c, 4), 1)
        wr(chip, ch(DMA2, c, 8), SRC)
        wr(chip, ch(DMA2, c, 12), DST + 4 * c)
        wr(chip, ch(DMA2, c, 0), MEM2MEM | TCIE | EN)
        chip.run_cycles(5)
    assert asserted(chip, 59)
    wr(chip, DMA2 + 4, G << 12)                              # clear channel 4 only: channel 5 keeps the line
    assert asserted(chip, 59)
    wr(chip, DMA2 + 4, G << 16)
    assert not asserted(chip, 59)


def test_dma1_served_before_dma2(desc):
    """RM0008 13.3.2 note: in high-density / XL / connectivity line devices DMA1 has priority over DMA2."""
    chip = spin_chip(big_desc(desc))
    chip.bus.write(SRC, 4, 0x1111)
    chip.bus.write(SRC + 4, 4, 0x2222)
    for base, src in ((DMA2, SRC + 4), (DMA1, SRC)):         # DMA2 enabled first, both pending at the same time
        wr(chip, ch(base, 1, 4), 1)
        wr(chip, ch(base, 1, 8), src)
        wr(chip, ch(base, 1, 12), DST)
        wr(chip, ch(base, 1, 0), MEM2MEM | 2 << 8 | 2 << 10 | EN)
    chip.run_cycles(5)
    assert rd(chip, DST) == 0x2222                           # DMA1 moved first, DMA2 wrote last


# ================================================================================================ ADC (ch. 11)
def power(chip, base=ADC1, cr2=0):
    wr(chip, base + CR2, ADON | cr2)
    chip.run_cycles(20)


def test_adc_adon_rewrite_starts_whatever_exttrig(desc):
    """RM0008 11.3.1 / 11.12.3 ADON: "Conversion starts when this bit holds a value of 1 and a 1 is written to it"
    (no EXTTRIG condition; a write that changes another CR2 bit does not start)."""
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    power(chip, cr2=EXTTRIG | 4 << 17)                       # EXTTRIG = 1, EXTSEL = TIM3_TRGO
    wr(chip, ADC1 + CR2, ADON | EXTTRIG | 4 << 17)
    chip.run_cycles(LAT + CONV0 + 4)
    assert adc.conversions == 1


def test_adc_eoc_at_each_discontinuous_conversion(desc):
    """RM0008 11.3.10 (regular discontinuous mode example): "An EOC event is generated at each conversion"."""
    chip = spin_chip(desc)
    wr(chip, ADC1 + SQR1, 2 << 20)
    wr(chip, ADC1 + CR1, DISCEN | 2 << 13)                   # 3 per trigger
    power(chip, cr2=SW)
    wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
    seen = []
    t0 = chip.now_cycles()
    for k in range(3):
        chip.sched.at_cycles(t0 + LAT + (k + 1) * CONV0 + 1,
                             lambda: (seen.append(rd(chip, ADC1 + SR) & EOC), rd(chip, ADC1 + DR)))
    chip.run_cycles(LAT + 3 * CONV0 + 5)
    assert seen == [EOC, EOC, EOC]


def test_adc_eoc_with_jeoc_at_end_of_injected_group(desc):
    """RM0008 11.12.1 SR.EOC: "set by hardware at the end of a group channel conversion (regular or injected)"; JEOC at
    the end of all injected channels (settles the 'EOC also set at the end of an injected group' assumption)."""
    chip = spin_chip(desc)
    wr(chip, ADC1 + JSQR, 1 << 20)                           # 2 injected conversions
    wr(chip, ADC1 + CR1, SCAN)
    power(chip, cr2=JSW)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | JSW | JSWSTART)
    sr = []
    chip.sched.at_cycles(t0 + JLAT + CONV0 + 1, lambda: sr.append(rd(chip, ADC1 + SR)))
    chip.run_cycles(JLAT + 2 * CONV0 + 4)
    assert sr[0] & (EOC | JEOC) == 0
    assert rd(chip, ADC1 + SR) & (EOC | JEOC) == EOC | JEOC


def test_adc2_reserved_tsvrefe_and_dualmod(desc):
    """RM0008 11.12.3 TSVREFE ("present only in ADC1") and 11.12.2 DUALMOD ("reserved in ADC2 and ADC3"); ADC2
    channels 16/17 are wired to VSS."""
    chip = spin_chip(desc)
    wr(chip, ADC2 + CR2, TSVREFE)
    wr(chip, ADC2 + CR1, 6 << 16)
    assert rd(chip, ADC2 + CR2) & TSVREFE == 0 and rd(chip, ADC2 + CR1) == 0
    wr(chip, ADC1 + CR2, TSVREFE)
    assert rd(chip, ADC1 + CR2) & TSVREFE
    assert chip.peripherals["ADC2"].volts(17) == 0.0


def test_adc_calibration_needs_two_adcclk_not_tstab(desc):
    """RM0008 11.4: before CAL the ADC must have been powered (ADON = 1) for at least two ADC clock cycles (tSTAB is
    for conversions); tCAL = 83 ADCCLK (DS15060)."""
    chip = spin_chip(desc)
    wr(chip, ADC1 + CR2, ADON)
    chip.run_cycles(2 * K)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | CAL)
    end = t0 + 83 * K
    got = []
    for t in (end - 1, end + 1):
        chip.sched.at_cycles(t, lambda: got.append(rd(chip, ADC1 + CR2) & CAL))
    chip.run_cycles(83 * K + 4)
    assert got == [CAL, 0] and not any("2 ADCCLK" in w for w in warns(chip))


def test_adc_slow_interleaved_runs_every_28_adcclk(desc):
    """RM0008 11.9.4: slow interleaved: ADC2 starts at the trigger, ADC1 14 ADCCLK later, then a new ADC2 start every
    28 ADCCLK (continuous without CONT)."""
    chip = spin_chip(desc)
    a1, a2 = chip.peripherals["ADC1"], chip.peripherals["ADC2"]
    wr(chip, ADC1 + CR1, 8 << 16)
    power(chip, ADC1, SW)
    power(chip, ADC2, SW)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
    n = []
    for k in range(3):                                       # ADC2 ends at LAT + CONV0 + 28k ADCCLK
        chip.sched.at_cycles(t0 + LAT + CONV0 + 28 * K * k + 1, lambda: n.append((a2.conversions, a1.conversions)))
    chip.run_cycles(LAT + CONV0 + 28 * K * 2 + 2)
    assert n == [(1, 0), (2, 1), (3, 2)]


def test_adc_jauto_switch_delay(desc):
    """RM0008 11.3.9 auto-injection: with the ADC prescaler at 2 a delay of 2 ADC clock periods is inserted when
    switching from the regular to the injected sequence."""
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    wr(chip, ADC1 + CR1, JAUTO)
    power(chip, cr2=SW)
    t0 = chip.now_cycles()
    wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
    end = t0 + LAT + CONV0 + 2 * K + CONV0
    got = []
    for t in (end - 1, end + 1):
        chip.sched.at_cycles(t, lambda: got.append(adc.injected_conversions))
    chip.run_cycles(end - t0 + 4)
    assert got == [0, 1]


def test_adc3_trigger_tables_vss_channels_and_dma2(desc):
    """RM0008 11.7 Tables 69/70 (ADC3 triggers), 11.12.2/11.12.4 (ADC3 channels 9, 14-17 on VSS), 11.8 (ADC1 and ADC3
    have the DMA request; ADC3 is served by DMA2 channel 5, 13.3.7 Table 79), 11.11 (own vector)."""
    chip = spin_chip(big_desc(desc))
    a3 = chip.peripherals["ADC3"]
    assert a3.triggers is TRIGGERS_ADC3 and a3.jtriggers is JTRIGGERS_ADC3 and a3._irqn == 47
    a3.set_channel("IN9", 2.0)
    assert a3.volts(9) == 0.0
    a3.set_channel("IN3", 1.0)
    wr(chip, ADC3 + SQR3, 3)
    wr(chip, ch(DMA2, 5, 4), 1)
    wr(chip, ch(DMA2, 5, 8), ADC3 + DR)
    wr(chip, ch(DMA2, 5, 12), DST)
    wr(chip, ch(DMA2, 5, 0), 1 << 8 | 1 << 10 | EN)
    power(chip, ADC3, SW | DMA)
    wr(chip, ADC3 + CR2, ADON | SW | DMA | SWSTART)
    chip.run_cycles(LAT + CONV0 + 5)
    assert rd(chip, DST, 2) == code(1.0) and rd(chip, DMA2) & TC << 16


def test_adc_exti_trigger_or_tim8_trgo_remap(desc):
    """RM0008 11.7 Table 67 / 9.4.2 AFIO_MAPR ADC1_ETRGREG_REMAP (bit 18): EXTSEL = 110 is EXTI line 11, or TIM8_TRGO
    when the remap bit is set (high/XL density)."""
    chip = spin_chip(big_desc(desc))
    adc = chip.peripherals["ADC1"]
    power(chip, cr2=EXTTRIG | 6 << 17)
    wr(chip, TIM8 + 0x04, 1 << 4)                            # MMS = 001: TRGO = CNT_EN
    wr(chip, TIM8, 1)
    chip.run_cycles(LAT + CONV0 + 4)
    assert adc.conversions == 0 and any("EXTI line 11" in w for w in warns(chip))
    wr(chip, TIM8, 0)
    afio = chip.peripherals["AFIO"]
    afio.set("MAPR", afio.get("MAPR") | 1 << 18)
    wr(chip, TIM8, 1)
    chip.run_cycles(LAT + CONV0 + 4)
    assert adc.conversions == 1


def test_adcclk_above_14mhz_warns(desc):
    """RM0008 11.1: the ADC input clock "must not exceed 14 MHz"."""
    chip = spin_chip(desc)
    adc = chip.peripherals["ADC1"]
    adc.adc_hz = lambda: 18e6
    power(chip, cr2=SW)
    wr(chip, ADC1 + CR2, ADON | SW | SWSTART)
    assert any("14 MHz" in w for w in warns(chip))


# ================================================================================================ USB (ch. 23)
class Dev:
    def __init__(self, c):
        self.c = c
        self.usb = c.peripherals["USB"]
        self.bus = UsbBus("T")
        self.bus.vbus = True
        self.bus.attach(self.usb)

    def x(self, kind, ep, pid=None, data=b""):
        return Transaction(kind, 0, ep, pid, data, self.c.now_ns())

    def setup(self, data=bytes(8)):
        return self.usb.usb_setup(self.bus, self.x("SETUP", 0, "DATA0", data))

    def out(self, ep, data, pid="DATA0"):
        return self.usb.usb_out(self.bus, self.x("OUT", ep, pid, data))

    def inn(self, ep):
        t = self.x("IN", ep)
        res = self.usb.usb_in(self.bus, t)
        if isinstance(res, tuple):
            self.usb.usb_in_done(self.bus, t, True, self.c.now_ns())
        return res


def usb_chip(desc):
    c = Chip(desc, clock_hz=8e6)
    code_ = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code_))
    img[0:8] = struct.pack("<II", 0x20002800, (FLASH + 0x200) | 1)
    img[0x200:] = code_
    c.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    c.reset()
    c.peripherals["USB"].usb_clock_hz = lambda: 48e6            # USBCLK ok without touching the RCC
    wr(c, RCC + APB1ENR, rd(c, RCC + APB1ENR) | 1 << 23)
    d = Dev(c)
    wr(c, CNTR, 1)
    wr(c, CNTR, 0)
    wr(c, ISTR, 0)
    wr(c, CNTR, 0xBF00)
    pma = c.peripherals["USB_CAN_SRAM"]
    for a, v in ((0, 0x40), (2, 0), (4, 0x80), (6, 0x8400)):    # EP0: TX 0x40, RX 0x80 (64 bytes)
        pma.w16(a, v)
    wr(c, USB, 0x0200 | 0x3000 | 0x0020)                     # control, STAT_RX VALID, STAT_TX NAK
    wr(c, DADDR, 0x80)
    return c, d


def epr(n):
    return USB + 4 * n


def test_usb_reset_keeps_ctr_bits(desc):
    """RM0008 23.5.2 EPnR: an USB reset clears the endpoint registers "except the CTR_RX and CTR_TX bits, which are
    kept unchanged to avoid missing a correct packet notification immediately followed by an USB reset event"."""
    c, d = usb_chip(desc)
    assert d.setup() == "ACK"
    d.usb.usb_reset(d.bus, True, c.now_ns())
    assert rd(c, epr(0)) == CTR_RX and rd(c, DADDR) == 0


def test_usb_no_transactions_while_istr_reset(desc):
    """RM0008 23.5.1 ISTR.RESET: "Reception and transmission are disabled until the RESET bit is cleared"."""
    c, d = usb_chip(desc)
    d.usb._flag(0x0400)                                      # USB reset seen, RESET not yet cleared
    assert d.setup() is None
    wr(c, ISTR, 0xFFFF & ~0x0400)
    assert d.setup() == "ACK"


def test_usb_setup_discarded_while_ctr_rx_pending(desc):
    """RM0008 23.4.2 (control transfers): a SETUP to an endpoint whose CTR_RX is still set is discarded without any
    handshake."""
    c, d = usb_chip(desc)
    assert d.setup() == "ACK"
    assert d.setup(b"\x01" * 8) is None
    assert c.peripherals["USB_CAN_SRAM"].read_bytes(0x80, 8) == bytes(8)


def test_usb_setup_bit_frozen_while_ctr_rx(desc):
    """RM0008 23.5.2 SETUP: "kept frozen while CTR_RX bit is at 1"."""
    c, d = usb_chip(desc)
    assert d.setup() == "ACK"
    wr(c, epr(0), (rd(c, epr(0)) & 0x070F) | CTR_RX | CTR_TX | 0x1000)   # STAT_RX NAK -> VALID, CTR_RX kept
    assert d.out(0, b"", "DATA1") == "ACK"
    assert rd(c, epr(0)) & (CTR_RX | SETUP) == CTR_RX | SETUP


def test_usb_out_overrun_copies_up_to_buffer_end(desc):
    """RM0008 23.4.2: on a buffer overrun the data is copied "only up to the last available location", the USB answers
    STALL, CTR_RX is not set."""
    c, d = usb_chip(desc)
    pma = c.peripherals["USB_CAN_SRAM"]
    pma.w16(8 + 4, 0x100)
    pma.w16(8 + 6, 2 << 10)                                  # 4-byte buffer
    pma.write_bytes(0x104, b"zz")
    wr(c, epr(1), 0x0001 | 0x3000)                           # bulk, EA 1, STAT_RX VALID
    assert d.out(1, b"abcdef") == "STALL"
    assert pma.read_bytes(0x100, 6) == b"abcdzz" and not rd(c, epr(1)) & CTR_RX


def test_usb_ep_id_priority(desc):
    """RM0008 23.5.1 ISTR.EP_ID: isochronous and double-buffered bulk endpoints are reported first, then the others,
    lowest register first."""
    c, d = usb_chip(desc)
    usb = d.usb
    usb._s(4 * 1, CTR_TX | 0x0001)                           # EP1R: plain bulk, CTR_TX
    usb._s(4 * 3, CTR_RX | 0x0100 | 0x0003)                  # EP3R: double-buffered bulk, CTR_RX
    assert rd(c, ISTR) & 0x801F == 0x8000 | 0x10 | 3
    usb._s(4 * 3, 0x0103)
    assert rd(c, ISTR) & 0x801F == 0x8000 | 1


def test_usb_esof_lsof_and_susp_after_three_missed_sofs(desc):
    """RM0008 23.5.1 ESOF ("set when an SOF packet is expected but not received"; three consecutive ESOFs -> SUSP),
    FNR.LSOF (consecutive lost SOFs, cleared by an SOF), 23.4.5 (no traffic for 3 ms)."""
    c, d = usb_chip(desc)
    wr(c, ISTR, 0)
    d.usb.usb_sof(d.bus, 5, c.now_ns())
    wr(c, ISTR, 0)
    c.run_for(1.01e6)
    assert rd(c, ISTR) & 0x0900 == 0x0100 and (rd(c, FNR) >> 11) & 3 == 1
    c.run_for(2e6)
    assert rd(c, ISTR) & 0x0900 == 0x0900 and (rd(c, FNR) >> 11) & 3 == 3
    d.usb.usb_sof(d.bus, 6, c.now_ns())
    assert (rd(c, FNR) >> 11) & 3 == 0 and rd(c, FNR) & 0x7FF == 6


def test_usb_wkup_only_in_suspend_mode(desc):
    """RM0008 23.5.1 WKUP: set "when, during suspend mode (FSUSP = 1), activity is detected"; it clears LP_MODE."""
    c, d = usb_chip(desc)
    d.usb.usb_suspend(d.bus, c.now_ns())
    wr(c, ISTR, 0)
    d.usb.usb_resume(d.bus, c.now_ns())
    assert not rd(c, ISTR) & 0x1000                          # FSUSP = 0: not in suspend mode
    d.usb.usb_suspend(d.bus, c.now_ns())
    wr(c, CNTR, rd(c, CNTR) | 0x0C)
    d.usb.usb_resume(d.bus, c.now_ns())
    assert rd(c, ISTR) & 0x1000 and not rd(c, CNTR) & 0x04


def test_usb_resume_pulse_length_checked(desc):
    """RM0008 23.5.1 CNTR.RESUME: active for no less than 1 ms and no more than 15 ms; 23.4.5 note: only after
    FSUSP = 1."""
    c, d = usb_chip(desc)
    wr(c, CNTR, rd(c, CNTR) | 0x10)
    wr(c, CNTR, rd(c, CNTR) & ~0x10)
    w = warns(c)
    assert any("FSUSP = 0" in x for x in w) and any("1 to 15 ms" in x for x in w)


# ================================================================================================ bxCAN (ch. 24)
def test_can_14_banks_filter_bits_27_14_reserved(desc):
    """RM0008 24.9.4: FM1R/FS1R/FFA1R/FA1R bits 27:14 "are available in connectivity line devices only and are
    reserved otherwise"; FMR resets to 0x2A1C 0E01."""
    chip = spin_chip(desc)
    assert rd(chip, CAN + 0x200) == 0x2A1C0E01
    for off in (0x204, 0x20C, 0x214, 0x21C):
        wr(chip, CAN + off, 0xFFFFFFFF)
        assert rd(chip, CAN + off) == 0x3FFF
    assert rd(chip, CAN + 0x240 + 8 * 14) == 0                # F14R1 does not exist


def test_can_28_banks_parameter(desc):
    """RM0008 24.3.4 / 24.9.4 (connectivity line): 28 filter banks and a writable CAN2SB (`filter_banks: 28`)."""
    d = copy.deepcopy(desc)
    next(p for p in d.peripherals if p.name == "CAN").params["filter_banks"] = 28
    chip = spin_chip(d)
    wr(chip, CAN + 0x200, 0x2A1C0000 | 20 << 8 | 1)
    assert rd(chip, CAN + 0x200) >> 8 & 0x3F == 20
    wr(chip, CAN + 0x21C, 1 << 27)
    assert rd(chip, CAN + 0x21C) == 1 << 27


# ================================================================================================ reused F4 models
def test_f1_usart_has_no_over8_onebit(desc):
    """RM0008 27.6.4 USART_CR1 (bits 15:14 reserved: no OVER8, 16x oversampling only) and 27.6.6 USART_CR3 (bits 15:11
    reserved: no ONEBIT)."""
    chip = spin_chip(desc)
    wr(chip, USART1 + 0x0C, 0xFFFF)
    wr(chip, USART1 + 0x14, 0xFFFF)
    assert rd(chip, USART1 + 0x0C) == 0x3FFF and rd(chip, USART1 + 0x14) == 0x7FF
    assert not chip.peripherals["USART1"].over8()            # 16x oversampling only


def test_f1_spi_no_ti_mode_and_i2spr_reset(desc):
    """RM0008 25.5.2 SPI_CR2 (bit 4 reserved: no FRF/TI mode), 25.5.3 SPI_SR (no FRE), 25.5.9 SPI_I2SPR reset 0x0002."""
    chip = spin_chip(desc)
    wr(chip, SPI1 + 4, 0xFF)
    assert rd(chip, SPI1 + 4) == 0xE7
    assert rd(chip, SPI1 + 0x20) == 0x0002


def test_f1_absent_registers_read_zero(desc):
    """RM0008 26.6.10 register map (no I2C_FLTR at 0x24) and 15.4.19 (no TIM2_OR at 0x50): addresses read 0, writes are
    ignored (`reserved_zero`); TIM2-5 CCER has no CCxNP (15.4.9)."""
    chip = spin_chip(desc)
    for a in (I2C1 + 0x24, TIM2 + 0x50, DMA1 + 0x94, ADC1 + 0x50, USB + 0x54):
        wr(chip, a, 0xFFFFFFFF)
        assert rd(chip, a) == 0
    wr(chip, TIM2 + 0x20, 0xFFFF)
    assert rd(chip, TIM2 + 0x20) == 0x3333


def test_f1_i2s_clock_is_sysclk(desc):
    """RM0008 25.4.3: on high/XL density parts the I2SxCLK source is the system clock (`i2s_clock: sysclk`)."""
    chip = spin_chip(big_desc(desc))
    eng = chip.peripherals["SPI2"].i2s
    assert eng.i2sclk_hz() == pytest.approx(8e6)             # HSI after reset


# ================================================================================================ vector table (10.1.2)
RM_TABLE63_LOW = {0: "WWDG", 1: "PVD", 2: "TAMPER", 3: "RTC", 4: "FLASH", 5: "RCC", 6: "EXTI0", 7: "EXTI1",
                  8: "EXTI2", 9: "EXTI3", 10: "EXTI4", 11: "DMA1_Channel1", 12: "DMA1_Channel2", 13: "DMA1_Channel3",
                  14: "DMA1_Channel4", 15: "DMA1_Channel5", 16: "DMA1_Channel6", 17: "DMA1_Channel7", 18: "ADC1_2",
                  19: "USB_HP_CAN1_TX", 20: "USB_LP_CAN1_RX0", 21: "CAN1_RX1", 22: "CAN1_SCE", 23: "EXTI9_5",
                  24: "TIM1_BRK", 25: "TIM1_UP", 26: "TIM1_TRG_COM", 27: "TIM1_CC", 28: "TIM2", 29: "TIM3",
                  31: "I2C1_EV", 32: "I2C1_ER", 35: "SPI1", 37: "USART1", 38: "USART2", 40: "EXTI15_10",
                  41: "RTC_Alarm", 42: "USBWakeUp"}


def test_t6_vector_table_matches_rm0008_table63(desc):
    """RM0008 10.1.2 Table 63 ("other STM32F10xxx devices", CMSIS names): positions 0-42; TIM4 (30), I2C2 (33/34),
    SPI2 (36) and USART3 (39) do not exist on the low-density F103T6."""
    assert desc.irqs == RM_TABLE63_LOW
    assert desc.core.num_irqs == 43


def test_f1_i2c_dma_last_with_channel_dma(desc):
    """RM0008 26.6.2 I2C_CR2.LAST ("Next DMA EOT is the last transfer", NACK on the last byte) with the F1's
    channel-based DMA (13.4.4 CNDTR): the I2C reads the remaining count of DMA1 channel 7 (I2C1_RX, Table 78)."""
    chip = spin_chip(desc)
    i2c = chip.peripherals["I2C1"]
    assert i2c._dma_last() is False                          # no channel serving I2C1_RX
    wr(chip, ch(DMA1, 7, 4), 1)
    wr(chip, ch(DMA1, 7, 8), I2C1 + 0x10)
    wr(chip, ch(DMA1, 7, 12), DST)
    wr(chip, ch(DMA1, 7, 0), MINC | EN)
    assert i2c._dma_last() is True
    wr(chip, ch(DMA1, 7, 0), 0)
    wr(chip, ch(DMA1, 7, 4), 5)
    wr(chip, ch(DMA1, 7, 0), MINC | EN)
    assert i2c._dma_last() is False
