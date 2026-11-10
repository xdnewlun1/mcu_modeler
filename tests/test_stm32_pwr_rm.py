"""STM32F4 PWR / RCC / IWDG details checked against RM0090 ch. 5, 7 and 21: PWR_CR reset value, PWR_CSR reset
behaviour (WUF/EWUP vs SBF/BRE), BRE needs DBP, Stop entry ignored with an RTC flag set, the PVD, RCC_BDCR backup
domain protection / BDRST / RTCSEL lock, RCC_CSR reset flags, clocks in use cannot be stopped, IWDG start value and
PVU/RVU."""
import pytest

from test_stm32 import IWDG, PWR, RCC, desc, rd, spin_chip, wr  # noqa: F401  (desc: fixture)

APB1ENR, BDCR, CSR = 0x40, 0x70, 0x74
EXTI, RTC = 0x40013C00, 0x40002800
DBP, PVDE, VOS = 1 << 8, 1 << 4, 1 << 14
WUF, SBF, PVDO, EWUP, BRE = 1, 2, 4, 1 << 8, 1 << 9
PINRSTF, SFTRSTF = 1 << 26, 1 << 28


def _chip(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + APB1ENR, 1 << 28)                                    # PWREN
    return chip


def test_pwr_cr_reset_value_vos_scale1(desc):
    chip = _chip(desc)
    assert rd(chip, PWR) == 0x4000                                      # RM0090 5.4.1


def test_pwr_csr_wuf_ewup_cleared_by_system_reset_sbf_bre_kept(desc):
    chip = _chip(desc)
    wr(chip, PWR, rd(chip, PWR) | DBP)
    wr(chip, PWR + 4, EWUP | BRE)
    pwr = chip.peripherals["PWR"]
    pwr.set("CSR", pwr.get("CSR") | WUF | SBF)
    chip.reset()                                                        # NRST: system reset
    wr(chip, RCC + APB1ENR, 1 << 28)
    assert rd(chip, PWR + 4) & (WUF | SBF | EWUP | BRE) == SBF | BRE
    assert rd(chip, PWR) & DBP == 0                                     # PWR_CR reset


def test_bre_needs_dbp(desc):
    chip = _chip(desc)
    wr(chip, PWR + 4, BRE)
    assert not rd(chip, PWR + 4) & BRE
    wr(chip, PWR, rd(chip, PWR) | DBP)
    wr(chip, PWR + 4, BRE)
    assert rd(chip, PWR + 4) & BRE


def test_stop_entry_ignored_with_rtc_flag_set(desc):
    chip = _chip(desc)
    rtc = chip.peripherals["RTC"]
    rtc.set("ISR", rtc.get("ISR") | 1 << 10)                            # WUTF still set
    pwr = chip.peripherals["PWR"]
    pwr._enter_stop(chip)
    assert pwr.mode != "stop" and any("RTC alarm/wakeup" in e.get("msg", "") for e in chip.log if e.kind == "info")


def test_pvd_output_and_exti16(desc):
    chip = _chip(desc)
    pwr = chip.peripherals["PWR"]
    wr(chip, EXTI, 1 << 16)                                             # IMR16
    wr(chip, EXTI + 8, 1 << 16)                                         # RTSR16: VDD falls below the threshold
    wr(chip, PWR, rd(chip, PWR) | 7 << 5 | PVDE)                        # PLS = 2.9 V
    assert not rd(chip, PWR + 4) & PVDO and not rd(chip, EXTI + 0x14) & 1 << 16
    pwr.set_vdd(2.7)
    assert rd(chip, PWR + 4) & PVDO and rd(chip, EXTI + 0x14) & 1 << 16
    wr(chip, PWR, rd(chip, PWR) & ~PVDE)
    assert not rd(chip, PWR + 4) & PVDO                                 # valid only while PVDE = 1
    pwr.set_vdd(3.3)


def test_bdcr_write_protected_bdrst_and_rtcsel_lock(desc):
    chip = _chip(desc)
    wr(chip, RCC + BDCR, 1 | 1 << 8 | 1 << 15)
    assert rd(chip, RCC + BDCR) == 0                                    # DBP = 0: ignored (RM0090 7.3.20)
    wr(chip, PWR, rd(chip, PWR) | DBP)
    wr(chip, RCC + BDCR, 1 | 1 << 8 | 1 << 15)                          # LSEON, RTCSEL = LSE, RTCEN
    assert rd(chip, RCC + BDCR) == 1 | 2 | 1 << 8 | 1 << 15
    wr(chip, RCC + BDCR, 1 | 2 << 8 | 1 << 15 | 1 << 2)                 # RTCSEL = LSI, LSEBYP while LSE on
    assert rd(chip, RCC + BDCR) == 1 | 2 | 1 << 8 | 1 << 15             # both ignored
    wr(chip, PWR + 4, BRE)
    wr(chip, RTC + 0x24, 0xCA)
    wr(chip, RTC + 0x24, 0x53)
    wr(chip, RTC + 0x50, 0xBEEF)                                        # BKP0R
    assert rd(chip, RTC + 0x50) == 0xBEEF
    wr(chip, RCC + BDCR, 1 << 16)                                       # BDRST
    wr(chip, RCC + BDCR, 0)
    assert rd(chip, RCC + BDCR) == 0 and rd(chip, RTC + 0x50) == 0 and not rd(chip, PWR + 4) & BRE
    wr(chip, RCC + BDCR, 2 << 8)                                        # RTCSEL selectable again
    assert (rd(chip, RCC + BDCR) >> 8) & 3 == 2


def test_rcc_reset_flags_software_and_pin(desc):
    chip = _chip(desc)
    wr(chip, RCC + CSR, 1 << 24)                                        # RMVF
    assert rd(chip, RCC + CSR) >> 24 == 0
    wr(chip, 0xE000ED0C, 0x05FA0004)                                    # AIRCR.SYSRESETREQ
    chip.run_for(1e4)
    assert rd(chip, RCC + CSR) & (SFTRSTF | PINRSTF) == SFTRSTF | PINRSTF
    wr(chip, RCC + CSR, 1 << 24)
    chip.reset()                                                        # external reset (NRST)
    assert rd(chip, RCC + CSR) >> 24 == PINRSTF >> 24


def test_clock_in_use_cannot_be_stopped(desc):
    chip = _chip(desc)
    wr(chip, RCC, rd(chip, RCC) | 1 << 16)                              # HSEON
    wr(chip, RCC + 8, 1)                                                # SW = HSE
    assert (rd(chip, RCC + 8) >> 2) & 3 == 1
    wr(chip, RCC, rd(chip, RCC) & ~(1 << 16 | 1))                       # stop HSE and HSI
    cr = rd(chip, RCC)
    assert cr & 1 << 16 and cr & 1 << 17 and not cr & 1                 # HSE kept (SYSCLK), HSI may stop


def test_iwdg_starts_from_0xfff_and_pvu_rvu(desc):
    chip = _chip(desc)
    iw = chip.peripherals["IWDG"]
    wr(chip, IWDG, 0x5555)
    wr(chip, IWDG + 4, 0)                                               # /4: 8 kHz at LSI 32 kHz
    assert rd(chip, IWDG + 0x0C) == 1                                   # PVU: update in progress
    wr(chip, IWDG + 4, 3)                                               # ignored while PVU = 1
    wr(chip, IWDG + 8, 79)
    assert rd(chip, IWDG + 0x0C) == 3
    chip.run_for(5 / 32e3 * 1e9 + 1e3)                                  # 5 LSI cycles
    assert rd(chip, IWDG + 0x0C) == 0 and rd(chip, IWDG + 4) == 0
    wr(chip, IWDG, 0xCCCC)
    assert iw.counter() == 0xFFF and rd(chip, RCC + CSR) & 2            # counter reset value; LSI forced on
    chip.run_for(20e6)
    assert iw.timeouts == 0                                             # 4096 ticks at 8 kHz = 512 ms
    assert iw.counter() == pytest.approx(0xFFF - 160, abs=2)
    wr(chip, IWDG, 0xAAAA)                                              # now RLR = 79: 10 ms
    assert iw.counter() == 79
    chip.run_for(10.5e6)
    assert iw.timeouts == 1
