"""STM32F4 low-power modes (stm32.pwr: Sleep / Stop / Standby, WFE event register; RM0090 5.3) and the USART's
flow control, break, parity, half-duplex and mute modes (stm32.usart; RM0090 ch. 30)."""
import struct

import pytest

from chipem.chip import Chip
from chipem.firmware import FirmwareImage
from engine_util import asm
from test_stm32 import FLASH, GPIOA, PWR, RCC, USART1, desc, rd, wr  # noqa: F401  (desc: fixture)

EXTI, RTC, IWDG = 0x40013C00, 0x40002800, 0x40003000
SCR, ISER0 = 0xE000ED10, 0xE000E100
AHB1ENR, APB1ENR, APB2ENR, CSR, BDCR, RCR_CR, RCR_CFGR = 0x30, 0x40, 0x44, 0x74, 0x70, 0x00, 0x08
RTC_CR, RTC_ISR, RTC_WUTR, RTC_WPR, RTC_BKP0R = 0x08, 0x0C, 0x14, 0x24, 0x50
WUTE, WUTIE, WUTF = 1 << 10, 1 << 14, 1 << 10
SLEEPDEEP, SEVONPEND = 4, 16
PDDS, CWUF, CSBF = 2, 4, 8
WUF, SBF, EWUP, BRE = 1, 2, 1 << 8, 1 << 9
RTC_WKUP_IRQ, EXTI0_IRQ, TIM6_IRQ = 3, 6, 54


def _chip(desc, code: str):
    """F405ZG at 16 MHz running `code` (Thumb) from 0x08000200 with PRIMASK set (interrupts wake WFI but are not
    taken, so no handlers are needed); r4 counts wake-ups in the test programs."""
    body = asm(code, FLASH + 0x200)
    img = bytearray(0x200 + len(body))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = body
    chip = Chip(desc, clock_hz=16e6)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    wr(chip, RCC + AHB1ENR, rd(chip, RCC + AHB1ENR) | 1)            # GPIOA
    wr(chip, RCC + APB1ENR, 1 << 28 | 1 << 4)                       # PWR, TIM6
    wr(chip, RCC + APB2ENR, 1 << 14 | 1 << 4)                       # SYSCFG, USART1
    return chip


LOOP = "cpsid i\nloop: wfi\nadds r4, #1\nb loop"


def _r4(chip):
    return chip.regs()["r4"]


def _rtc_wakeup(chip, period_s=1.0):
    wr(chip, PWR, rd(chip, PWR) | 1 << 8)                           # DBP
    wr(chip, RCC + BDCR, 1 | 1 << 8 | 1 << 15)                      # LSE, RTCSEL = LSE, RTCEN
    wr(chip, RTC + RTC_WPR, 0xCA)
    wr(chip, RTC + RTC_WPR, 0x53)
    wr(chip, RTC + RTC_WUTR, int(period_s * 2048) - 1)              # RTCCLK / 16 = 2048 Hz
    wr(chip, RTC + RTC_CR, WUTE | WUTIE)


def _pll_on(chip):
    """HSI 16 MHz / 8 x 168 / 2 = 168 MHz."""
    wr(chip, RCC + 0x04, 8 | 168 << 6)
    wr(chip, RCC + RCR_CR, rd(chip, RCC) | 1 << 16 | 1 << 24)       # HSEON, PLLON
    wr(chip, RCC + RCR_CFGR, 2 | 4 << 10 | 4 << 13)                  # SW = PLL, PPRE1/2 = /2
    assert (rd(chip, RCC + RCR_CFGR) >> 2) & 3 == 2


def _infos(chip, text):
    return [e for e in chip.log if e.kind == "info" and text in e.fields.get("msg", "")]


# ================================================================================================ Stop
def test_stop_mode_rtc_wakeup_clocks_and_hsi_on_exit(desc):
    chip = _chip(desc, LOOP)
    _pll_on(chip)
    assert chip.clock.hz == pytest.approx(168e6)
    _rtc_wakeup(chip)
    wr(chip, EXTI, 1 << 22)                                         # IMR22
    wr(chip, EXTI + 8, 1 << 22)                                     # RTSR22
    wr(chip, ISER0, 1 << RTC_WKUP_IRQ)
    wr(chip, SCR, SLEEPDEEP)                                        # PDDS = 0: Stop
    pwr = chip.peripherals["PWR"]
    t0 = chip.now_ns()
    chip.run_for(0.5e9)
    assert pwr.mode == "stop" and _r4(chip) == 0 and pwr.entries["stop"] == 1
    cr = rd(chip, RCC)
    assert not cr & (1 << 16 | 1 << 24) and cr & 1                  # HSE, PLL off, HSI on
    assert (rd(chip, RCC + RCR_CFGR) >> 2) & 3 == 0 and chip.clock.hz == pytest.approx(16e6 / 1)
    chip.run_for(t0 + 1.0e9 + 1e5 - chip.now_ns())
    assert _r4(chip) >= 1 and _infos(chip, "exit Stop mode")
    assert rd(chip, EXTI + 0x14) & 1 << 22                          # the wake-up line stays pending
    # with an EXTI line pending, a new Stop entry is ignored (RM0090 5.3.5)
    assert _infos(chip, "Stop mode entry ignored")


def test_stop_mode_only_exti_wakes_and_usart_freezes(desc):
    chip = _chip(desc, LOOP)
    u = chip.peripherals["USART1"]
    wr(chip, USART1 + 0x08, 139)
    wr(chip, USART1 + 0x0C, 1 << 13 | 1 << 3 | 1 << 2)             # UE TE RE
    # TIM6 update interrupt every 1 ms: not an EXTI line, it must not end Stop
    wr(chip, 0x40001000 + 0x28, 15)                                 # PSC: 1 MHz
    wr(chip, 0x40001000 + 0x2C, 999)
    wr(chip, 0x40001000 + 0x0C, 1)                                  # UIE
    wr(chip, 0x40001000, 1)                                         # CEN
    wr(chip, ISER0 + 4, 1 << (TIM6_IRQ - 32))
    wr(chip, EXTI, 1)                                               # IMR0
    wr(chip, EXTI + 0x0C, 1)                                        # FTSR0: PA0 falling edge
    wr(chip, ISER0, 1 << EXTI0_IRQ)
    chip.pins.set_external("PA0", "H")
    wr(chip, USART1 + 0x04, 0x41)                                   # character in progress at Stop entry
    wr(chip, SCR, SLEEPDEEP)
    chip.run_for(5e6)
    pwr = chip.peripherals["PWR"]
    assert pwr.mode == "stop" and _r4(chip) == 0
    assert not rd(chip, USART1) & 0x40                              # TC frozen (APB clock stopped)
    u.feed(b"z")
    chip.run_for(1e6)
    assert [e.get("reason") for e in chip.log if e.kind == "uart" and e.get("dir") == "drop"] == ["clock_stopped"]
    chip.pins.set_external("PA0", "L")
    chip.run_for(1e3)
    assert pwr.mode != "stop" and _r4(chip) >= 1
    chip.run_for(1e6)
    assert rd(chip, USART1) & 0x40                                  # the character finished after the wake-up


def test_iwdg_keeps_running_in_stop(desc):
    chip = _chip(desc, LOOP)
    wr(chip, IWDG, 0x5555)
    wr(chip, IWDG + 4, 0)                                           # /4: 8 kHz
    wr(chip, IWDG + 8, 79)                                          # 10 ms
    wr(chip, IWDG, 0xCCCC)
    wr(chip, SCR, SLEEPDEEP)
    chip.run_for(20e6)
    assert [e.get("reason") for e in chip.log if e.kind == "reset"][-1] == "watchdog"


# ================================================================================================ WFE
def test_wfe_event_register_sev_and_exti_event_line(desc):
    chip = _chip(desc, "sev\nwfe\nadds r4, #1\nloop: wfe\nadds r4, #1\nb loop")
    wr(chip, EXTI + 4, 1)                                           # EMR0 (event only, IMR0 = 0)
    wr(chip, EXTI + 8, 1)                                           # RTSR0
    wr(chip, SCR, SLEEPDEEP)                                        # Stop with WFE
    chip.pins.set_external("PA0", "L")
    chip.run_for(2e6)
    assert _r4(chip) == 1 and chip.sleeping                         # SEV made the first WFE fall through
    assert chip.peripherals["PWR"].mode == "stop"
    chip.pins.set_external("PA0", "H")                              # EXTI event: wakes WFE without PR
    chip.run_for(1e3)
    assert _r4(chip) == 2 and not rd(chip, EXTI + 0x14) & 1
    chip.run_for(2e6)
    assert _r4(chip) == 2 and chip.sleeping


def test_wfe_sevonpend_wakes_on_masked_exti_interrupt(desc):
    chip = _chip(desc, "loop: wfe\nadds r4, #1\nb loop")
    wr(chip, EXTI, 1)
    wr(chip, EXTI + 8, 1)                                           # IMR0 + RTSR0, NVIC line not enabled
    wr(chip, SCR, SEVONPEND)                                        # Sleep (no SLEEPDEEP)
    chip.pins.set_external("PA0", "L")
    chip.run_for(1e6)
    assert _r4(chip) == 0
    chip.pins.set_external("PA0", "H")
    chip.run_for(1e3)
    assert _r4(chip) == 1


# ================================================================================================ Standby
def test_standby_wkup_pin_resets_with_sbf_wuf_and_keeps_backup_domain(desc):
    chip = _chip(desc, LOOP)
    wr(chip, PWR, rd(chip, PWR) | 1 << 8)                           # DBP
    wr(chip, RCC + BDCR, 1 | 1 << 8 | 1 << 15)
    wr(chip, RTC + RTC_WPR, 0xCA)
    wr(chip, RTC + RTC_WPR, 0x53)
    wr(chip, RTC + RTC_BKP0R, 0xCAFE)
    chip.bus.write(0x20001000, 4, 0x12345678)
    chip.bus.write(0x40024000, 4, 0x55AA55AA)                       # backup SRAM, BRE = 0: lost
    wr(chip, PWR + 4, EWUP)
    wr(chip, PWR, rd(chip, PWR) | PDDS)
    wr(chip, SCR, SLEEPDEEP)
    chip.pins.set_external("PA0", "L")
    chip.run_for(1e6)
    pwr = chip.peripherals["PWR"]
    assert pwr.mode == "standby" and rd(chip, PWR + 4) & SBF
    assert chip.bus.read(0x20001000, 4) == 0 and chip.bus.read(0x40024000, 4) == 0
    assert chip.pins.get("PA0").pull == "down"
    chip.run_for(10e6)
    assert not [e for e in chip.log if e.kind == "reset" and e.get("reason") == "standby_wakeup"]
    chip.pins.set_external("PA0", "H")
    chip.run_for(1e4)
    assert [e.get("reason") for e in chip.log if e.kind == "reset"][-1] == "standby_wakeup"
    assert pwr.mode != "standby" and rd(chip, PWR + 4) & (WUF | SBF | EWUP) == WUF | SBF | EWUP
    assert rd(chip, RTC + RTC_BKP0R) == 0xCAFE
    wr(chip, RCC + APB1ENR, 1 << 28)
    wr(chip, PWR, CWUF | CSBF)
    assert rd(chip, PWR + 4) & (WUF | SBF) == 0 and rd(chip, PWR) & (CWUF | CSBF) == 0


def test_standby_rtc_wakeup_timer(desc):
    chip = _chip(desc, LOOP)
    _rtc_wakeup(chip, 0.5)
    wr(chip, PWR + 4, BRE)
    chip.bus.write(0x40024000, 4, 0x55AA55AA)                       # backup SRAM kept with BRE = 1
    wr(chip, PWR, rd(chip, PWR) | PDDS)
    wr(chip, SCR, SLEEPDEEP)
    t0 = chip.now_ns()
    chip.run_for(0.4e9)
    assert chip.peripherals["PWR"].mode == "standby"
    chip.run_for(t0 + 0.6e9 - chip.now_ns())
    rs = [(e.get("reason"), e.t_ns) for e in chip.log if e.kind == "reset"]
    assert rs[-1][0] == "standby_wakeup" and rs[-1][1] == pytest.approx(t0 + 0.5e9, abs=1e6)
    assert rd(chip, PWR + 4) & (WUF | SBF | BRE) == WUF | SBF | BRE
    assert chip.bus.read(0x40024000, 4) == 0x55AA55AA


def test_ewup_with_wkup_high_sets_wuf_and_sleep_mode_is_plain(desc):
    chip = _chip(desc, LOOP)
    chip.pins.set_external("PA0", "H")
    wr(chip, PWR + 4, EWUP)
    assert rd(chip, PWR + 4) & WUF
    chip.run_for(1e6)                                               # SLEEPDEEP = 0: Sleep
    assert chip.peripherals["PWR"].mode == "sleep" and chip.sleeping


# ================================================================================================ USART
def _af7(chip, *pins):
    for p in pins:
        hi = p >= 8
        reg = GPIOA + (0x24 if hi else 0x20)
        sh = 4 * (p - 8 if hi else p)
        wr(chip, reg, (rd(chip, reg) & ~(0xF << sh)) | 7 << sh)
        wr(chip, GPIOA, (rd(chip, GPIOA) & ~(3 << 2 * p)) | 2 << 2 * p)


def _usart(desc, cr3=0, cr1_extra=0):
    chip = _chip(desc, "loop: b loop")
    u = chip.peripherals["USART1"]
    wr(chip, USART1 + 0x08, 139)
    wr(chip, USART1 + 0x14, cr3)
    wr(chip, USART1 + 0x0C, 1 << 13 | 1 << 3 | 1 << 2 | cr1_extra)
    return chip, u


def test_usart_cts_holds_transmission_until_asserted(desc):
    chip, u = _usart(desc)
    _af7(chip, 11)                                                  # PA11 = USART1_CTS
    chip.pins.set_external("PA11", "H")                             # deasserted
    wr(chip, USART1 + 0x14, 1 << 9 | 1 << 10)                       # CTSE, CTSIE
    wr(chip, USART1 + 0x04, 0x41)
    chip.run_for(2 * u.char_ns())
    assert bytes(u.output) == b"" and not rd(chip, USART1) & 0xC0   # held in TDR: TXE = TC = 0
    chip.pins.set_external("PA11", "L")
    assert bytes(u.output) == b"A" and rd(chip, USART1) & 1 << 9    # SR.CTS: the input toggled
    assert (16 + 37) in chip.arch.asserted                          # CTSIE
    chip.run_for(u.char_ns() + 1e3)
    assert rd(chip, USART1) & 0xC0 == 0xC0
    # CTS deasserted while shifting: the character completes, the next one waits
    wr(chip, USART1 + 0x04, 0x42)
    chip.pins.set_external("PA11", "H")
    wr(chip, USART1 + 0x04, 0x43)
    chip.run_for(3 * u.char_ns())
    assert bytes(u.output) == b"AB"
    chip.pins.set_external("PA11", "L")
    assert bytes(u.output) == b"ABC"


def test_usart_rts_follows_rxne_and_uart4_has_no_flow_control(desc):
    chip, u = _usart(desc, cr3=1 << 8)                              # RTSE
    _af7(chip, 10, 12)                                              # PA10 RX, PA12 RTS
    assert chip.pins.get("PA12").level() == "L"                     # receiver ready
    u.feed(b"x")
    chip.run_for(u.char_ns() + 1e3)
    assert rd(chip, USART1) & 0x20 and chip.pins.get("PA12").level() == "H"
    assert rd(chip, USART1 + 4) == ord("x")
    assert chip.pins.get("PA12").level() == "L"
    wr(chip, USART1 + 0x14, 0)
    assert chip.pins.get("PA12").level() == "H"                     # released: idle (deasserted)
    wr(chip, 0x40004C00 + 0x14, 3 << 8)                             # UART4: RTSE/CTSE reserved
    assert rd(chip, 0x40004C00 + 0x14) == 0


def test_usart_break_lin_detection_and_parity(desc):
    chip, u = _usart(desc, cr1_extra=0)
    _af7(chip, 10)
    wr(chip, USART1 + 0x10, 1 << 14 | 1 << 6)                       # LINEN, LBDIE
    wr(chip, USART1 + 0x0C, rd(chip, USART1 + 0x0C) | 1)            # SBK
    assert [e.get("brk") for e in chip.log if e.kind == "uart" and e.get("dir") == "tx"] == [1]
    assert rd(chip, USART1 + 0x0C) & 1
    chip.run_for(10 * 139 / 16e6 * 1e9 + 1e3)
    assert not rd(chip, USART1 + 0x0C) & 1                          # SBK cleared at the end of the break
    u.receive_break()
    sr = rd(chip, USART1)
    assert sr & (1 << 8 | 1 << 1 | 1 << 5) == (1 << 8 | 1 << 1 | 1 << 5) and (16 + 37) in chip.arch.asserted
    assert rd(chip, USART1 + 4) == 0
    wr(chip, USART1, 0)                                             # LBD rc_w0
    # parity: 7 data bits + even parity (M = 0, PCE = 1)
    wr(chip, USART1 + 0x0C, 1 << 13 | 1 << 3 | 1 << 2 | 1 << 10)
    wr(chip, USART1 + 4, 0xFF)
    assert u.output[-1] == 0x7F
    u.receive(b"\x01")
    assert rd(chip, USART1 + 4) == 0x81                             # parity bit in the MSB
    u.receive(b"\x03")
    assert rd(chip, USART1 + 4) == 0x03


def test_usart_half_duplex_echo_and_mute_modes(desc):
    chip, u = _usart(desc, cr3=1 << 3)                              # HDSEL
    _af7(chip, 9)                                                   # PA9 = USART1_TX only
    wr(chip, USART1 + 4, 0x55)
    chip.run_for(u.char_ns() + 1e3)
    assert rd(chip, USART1) & 0x20 and rd(chip, USART1 + 4) == 0x55
    u.receive(b"\x66")                                              # the other node on the single wire
    assert rd(chip, USART1 + 4) == 0x66
    # address-mark mute: ADD = 5
    wr(chip, USART1 + 0x10, 5)
    wr(chip, USART1 + 0x0C, 1 << 13 | 1 << 3 | 1 << 2 | 1 << 11 | 1 << 1)   # WAKE = 1, RWU
    u.receive(b"\x10")
    assert not rd(chip, USART1) & 0x20
    u.receive(b"\x85")                                              # our address: wakes, received
    assert rd(chip, USART1 + 4) == 0x85 and not rd(chip, USART1 + 0x0C) & 2
    u.receive(b"\x11")
    assert rd(chip, USART1 + 4) == 0x11
    u.receive(b"\x86")                                              # another address: mute again
    assert rd(chip, USART1 + 0x0C) & 2 and not rd(chip, USART1) & 0x20
    # idle-line mute
    wr(chip, USART1 + 0x0C, 1 << 13 | 1 << 3 | 1 << 2 | 1 << 1)
    u.feed(b"ab")
    chip.run_for(2 * u.char_ns() + 1e3)
    assert not rd(chip, USART1) & 0x20
    chip.run_for(3 * u.char_ns())
    u.feed(b"c")
    chip.run_for(u.char_ns() + 1e3)
    assert rd(chip, USART1 + 4) == ord("c") and not rd(chip, USART1 + 0x0C) & 2


def test_usart_smartcard_irda_warn_once(desc):
    chip, u = _usart(desc)
    wr(chip, USART1 + 0x14, 1 << 5)
    wr(chip, USART1 + 0x14, 1 << 5 | 1 << 1)
    w = [e.fields["msg"] for e in chip.log if e.kind == "warn" and "not modelled" in e.fields.get("msg", "")]
    assert len(w) == 2 and "smartcard" in w[0]
