"""Regression tests for the RM0008 verification of the STM32F1 system models (RCC, GPIO/AFIO, EXTI, PWR, BKP, RTC,
FLASH, DBGMCU) on the STM32F103T6.  Each test names the RM0008 Rev 21 section whose behaviour it checks: a
discrepancy found and fixed during the verification (or a behaviour that was missing)."""
from __future__ import annotations

import copy
import struct
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "chips" / "stm32f103t6.yaml"
FLASH = 0x08000000
RCC, FLASH_IF, PWR, BKP, RTC = 0x40021000, 0x40022000, 0x40007000, 0x40006C00, 0x40002800
AFIO, EXTI, GPIOA, GPIOB, GPIOD, DBG = 0x40010000, 0x40010400, 0x40010800, 0x40010C00, 0x40011400, 0xE0042000
CR, CFGR, CIR, APB1ENR, APB2ENR, BDCR, CSR = 0x00, 0x04, 0x08, 0x1C, 0x18, 0x20, 0x24


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def with_params(desc, block: str, **params):
    d = copy.deepcopy(desc)
    for p in d.peripherals:
        if p.name == block:
            p.params = dict(p.params or {}, **params)
    return d


def spin_chip(desc, **kw) -> Chip:
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20002800, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc, **kw)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    return chip


def rd(chip, a):
    return chip.bus.read(a, 4)


def wr(chip, a, v):
    chip.bus.write(a, 4, v)


def warns(chip):
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def asserted(chip, irq):
    return (16 + irq) in chip.arch.asserted


def backup_on(chip):
    wr(chip, RCC + APB1ENR, (1 << 27) | (1 << 28))
    wr(chip, PWR, 1 << 8)


class FakePin:
    """Tamper pin stand-in (PC13 is not bonded on the VFQFPN36)."""

    def __init__(self, level="L"):
        self.lvl = level

    def level(self):
        return self.lvl


# ------------------------------------------------------------------ RCC (RM0008 ch. 7)
def test_rcc_cir_ready_flag_needs_rdyie(desc):
    """RM0008 7.3.3 RCC_CIR: HSERDYF is set when the HSE becomes stable *and* HSERDYIE is set."""
    chip = spin_chip(desc)
    wr(chip, RCC + CR, rd(chip, RCC) | (1 << 16))
    assert rd(chip, RCC + CR) & (1 << 17) and not rd(chip, RCC + CIR) & (1 << 3)


def test_rcc_css_failure_goes_to_nmi(desc):
    """RM0008 7.2.7 CSS: an HSE failure with CSSON clears HSEON, sets CSSF, pends the NMI (not the RCC vector) and
    switches SYSCLK on HSE back to HSI; CSSC clears CSSF."""
    chip = spin_chip(desc)
    r = chip.peripherals["RCC"]
    wr(chip, RCC + CIR, 0x1F00)                                   # all ready interrupts enabled
    wr(chip, RCC + CR, rd(chip, RCC) | (1 << 16) | (1 << 19))     # HSEON, CSSON
    wr(chip, RCC + CIR, 0x1F00 | (0x1F << 16))                    # clear the ready flags
    wr(chip, RCC + CFGR, 1)                                       # SYSCLK = HSE
    assert (rd(chip, RCC + CFGR) >> 2) & 3 == 1
    assert r.hse_failure()
    assert rd(chip, RCC + CIR) & 0x80 and not asserted(chip, 5)   # CSSF, RCC vector not asserted
    assert 2 in chip.arch.pending                                 # NMI pending
    assert not rd(chip, RCC + CR) & (3 << 16) and (rd(chip, RCC + CFGR) & 0xF) == 0 and chip.clock.hz == 8e6
    wr(chip, RCC + CIR, 0x1F00 | (1 << 23))                       # CSSC
    assert not rd(chip, RCC + CIR) & 0x80


def test_rcc_selected_source_cannot_be_stopped_and_switch_waits(desc):
    """RM0008 7.3.1 PLLON "cannot be reset if the PLL clock is ... selected to become the system clock" and 7.2.6 (the
    switch occurs when the target source is ready), with a PLL lock time."""
    chip = spin_chip(desc)
    chip.peripherals["RCC"].startup_ns["PLL"] = 200e3
    wr(chip, FLASH_IF, 0x32)
    wr(chip, RCC + CFGR, 14 << 18)                                # HSI/2 x 16 = 64 MHz (PPRE1 warn is fine)
    wr(chip, RCC + CR, rd(chip, RCC) | (1 << 24))
    assert not rd(chip, RCC + CR) & (1 << 25)                     # not locked yet
    wr(chip, RCC + CFGR, rd(chip, RCC + CFGR) | 2 | (4 << 8))     # SW = PLL: pending
    assert (rd(chip, RCC + CFGR) >> 2) & 3 == 0 and chip.clock.hz == 8e6
    wr(chip, RCC + CR, rd(chip, RCC) & ~(1 << 24))                # PLLON cannot be cleared: selected
    assert rd(chip, RCC + CR) & (1 << 24)
    chip.run_for(250e3)
    assert rd(chip, RCC + CR) & (1 << 25) and (rd(chip, RCC + CFGR) >> 2) & 3 == 2 and chip.clock.hz == 64e6


def test_rcc_startup_delay_parameter(desc):
    """RM0008 7.2.1 (HSERDY set by hardware once the oscillator is stable): `hse_startup_ns` delays HSERDY."""
    chip = spin_chip(with_params(desc, "RCC", hse_startup_ns=2e6))
    wr(chip, RCC + CR, rd(chip, RCC) | (1 << 16))
    assert not rd(chip, RCC + CR) & (1 << 17)
    chip.run_for(1.9e6)
    assert not rd(chip, RCC + CR) & (1 << 17)
    chip.run_for(0.2e6)
    assert rd(chip, RCC + CR) & (1 << 17)


def test_rcc_usbpre_locked_by_usb_clock_not_pll(desc):
    """RM0008 7.3.2 USBPRE: "can't be reset if the USB clock is enabled" (it is not one of the PLL-locked fields)."""
    chip = spin_chip(desc)
    wr(chip, RCC + CFGR, 7 << 18)
    wr(chip, RCC + CR, rd(chip, RCC) | (1 << 24))
    wr(chip, RCC + CFGR, rd(chip, RCC + CFGR) | (1 << 22))        # PLL on: USBPRE still writable
    assert rd(chip, RCC + CFGR) & (1 << 22)
    wr(chip, RCC + APB1ENR, 1 << 23)                              # USBEN
    wr(chip, RCC + CFGR, rd(chip, RCC + CFGR) & ~(1 << 22))
    assert rd(chip, RCC + CFGR) & (1 << 22)


def test_rcc_hsebyp_with_hseon_in_one_write(desc):
    """RM0008 7.3.1 HSEBYP "can be written only if the HSE oscillator is disabled": it is, before this write."""
    chip = spin_chip(desc)
    wr(chip, RCC + CR, rd(chip, RCC) | (1 << 16) | (1 << 18))
    assert rd(chip, RCC + CR) & (1 << 18)
    wr(chip, RCC + CR, rd(chip, RCC) & ~(1 << 18))                # HSE on: HSEBYP frozen
    assert rd(chip, RCC + CR) & (1 << 18)


def test_rcc_bdcr_bdrst_not_protected_and_holds_reset(desc):
    """RM0008 7.3.9: only LSEON/LSEBYP/RTCSEL/RTCEN are write-protected by DBP; BDRST resets the backup domain and
    holds it in reset while set."""
    chip = spin_chip(desc)
    backup_on(chip)
    wr(chip, RCC + BDCR, 1 | (1 << 8) | (1 << 15))
    wr(chip, BKP + 0x04, 0x1234)
    wr(chip, PWR, 0)                                              # DBP = 0
    wr(chip, RCC + BDCR, rd(chip, RCC + BDCR) | (1 << 16))        # BDRST still works
    assert rd(chip, BKP + 0x04) == 0 and rd(chip, RCC + BDCR) == 1 << 16
    wr(chip, PWR, 1 << 8)
    wr(chip, RCC + BDCR, (1 << 16) | 1)                           # held in reset: LSEON does not stick
    assert rd(chip, RCC + BDCR) == 1 << 16
    wr(chip, RCC + BDCR, 0)
    wr(chip, RCC + BDCR, 1)
    assert rd(chip, RCC + BDCR) == 3                              # LSEON | LSERDY


def test_rcc_standby_exit_sets_pinrstf(desc):
    """RM0008 7.1.2: the Standby exit is a power reset whose pulse is output on NRST (Figure 7) -> PINRSTF."""
    chip = spin_chip(desc)
    wr(chip, RCC + CSR, 1 << 24)                                  # RMVF
    chip.arch.reset_requested = "standby_wakeup"
    chip.reset()
    assert rd(chip, RCC + CSR) & 0xFC000000 == 1 << 26


def test_rcc_xl_timer_gates_and_hsical(desc):
    """RM0008 7.3.7/7.3.8 (TIM9-11 APB2ENR bits 19-21, TIM12-14 APB1ENR bits 6-8) and 7.3.1 (CR reset 0x0000XX83)."""
    chip = spin_chip(with_params(desc, "RCC", hsical=0x5A))
    r = chip.peripherals["RCC"]
    assert r.gate_location("TIM12") == (0x1C, 1 << 6) and r.gate_location("TIM11") == (0x1A, 1 << 5)
    assert rd(chip, RCC + CR) & 0xFFFF == 0x5A83


# ------------------------------------------------------------------ GPIO / AFIO (RM0008 ch. 9, 31.4)
def lock(chip, port, mask):
    for v in (0x10000 | mask, mask, 0x10000 | mask):
        wr(chip, port + 0x18, v)


def test_gpio_lckr_read_sequence(desc):
    """RM0008 9.2.7 LOCK key sequence: write 1, write 0, write 1, read 0, read 1."""
    chip = spin_chip(desc)
    wr(chip, RCC + APB2ENR, 1 << 3)
    lock(chip, GPIOB, 0x0001)
    assert rd(chip, GPIOB + 0x18) == 0x0001                       # first read: LCKK = 0
    assert rd(chip, GPIOB + 0x18) == 0x10001                      # then 1: locked
    wr(chip, GPIOB, 0x44444443)
    assert rd(chip, GPIOB) & 0xF == 4


def test_gpio_lckr_read_in_sequence_aborts(desc):
    """RM0008 9.2.7: "Any error in the lock sequence will abort the lock" (a read between the writes)."""
    chip = spin_chip(desc)
    wr(chip, RCC + APB2ENR, 1 << 3)
    wr(chip, GPIOB + 0x18, 0x10001)
    rd(chip, GPIOB + 0x18)
    wr(chip, GPIOB + 0x18, 0x00001)
    wr(chip, GPIOB + 0x18, 0x10001)
    rd(chip, GPIOB + 0x18)
    assert not rd(chip, GPIOB + 0x18) & 0x10000
    wr(chip, GPIOB, 0x44444443)
    assert rd(chip, GPIOB) & 0xF == 3


def test_afio_swj_cfg_other_values_no_effect(desc):
    """RM0008 9.4.2 SWJ_CFG "Other combinations: no effect": HAL's AFIO_REMAP_ENABLE writes 111 with every remap and
    must not give the JTAG pins back."""
    chip = spin_chip(desc)
    wr(chip, RCC + APB2ENR, 1 | (1 << 2) | (1 << 3))
    wr(chip, AFIO + 0x04, 2 << 24)                                # JTAG off, SW on
    assert chip.pins.get("PB3").owner == "GPIO"
    wr(chip, AFIO + 0x04, (7 << 24) | 1)                          # SPI1_REMAP with SWJ_CFG = 111
    assert chip.pins.get("PB3").owner != "JTDO/TRACESWO" and chip.pins.get("PA15").owner == "GPIO"
    assert chip.pins.get("PA13").owner == "JTMS/SWDIO"


def test_afio_remap_tables_usart3_tim4_and_lines(desc):
    """RM0008 9.4.2 / Tables 43, 52: USART3_REMAP (bits 5:4) and TIM4_REMAP (bit 12); 9.4.7: MAPR2 exists on XL
    density only (reserved elsewhere)."""
    from chipem.periph.stm32f1.gpio import AFIO as AfioCls
    chip = spin_chip(desc)
    a = chip.peripherals["AFIO"]
    wr(chip, RCC + APB2ENR, 1)
    assert a.signal_pin("USART3_TX") == "PB10" and a.signal_pin("TIM4_CH1") == "PB6"
    wr(chip, AFIO + 0x04, (1 << 4) | (1 << 12))
    assert a.signal_pin("USART3_TX") == "PC10" and a.signal_pin("USART3_CTS") == "PB13"
    assert a.signal_pin("TIM4_CH4") == "PD15"
    wr(chip, AFIO + 0x04, 2 << 4)                                 # 10: not used
    assert a.signal_pin("USART3_TX") == ""
    wr(chip, AFIO + 0x1C, 0x7E0)                                  # MAPR2: reserved on low density
    assert rd(chip, AFIO + 0x1C) == 0
    xl = AfioCls("AFIO", AFIO, 0x400, params={"density": "xl"})
    xl.reset()
    xl.write(0x1C, 4, 0xFFFFFFFF)
    assert xl.read(0x1C, 4) == 0x7E0 and xl.signal_pin("TIM9_CH1") == "PE5" and xl.signal_pin("TIM14_CH1") == "PF9"


def test_jtdo_tristate_and_async_trace(desc):
    """RM0008 31.4.4 (JTDO tri-stated) and Table 37 note 1 / 31.16.3: with TRACE_IOEN and asynchronous trace PB3 is
    TRACESWO, even after SWJ_CFG = 010."""
    chip = spin_chip(desc)
    pb3 = chip.pins.get("PB3")
    assert pb3.direction == "in" and pb3.drive is None
    wr(chip, RCC + APB2ENR, 1)
    wr(chip, AFIO + 0x04, 2 << 24)
    wr(chip, DBG + 4, 1 << 5)                                     # TRACE_IOEN, TRACE_MODE = 00
    assert pb3.owner == "TRACESWO" and pb3.direction == "out"
    wr(chip, DBG + 4, 0)
    assert pb3.owner == "GPIO"


def test_pwr_ewup_forces_wkup_pull_down(desc):
    """RM0008 5.4.2 EWUP: "WKUP pin is used for wakeup ... and forced in input pull down configuration"."""
    chip = spin_chip(desc)
    wr(chip, RCC + APB1ENR, 1 << 28)
    wr(chip, RCC + APB2ENR, 1 << 2)
    wr(chip, GPIOA, (rd(chip, GPIOA) & ~0xF) | 0x8)               # PA0 input pull-up
    wr(chip, GPIOA + 0x0C, 1)
    assert chip.pins.get("PA0").pull == "up"
    wr(chip, PWR + 4, 1 << 8)
    pa0 = chip.pins.get("PA0")
    assert pa0.owner == "WKUP" and pa0.pull == "down" and pa0.level() == "L"
    wr(chip, PWR + 4, 0)
    assert chip.pins.get("PA0").pull == "up"


# ------------------------------------------------------------------ EXTI (RM0008 10.2)
def test_exti_pd0_pd1_on_osc_pins_have_no_exti(desc):
    """RM0008 9.3.2 note: with PD01_REMAP "PD0 and PD1 cannot be used for external interrupt/event generation on 36-,
    48- and 64-pin packages"."""
    chip = spin_chip(desc)
    wr(chip, RCC + APB2ENR, 1 | (1 << 5))
    wr(chip, AFIO + 0x04, 1 << 15)
    wr(chip, GPIOD, 0x44444448)                                   # PD0 input pull-up
    wr(chip, GPIOD + 0x0C, 1)
    wr(chip, AFIO + 0x08, 3)                                      # EXTI0 = PD0
    wr(chip, EXTI + 0x0C, 1)
    wr(chip, EXTI, 1)
    chip.pins.set_external("PD0", 0)
    assert rd(chip, EXTI + 0x14) == 0


def test_exti_line19_reserved_outside_cl(desc):
    """RM0008 10.3.1-10.3.6: bit 19 is used in connectivity line devices only (`lines` = 20) and reserved otherwise."""
    chip = spin_chip(desc)
    wr(chip, EXTI, 0xFFFFFFFF)
    assert rd(chip, EXTI) == 0x7FFFF
    chip2 = spin_chip(with_params(desc, "EXTI", lines=20))
    assert chip2.peripherals["EXTI"].regs["IMR"].ro_mask == 0xFFF00000


# ------------------------------------------------------------------ BKP (RM0008 ch. 6)
def tamper_chip(desc, level="L"):
    chip = spin_chip(desc)
    backup_on(chip)
    bkp = chip.peripherals["BKP"]
    bkp._tamper_pin = FakePin(level)
    return chip, bkp


def test_bkp_tamper_edge_and_tef_holds_data_registers(desc):
    """RM0008 6.3.1 (event = edge of pin AND TPE: an already active pin gives an event when TPE is set) and 6.4.4 TEF
    note (DRx held in reset while TEF = 1: writes are not stored)."""
    chip, bkp = tamper_chip(desc, level="H")
    wr(chip, BKP + 0x04, 0xBEEF)
    wr(chip, BKP + 0x34, 1 << 2)                                  # TPIE
    wr(chip, BKP + 0x30, 1)                                       # TPE, TPAL = 0, pin already high
    assert bkp.tamper_events == 1 and rd(chip, BKP + 0x34) & 0x300 == 0x300 and asserted(chip, 2)
    assert rd(chip, BKP + 0x04) == 0
    wr(chip, BKP + 0x04, 0x1111)
    assert rd(chip, BKP + 0x04) == 0                              # held in reset while TEF = 1
    wr(chip, BKP + 0x34, (1 << 2) | 3)                            # CTE | CTI
    assert rd(chip, BKP + 0x34) == 1 << 2 and not asserted(chip, 2)
    bkp._tamper_check()                                           # pin still high: no new edge
    assert bkp.tamper_events == 1
    wr(chip, BKP + 0x04, 0x2222)
    assert rd(chip, BKP + 0x04) == 0x2222


def test_bkp_tif_cleared_with_tpie_and_by_system_reset(desc):
    """RM0008 6.4.4: TIF "is also cleared if the TPIE bit is reset"; TPIE/TIF are reset by a system reset; reserved
    CSR bits read 0."""
    chip, bkp = tamper_chip(desc)
    wr(chip, BKP + 0x34, (1 << 2) | 0xF8)
    assert rd(chip, BKP + 0x34) == 1 << 2                         # bits 7:3 reserved
    wr(chip, BKP + 0x30, 1)
    bkp._tamper_pin.lvl = "H"
    bkp._tamper_check()
    assert rd(chip, BKP + 0x34) & (1 << 9)
    wr(chip, BKP + 0x34, 0)                                       # TPIE = 0 clears TIF
    assert not rd(chip, BKP + 0x34) & (1 << 9) and rd(chip, BKP + 0x34) & (1 << 8)
    wr(chip, BKP + 0x34, 1 << 2)
    chip.reset()
    assert rd(chip, BKP + 0x34) == 1 << 8                         # TPIE reset, TEF (backup domain) kept
    assert rd(chip, BKP + 0x30) == 1                              # CR kept


# ------------------------------------------------------------------ RTC (RM0008 ch. 18)
def rtc_cfg(chip, prl, cnt=0, alr=None):
    backup_on(chip)
    wr(chip, RCC + BDCR, 1 | (1 << 8) | (1 << 15))
    chip.run_for(100e3)
    wr(chip, RTC + 0x04, 0x10)
    wr(chip, RTC + 0x08, prl >> 16)
    wr(chip, RTC + 0x0C, prl & 0xFFFF)
    wr(chip, RTC + 0x18, cnt >> 16)
    wr(chip, RTC + 0x1C, cnt & 0xFFFF)
    if alr is not None:
        wr(chip, RTC + 0x20, alr >> 16)
        wr(chip, RTC + 0x24, alr & 0xFFFF)
    wr(chip, RTC + 0x04, 0)
    return chip.now_ns()


def first_info(chip, text):
    return [e.t_ns for e in chip.log if e.kind == "info" and text in e.fields.get("msg", "")]


def test_rtc_alarm_flag_at_end_of_alarm_second(desc):
    """RM0008 18.3.5 / Figure 180: ALRF is asserted on the last RTCCLK cycle before the counter reaches ALR + 1."""
    chip = spin_chip(desc)
    t0 = rtc_cfg(chip, prl=3, alr=4)                              # TR_CLK = RTCCLK / 4
    period = 1e9 / 32768
    chip.run_for(4 * 4 * period + 1e3)                            # CNT = 4 now
    assert rd(chip, RTC + 0x1C) == 4 and not rd(chip, RTC + 0x04) & 2
    chip.run_for(4 * period)
    assert rd(chip, RTC + 0x04) & 2
    t = first_info(chip, "RTC alarm")[0]
    assert abs(t - (t0 + (5 * 4 - 1) * period)) < 0.1 * period


def test_rtc_second_and_overflow_flags_one_rtcclk_early(desc):
    """RM0008 18.3.5 / Figure 181: SECF on the RTC core clock cycle before the counter update; OWF on the last cycle
    before CNT reaches 0."""
    chip = spin_chip(desc)
    t0 = rtc_cfg(chip, prl=3, cnt=0xFFFFFFFE)
    period = 1e9 / 32768
    chip.run_for(3 * period + 0.5 * period)                       # DIV = 0, CNT still 0xFFFFFFFE
    assert rd(chip, RTC + 0x1C) == 0xFFFE and rd(chip, RTC + 0x04) & 1
    wr(chip, RTC + 0x04, 0)                                       # clear SECF (rc_w0)
    chip.run_for(4 * period)                                      # last cycle of CNT = 0xFFFFFFFF
    assert rd(chip, RTC + 0x18) == 0xFFFF and rd(chip, RTC + 0x04) & 4
    assert chip.now_ns() - t0 < 8 * period


def test_rtc_counter_write_reloads_prescaler(desc):
    """RM0008 18.4.5: a write to RTC_CNTH/CNTL "directly loads the corresponding programmable counter and reloads the
    RTC Prescaler" (18.4.4: DIV reloaded after any change of PRL or CNT)."""
    chip = spin_chip(desc)
    rtc_cfg(chip, prl=999)
    chip.run_for(10e6)
    chip.run_for(200e3)
    wr(chip, RTC + 0x04, 0x10)
    wr(chip, RTC + 0x1C, 7)
    wr(chip, RTC + 0x04, 0)
    assert rd(chip, RTC + 0x14) == 999 and rd(chip, RTC + 0x1C) == 7


# ------------------------------------------------------------------ FLASH / DBGMCU (RM0008 3.3.3, 31)
def test_flash_latency_follows_sysclk(desc):
    """RM0008 3.3.3 FLASH_ACR.LATENCY: the wait states are the ratio of the SYSCLK period to the access time (not
    HCLK); prefetch must stay on with an AHB prescaler; half-cycle only up to 8 MHz without prescaler."""
    chip = spin_chip(desc)
    wr(chip, RCC + CR, rd(chip, RCC) | (1 << 16))
    wr(chip, FLASH_IF, 0x31)                                      # LATENCY 1
    wr(chip, RCC + CFGR, (1 << 16) | (7 << 18) | (4 << 8) | (8 << 4))   # HSE x 9 = 72 MHz, HPRE /2: HCLK 36 MHz
    wr(chip, RCC + CR, rd(chip, RCC) | (1 << 24))
    wr(chip, RCC + CFGR, rd(chip, RCC + CFGR) | 2)
    assert chip.clock.hz == 36e6
    assert any("LATENCY = 1 is too low for SYSCLK 72.000 MHz" in m for m in warns(chip))
    wr(chip, FLASH_IF, 0x2 | 0x8)                                 # PRFTBE off, HLFCYA on
    w = warns(chip)
    assert any("PRFTBE = 0 with an AHB prescaler" in m for m in w) and any("HLFCYA" in m for m in w)


def test_flash_erase_needs_hsi(desc):
    """RM0008 3.3.3: "For write and erase operations on the Flash memory (write/erase), the internal RC oscillator
    (HSI) must be ON"."""
    chip = spin_chip(desc)
    wr(chip, RCC + CR, rd(chip, RCC) | (1 << 16))
    wr(chip, RCC + CFGR, 1)
    wr(chip, RCC + CR, rd(chip, RCC) & ~1)                        # HSI off (SYSCLK = HSE)
    assert not rd(chip, RCC + CR) & 2
    wr(chip, FLASH_IF + 4, 0x45670123)
    wr(chip, FLASH_IF + 4, 0xCDEF89AB)
    wr(chip, FLASH_IF + 0x10, 2)
    wr(chip, FLASH_IF + 0x14, 0x08007C00)
    wr(chip, FLASH_IF + 0x10, 2 | 0x40)
    assert any("HSI must be ON" in m for m in warns(chip))


def test_dbgmcu_cr_survives_system_reset_and_density(desc):
    """RM0008 31.16.3 DBGMCU_CR "POR Reset: 0x0000 0000 (not reset by system reset)"; 31.6.1 DEV_ID per density."""
    chip = spin_chip(desc)
    wr(chip, DBG + 4, 0x307)                                      # DBG_SLEEP/STOP/STANDBY, DBG_IWDG/WWDG_STOP
    chip.reset()
    assert rd(chip, DBG + 4) == 0x307 and rd(chip, DBG) == 0x10006412
    wr(chip, DBG + 4, 0x7E000000)                                 # TIM9-14 bits: reserved on low density
    assert rd(chip, DBG + 4) == 0
    hd = spin_chip(with_params(desc, "DBG", density="high", idcode_readable=False))
    assert rd(hd, DBG) == 0 and hd.peripherals["DBG"].idcode == 0x10036414


def test_t6_syst_calib_is_9000():
    """RM0008 10.1.1: "The SysTick calibration value is set to 9000" (TENMS, HCLK/8 = 9 MHz -> 1 ms); also inherited by
    the low-density package variants (one-level include of stm32f103t6.yaml)."""
    from chipem.chip import Chip
    from chipem.chipdesc import load_chip
    for part in ("stm32f103t6", "stm32f103c6"):
        chip = Chip(load_chip(f"chips/{part}.yaml"))
        assert chip.bus.read(0xE000E01C, 4) & 0x00FFFFFF == 9000
