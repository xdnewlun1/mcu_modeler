"""PIT, LPTMR and RTC models (chipem/periph/kinetis/{pit,lptmr,rtc}.py)."""
import pytest

from kutil import (LPTMR_BASE, PIT_BASE, RTC_BASE, SPIN, count_handler, counter, enable_irq, kchip)

# 1 MHz core, no MCG model -> bus clock 500 kHz


def pit_setup(ch: int, ldval: int, tctrl: int = 3) -> str:
    b = 0x100 + 0x10 * ch
    return f"""
  ldr r0, =0x{PIT_BASE:08X}
  movs r1, #0
  str r1, [r0]
  ldr r1, ={ldval}
  str r1, [r0, #0x{b:X}]
  movs r1, #{tctrl}
  str r1, [r0, #0x{b + 8:X}]
"""


PIT0_CLEAR = f"""
  ldr r0, =0x{PIT_BASE:08X}
  movs r1, #1
  str r1, [r0, #0x10C]
"""


def test_pit_period_and_irq():
    # LDVAL 499 -> 500 bus clocks = 1 ms
    chip, _ = kchip({"reset": pit_setup(0, 499) + enable_irq(0) + SPIN, "h": count_handler(PIT0_CLEAR)},
                    vectors={16: "h"})
    chip.run_for(10.5e6)
    assert counter(chip) == 10
    pit = chip.peripherals["PIT"]
    assert pit.get("TFLG0") == 0                      # cleared by the handler (w1c)
    assert 16 not in chip.arch.pending


def test_pit_disabled_by_default_and_cval_live():
    chip, _ = kchip({"reset": SPIN})
    pit = chip.peripherals["PIT"]
    assert pit.get("MCR") & 2                         # MDIS set at reset
    chip.bus.write(PIT_BASE + 0x100, 4, 9999)
    chip.bus.write(PIT_BASE + 0x108, 4, 1)            # TEN, but module disabled
    chip.run_for(1e6)
    assert not pit.ch[0].running
    chip.bus.write(PIT_BASE, 4, 0)                    # MDIS=0 -> starts
    chip.run_for(2e6)                                 # 2 ms = 1000 bus clocks
    cv = chip.bus.read(PIT_BASE + 0x104, 4)
    assert 9999 - 1000 - 5 <= cv <= 9999 - 1000 + 5
    chip.bus.write(PIT_BASE + 0x108, 4, 0)            # stop: CVAL freezes
    frozen = chip.bus.read(PIT_BASE + 0x104, 4)
    chip.run_for(1e6)
    assert chip.bus.read(PIT_BASE + 0x104, 4) == frozen


def test_pit_tif_level_without_tie_and_w1c():
    chip, _ = kchip({"reset": pit_setup(1, 99, tctrl=1) + SPIN})
    chip.run_for(1e6)                                 # 100 bus clocks = 0.2 ms period
    assert chip.bus.read(PIT_BASE + 0x11C, 4) == 1
    assert 17 not in chip.arch.pending                # TIE=0: no IRQ
    chip.bus.write(PIT_BASE + 0x11C, 4, 1)
    assert chip.bus.read(PIT_BASE + 0x11C, 4) == 0


def test_pit_chained():
    # ch0 period 100 bus clocks (0.2 ms); ch1 chained, LDVAL 4 -> expires every 5 ch0 expirations (1 ms)
    code = pit_setup(0, 99, tctrl=1) + f"""
  ldr r1, =4
  str r1, [r0, #0x110]
  movs r1, #7
  str r1, [r0, #0x118]
""" + enable_irq(1) + SPIN
    clear1 = f"""
  ldr r0, =0x{PIT_BASE:08X}
  movs r1, #1
  str r1, [r0, #0x11C]
"""
    chip, _ = kchip({"reset": code, "h": count_handler(clear1)}, vectors={17: "h"})
    chip.run_for(5.1e6)
    assert counter(chip) == 5


def test_lptmr_compare_irq_lpo():
    # LPO 1 kHz, prescaler bypassed, CMR=4 -> TCF every 5 ms
    code = f"""
  ldr r0, =0x{LPTMR_BASE:08X}
  movs r1, #5
  str r1, [r0, #4]
  movs r1, #4
  str r1, [r0, #8]
  movs r1, #0x41
  str r1, [r0]
""" + enable_irq(4) + SPIN
    clear = f"""
  ldr r0, =0x{LPTMR_BASE:08X}
  movs r1, #0xC1
  str r1, [r0]
"""
    chip, _ = kchip({"reset": code, "h": count_handler(clear)}, vectors={20: "h"})
    chip.run_for(26e6)
    assert counter(chip) == 5
    lp = chip.peripherals["LPTMR0"]
    assert lp.tick_hz() == 1000.0
    assert 0 <= chip.bus.read(LPTMR_BASE + 0xC, 4) <= 4


def test_lptmr_free_running_and_disable_resets():
    chip, _ = kchip({"reset": SPIN})
    lp = chip.peripherals["LPTMR0"]
    chip.bus.write(LPTMR_BASE + 4, 4, 5)              # LPO, bypass
    chip.bus.write(LPTMR_BASE + 8, 4, 2)
    chip.bus.write(LPTMR_BASE, 4, 0x05)               # TEN | TFC (free running)
    chip.run_for(10.5e6)
    assert lp.get("CSR") & 0x80                       # TCF
    assert chip.bus.read(LPTMR_BASE + 0xC, 4) == 10   # not reset on compare
    chip.bus.write(LPTMR_BASE, 4, 0x85)               # w1c TCF
    assert not lp.get("CSR") & 0x80
    chip.bus.write(LPTMR_BASE, 4, 0)                  # disable -> counter reset
    assert chip.bus.read(LPTMR_BASE + 0xC, 4) == 0


def test_lptmr_prescaler():
    chip, _ = kchip({"reset": SPIN})
    lp = chip.peripherals["LPTMR0"]
    chip.bus.write(LPTMR_BASE + 4, 4, (3 << 3) | 2)   # ERCLK32K / 16
    assert lp.tick_hz() == 32768 / 16
    chip.bus.write(LPTMR_BASE + 4, 4, 3 | 4)          # OSCERCLK bypass
    assert lp.tick_hz() == 8e6


def rtc_start(tsr: int, tar: int | None = None, ier: int = 0) -> str:
    s = f"""
  ldr r0, =0x{RTC_BASE:08X}
  ldr r1, ={tsr}
  str r1, [r0]
  ldr r1, ={ier}
  str r1, [r0, #0x1C]
"""
    if tar is not None:
        s += f"""
  ldr r1, ={tar}
  str r1, [r0, #8]
"""
    return s + """
  movs r1, #0x10
  str r1, [r0, #0x14]
"""


def test_rtc_seconds_and_tif():
    chip, _ = kchip({"reset": SPIN})
    rtc = chip.peripherals["RTC"]
    assert rtc.get("SR") & 1                          # time invalid after POR
    chip.bus.write(RTC_BASE + 0x14, 4, 0x10)          # TCE with TIF set: does not count
    chip.run_for(1.5e9)
    assert chip.bus.read(RTC_BASE, 4) == 0
    chip.bus.write(RTC_BASE + 0x14, 4, 0)
    chip.bus.write(RTC_BASE, 4, 100)                  # clears TIF
    assert not rtc.get("SR") & 1
    chip.bus.write(RTC_BASE + 0x14, 4, 0x10)
    chip.run_for(3.5e9)
    assert chip.bus.read(RTC_BASE, 4) == 103
    tpr = chip.bus.read(RTC_BASE + 4, 4)
    assert 16000 <= tpr <= 16800                      # half a second into the current second
    chip.bus.write(RTC_BASE, 4, 5)                    # ignored while TCE=1
    assert chip.bus.read(RTC_BASE, 4) == 103


def test_rtc_alarm_irq_and_seconds_irq():
    # alarm at TSR == 3 -> TAF on the 3 -> 4 transition (4 s); TAIE (bit 2) + TSIE (bit 4)
    clear_alarm = f"""
  ldr r0, =0x{RTC_BASE:08X}
  ldr r1, =0xFFFF
  str r1, [r0, #8]
"""
    sec = f"""
  ldr r0, =0x{0x20000100 + 0x90:08X}
  ldr r1, [r0]
  adds r1, #1
  str r1, [r0]
  bx lr
"""
    # TIF is set out of POR with TIIE=1, so the alarm line was asserted (and pended) before rtc_start cleared TIF;
    # like real firmware, clear the stale NVIC pending bits (ICPR) before enabling the lines.
    code = rtc_start(0, tar=3, ier=0x14) + """
  ldr r0, =0xE000E280
  movs r1, #0x60
  str r1, [r0]
""" + enable_irq(5) + enable_irq(6) + f"""
  ldr r0, =0xE000E100
  movs r1, #0x60
  str r1, [r0]
""" + SPIN
    chip, _ = kchip({"reset": code, "alarm": count_handler(clear_alarm), "sec": sec}, vectors={21: "alarm", 22: "sec"})
    chip.run_for(3.9e9)
    assert counter(chip) == 0
    chip.run_for(0.2e9)
    assert counter(chip) == 1
    assert chip.bus.read(RTC_BASE, 4) == 4
    assert not chip.peripherals["RTC"].get("SR") & 4  # TAF cleared by the TAR write
    assert chip.bus.read(0x20000100 + 0x90, 4) == 4   # one seconds interrupt per second


def test_rtc_survives_system_reset_and_swr():
    chip, _ = kchip({"reset": rtc_start(10) + SPIN})
    chip.run_for(2.5e9)
    assert chip.bus.read(RTC_BASE, 4) == 12
    chip.reset("SYSRESETREQ")
    chip.run_for(1e9)
    assert chip.bus.read(RTC_BASE, 4) >= 13           # kept counting (VBAT domain)
    chip.bus.write(RTC_BASE + 0x10, 4, 1)             # CR.SWR
    rtc = chip.peripherals["RTC"]
    assert rtc.get("SR") & 1 and chip.bus.read(RTC_BASE, 4) == 0
    chip.bus.write(RTC_BASE + 0x10, 4, 0)
    assert rtc.get("CR") == 0
