"""STM32F1 family models on the STM32F103T6: RCC, GPIO/AFIO, EXTI, PWR, BKP, RTC, FLASH/option bytes, system memory,
DBGMCU, bxCAN (F1 parameters) and the F4 models reused by parameter (USART, TIM, IWDG), driven through the bus with a
spin-loop firmware."""
from __future__ import annotations

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
AFIO, EXTI, GPIOA, GPIOB, GPIOD = 0x40010000, 0x40010400, 0x40010800, 0x40010C00, 0x40011400
USART1, TIM2, CAN = 0x40013800, 0x40000000, 0x40006400


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


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


def asserted(chip, irq):
    return (16 + irq) in chip.arch.asserted


def warns(chip):
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def pll72(chip):
    wr(chip, RCC + 0x00, rd(chip, RCC) | (1 << 16))              # HSEON
    assert rd(chip, RCC) & (1 << 17)                             # HSERDY follows at once
    wr(chip, FLASH_IF, 0x32)                                     # LATENCY 2
    wr(chip, RCC + 0x04, (1 << 16) | (7 << 18) | (4 << 8) | (2 << 14))   # PLLSRC HSE, x9, PPRE1 /2, ADCPRE /6
    wr(chip, RCC, rd(chip, RCC) | (1 << 24))
    wr(chip, RCC + 0x04, rd(chip, RCC + 0x04) | 2)


# ------------------------------------------------------------------ RCC
def test_rcc_reset_and_pll(desc):
    chip = spin_chip(desc)
    r = chip.peripherals["RCC"]
    assert rd(chip, RCC) & 0x3 == 0x3 and rd(chip, RCC + 0x14) == 0x14     # HSION|HSIRDY, AHBENR SRAM|FLITF
    assert rd(chip, RCC + 0x24) & 0x0C000000 == 0x0C000000                 # PORRSTF | PINRSTF at power-on
    assert chip.clock.hz == 8e6
    pll72(chip)
    assert (rd(chip, RCC + 0x04) >> 2) & 3 == 2                            # SWS = PLL
    assert chip.clock.hz == 72e6 and r.apb1_hz == 36e6 and r.apb2_hz == 72e6
    assert r.effective_timer_hz(1) == 72e6 and r.adc_hz() == 12e6
    assert r.usb_hz() == 48e6
    assert not warns(chip)


def test_rcc_pll_hsi_div2_and_limits(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + 0x04, 14 << 18)                               # HSI/2 x 16 = 64 MHz
    wr(chip, RCC, rd(chip, RCC) | (1 << 24))
    wr(chip, RCC + 0x04, rd(chip, RCC + 0x04) | 2)
    assert chip.clock.hz == 64e6
    assert any("PCLK1" in m for m in warns(chip))                 # APB1 64 MHz > 36 MHz
    assert any("LATENCY" in m for m in warns(chip))               # 0 wait states at 64 MHz
    # the PLL configuration is locked while the PLL runs
    wr(chip, RCC + 0x04, (rd(chip, RCC + 0x04) & ~(0xF << 18)) | (2 << 18))
    assert (rd(chip, RCC + 0x04) >> 18) & 0xF == 14


def test_rcc_switch_refused_and_cir(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + 0x04, 1)                                      # SW = HSE while HSE is off: pending (7.2.6)
    assert (rd(chip, RCC + 0x04) >> 2) & 3 == 0 and any("pending" in m for m in warns(chip))
    wr(chip, RCC + 0x08, 1 << 11)                                # HSERDYIE
    wr(chip, RCC, rd(chip, RCC) | (1 << 16))
    assert rd(chip, RCC + 0x08) & (1 << 3) and asserted(chip, 5)   # HSERDYF -> RCC vector
    assert (rd(chip, RCC + 0x04) >> 2) & 3 == 1                    # the pending switch completes once HSE is ready
    wr(chip, RCC + 0x08, (1 << 11) | (1 << 19))                  # HSERDYC
    assert not rd(chip, RCC + 0x08) & (1 << 3) and not asserted(chip, 5)


def test_rcc_reset_flags_and_bdcr(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + 0x24, 1 << 24)                                # RMVF
    assert rd(chip, RCC + 0x24) & 0xFC000000 == 0
    chip.arch.reset_requested = "SYSRESETREQ"
    chip.reset()
    assert rd(chip, RCC + 0x24) & 0xFC000000 == (1 << 28) | (1 << 26)    # SFTRSTF | PINRSTF
    wr(chip, RCC + 0x20, 1)                                      # BDCR without DBP: ignored
    assert rd(chip, RCC + 0x20) == 0 and any("DBP" in m for m in warns(chip))
    wr(chip, RCC + 0x1C, 1 << 28)
    wr(chip, PWR, 1 << 8)
    wr(chip, RCC + 0x20, 1 | (1 << 8) | (1 << 15))               # LSEON, RTCSEL LSE, RTCEN
    assert rd(chip, RCC + 0x20) == 0x8103
    chip.reset()
    assert rd(chip, RCC + 0x20) == 0x8103                        # backup domain survives a system reset


def test_rcc_apb_reset_resets_block(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + 0x18, 1 << 14)
    wr(chip, USART1 + 0x08, 625)
    wr(chip, RCC + 0x0C, 1 << 14)                                # USART1RST
    assert rd(chip, USART1 + 0x08) == 0


# ------------------------------------------------------------------ GPIO / AFIO
def test_gpio_modes_bsrr_brr(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + 0x18, 0b1111 << 2 | 1)
    b0 = chip.pins.get("PB0")
    assert b0.owner == "GPIO" and b0.direction == "in"            # reset: floating input
    wr(chip, GPIOB + 0x00, (rd(chip, GPIOB) & ~0xF) | 0x2)       # output push-pull 2 MHz
    wr(chip, GPIOB + 0x10, 1)
    assert b0.direction == "out" and b0.level() == "H"
    wr(chip, GPIOB + 0x14, 1)                                    # BRR
    assert b0.level() == "L" and rd(chip, GPIOB + 0x0C) & 1 == 0
    wr(chip, GPIOB + 0x10, 0x10001)                              # set and reset: set wins
    assert b0.level() == "H"
    wr(chip, GPIOB + 0x00, (rd(chip, GPIOB) & ~0xF) | 0x6)       # open-drain: 1 releases
    assert b0.direction == "in"
    # input pull-up/down chosen by ODR
    wr(chip, GPIOB + 0x00, (rd(chip, GPIOB) & ~0xF0) | 0x80)
    b1 = chip.pins.get("PB1")
    wr(chip, GPIOB + 0x10, 2)
    assert b1.pull == "up" and rd(chip, GPIOB + 0x08) & 2
    wr(chip, GPIOB + 0x14, 2)
    assert b1.pull == "down" and not rd(chip, GPIOB + 0x08) & 2
    # analog mode: ADC channel owner, IDR bit reads 0
    wr(chip, GPIOA + 0x00, rd(chip, GPIOA) & ~0xF)
    assert chip.pins.get("PA0").owner == "ADC12_IN0" and chip.pins.get("PA0").direction == "analog"


def test_gpio_lckr(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + 0x18, 1 << 3)
    for v in (0x10001, 0x00001, 0x10001):
        wr(chip, GPIOB + 0x18, v)
    rd(chip, GPIOB + 0x18)
    assert rd(chip, GPIOB + 0x18) & 0x10000
    wr(chip, GPIOB + 0x00, 0x44444443)
    assert rd(chip, GPIOB) & 0xF == 4 and any("locked" in m for m in warns(chip))


def test_af_owner_and_usart1_remap(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + 0x18, (1 << 14) | (0b11 << 2) | 1)            # USART1, GPIOA/B, AFIO
    wr(chip, USART1 + 0x0C, (1 << 13) | (1 << 3) | (1 << 2))     # UE TE RE
    wr(chip, GPIOA + 0x04, (rd(chip, GPIOA + 4) & ~0xF0) | 0xB0)  # PA9 AF push-pull
    a9, b6 = chip.pins.get("PA9"), chip.pins.get("PB6")
    assert a9.owner == "USART1_TX" and a9.direction == "out" and a9.level() == "H"
    assert chip.pins.get("PA10").owner == "USART1_RX"            # input floating + USART clocked
    wr(chip, GPIOB + 0x00, (rd(chip, GPIOB) & ~0x0F000000) | 0x0B000000)    # PB6 AF push-pull
    assert b6.owner == "I2C1_SCL" and b6.direction == "in"       # only I2C1 routed here (off: released)
    wr(chip, AFIO + 0x04, 1 << 2)                                 # USART1_REMAP
    assert b6.owner == "USART1_TX" and a9.owner == "TIM1_CH2"    # PA9 keeps only TIM1_CH2 (TIM1 off)
    assert chip.pins.get("PB7").owner == "USART1_RX" and chip.pins.get("PA10").owner == "GPIO"


def test_swj_release_and_pd01(desc):
    chip = spin_chip(desc)
    assert chip.pins.get("PA13").owner == "JTMS/SWDIO" and chip.pins.get("PA13").pull == "up"
    assert chip.pins.get("PB3").owner == "JTDO/TRACESWO" and chip.pins.get("PD0").owner == "OSC_IN"
    wr(chip, RCC + 0x18, 1 | (1 << 3) | (1 << 5))
    wr(chip, AFIO + 0x04, 2 << 24)                                # JTAG-DP off, SW-DP on
    assert rd(chip, AFIO + 0x04) == 0                             # SWJ_CFG is write-only
    assert chip.pins.get("PB3").owner == "GPIO" and chip.pins.get("PA15").owner == "GPIO"
    assert chip.pins.get("PA13").owner == "JTMS/SWDIO"
    wr(chip, AFIO + 0x04, 1 << 15)                                # PD01_REMAP
    wr(chip, GPIOD + 0x00, 0x44444442)
    assert chip.pins.get("PD0").owner == "GPIO" and chip.pins.get("PD0").direction == "out"
    wr(chip, RCC, rd(chip, RCC) | (1 << 16))                     # HSE on: the oscillator takes the pins back
    assert chip.pins.get("PD0").owner == "OSC_IN"


def test_exti_through_afio(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + 0x18, 1 | (1 << 3))
    wr(chip, AFIO + 0x0C, 1 << 4)                                 # EXTICR2: EXTI5 = PB5
    wr(chip, EXTI + 0x0C, 1 << 5)                                 # falling
    wr(chip, EXTI + 0x00, 1 << 5)
    chip.pins.set_external("PB5", 1)
    chip.pins.set_external("PB5", 0)
    assert rd(chip, EXTI + 0x14) == 1 << 5 and asserted(chip, 23)   # EXTI9_5
    wr(chip, EXTI + 0x14, 1 << 5)
    assert rd(chip, EXTI + 0x14) == 0 and not asserted(chip, 23)
    chip.pins.set_external("PA5", 1)
    chip.pins.set_external("PA5", 0)                              # PA5 is not selected
    assert rd(chip, EXTI + 0x14) == 0


# ------------------------------------------------------------------ BKP / RTC / PWR
def backup_on(chip):
    wr(chip, RCC + 0x1C, (1 << 27) | (1 << 28))
    wr(chip, PWR, 1 << 8)


def test_bkp_protection_and_persistence(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + 0x1C, 1 << 27)
    wr(chip, BKP + 0x04, 0x1234)
    assert rd(chip, BKP + 0x04) == 0 and any("DBP" in m for m in warns(chip))
    backup_on(chip)
    wr(chip, BKP + 0x04, 0xABCD)
    wr(chip, BKP + 0x28, 0x5A5A)
    chip.reset()
    assert rd(chip, BKP + 0x04) == 0xABCD and rd(chip, BKP + 0x28) == 0x5A5A
    wr(chip, RCC + 0x1C, (1 << 27) | (1 << 28))
    wr(chip, PWR, 1 << 8)
    wr(chip, RCC + 0x20, 1 << 16)                                 # BDRST
    assert rd(chip, BKP + 0x04) == 0


def rtc_start(chip, prl=32767, alarm=None, secie=False):
    backup_on(chip)
    wr(chip, RCC + 0x20, 1 | (1 << 8) | (1 << 15))
    while not rd(chip, RTC + 0x04) & 0x20:
        chip.run_for(10e3)
    wr(chip, RTC + 0x04, 0x10)                                    # CNF
    wr(chip, RTC + 0x08, prl >> 16)
    wr(chip, RTC + 0x0C, prl & 0xFFFF)
    wr(chip, RTC + 0x18, 0)
    wr(chip, RTC + 0x1C, 0)
    if alarm is not None:
        wr(chip, RTC + 0x20, alarm >> 16)
        wr(chip, RTC + 0x24, alarm & 0xFFFF)
    wr(chip, RTC + 0x04, 0)
    if secie:
        chip.run_for(200e3)
        wr(chip, RTC + 0x00, 1)


def test_rtc_counting_alarm_and_flags(desc):
    chip = spin_chip(desc)
    rtc_start(chip, prl=327, alarm=3)                             # 32768 / 328 = 99.9 Hz
    assert not rd(chip, RTC + 0x04) & 0x20                        # RTOFF = 0 during the write operation
    assert rd(chip, RTC + 0x08) == 0 and rd(chip, RTC + 0x24) == 0   # PRL/ALR are write-only
    t0 = chip.now_ns()
    chip.run_for(100e3)
    assert rd(chip, RTC + 0x04) & 0x20
    chip.run_for(35e6)
    assert rd(chip, RTC + 0x1C) == 3 and not rd(chip, RTC + 0x04) & 0x2
    chip.run_for(6e6)
    assert rd(chip, RTC + 0x1C) == 4 and rd(chip, RTC + 0x04) & 0x2   # ALRF at the end of CNT = ALR (18.3.5)
    alarm = [e for e in chip.log if e.kind == "info" and "RTC alarm" in e.fields.get("msg", "")]
    assert alarm and abs(alarm[0].t_ns - t0 - (4 * 328 - 1) / 32768 * 1e9) < 2e5
    div = rd(chip, RTC + 0x14)
    assert 0 <= div <= 327
    wr(chip, RTC + 0x04, 0x3F & ~0x2)                             # rc_w0: clear ALRF only
    assert not rd(chip, RTC + 0x04) & 0x2
    # core keeps counting across a system reset; CRL comes back with RSF = 0
    cnt = rd(chip, RTC + 0x1C)
    chip.reset()
    assert rd(chip, RTC + 0x04) & 0x8 == 0
    chip.run_for(20e6)
    assert rd(chip, RTC + 0x1C) >= cnt + 1


def test_rtc_write_outside_cnf_ignored(desc):
    chip = spin_chip(desc)
    rtc_start(chip)
    chip.run_for(1e6)
    wr(chip, RTC + 0x1C, 77)
    assert rd(chip, RTC + 0x1C) != 77 and any("CNF" in m for m in warns(chip))


def test_rtc_second_irq(desc):
    chip = spin_chip(desc)
    rtc_start(chip, prl=327, secie=True)
    chip.run_for(15e6)
    assert rd(chip, RTC + 0x04) & 1 and asserted(chip, 3)
    wr(chip, RTC + 0x04, 0x3E)
    assert not asserted(chip, 3)


def test_pwr_pvd_and_flags(desc):
    chip = spin_chip(desc)
    wr(chip, RCC + 0x1C, 1 << 28)
    p = chip.peripherals["PWR"]
    wr(chip, PWR, (1 << 4) | (7 << 5))                            # PVDE, 2.9 V
    assert not rd(chip, PWR + 4) & 4
    p.set_vdd(2.5)
    assert rd(chip, PWR + 4) & 4                                  # PVDO
    p.set("CSR", p.get("CSR") | 3)
    wr(chip, PWR, (1 << 4) | (7 << 5) | (1 << 2) | (1 << 3))      # CWUF, CSBF
    assert rd(chip, PWR + 4) & 3 == 0 and rd(chip, PWR) & 0xC == 0


# ------------------------------------------------------------------ FLASH / option bytes / signature
def unlock(chip):
    wr(chip, FLASH_IF + 0x04, 0x45670123)
    wr(chip, FLASH_IF + 0x04, 0xCDEF89AB)


def test_flash_unlock_erase_timing(desc):
    chip = spin_chip(desc)
    assert rd(chip, FLASH_IF + 0x10) & 0x80                       # LOCK
    assert rd(chip, FLASH_IF) == 0x30                              # PRFTBE | PRFTBS
    unlock(chip)
    assert not rd(chip, FLASH_IF + 0x10) & 0x80
    chip.core.write_mem(0x08007C00, b"\x00" * 16)                  # pretend the page holds data
    chip.peripherals["FLASH"]._snap = None
    wr(chip, FLASH_IF + 0x10, 2)
    wr(chip, FLASH_IF + 0x14, 0x08007C10)
    wr(chip, FLASH_IF + 0x10, 2 | 0x40)                            # PER STRT
    assert rd(chip, FLASH_IF + 0x0C) & 1                           # BSY
    assert chip.core.read_mem(0x08007C00, 16) == b"\xff" * 16
    chip.run_for(29e6)
    assert rd(chip, FLASH_IF + 0x0C) & 1
    chip.run_for(2e6)
    assert rd(chip, FLASH_IF + 0x0C) == 0x20                       # EOP after 30 ms


def test_flash_wrong_key_locks(desc):
    chip = spin_chip(desc)
    wr(chip, FLASH_IF + 0x04, 0x12345678)
    unlock(chip)
    assert rd(chip, FLASH_IF + 0x10) & 0x80 and any("wrong unlock" in m for m in warns(chip))


def test_option_bytes_and_signature(desc):
    chip = spin_chip(desc)
    assert chip.bus.read(0x1FFFF800, 2) == 0x5AA5 and chip.bus.read(0x1FFFF802, 2) == 0x00FF
    assert rd(chip, FLASH_IF + 0x1C) == 0x03FFFFFC and rd(chip, FLASH_IF + 0x20) == 0xFFFFFFFF
    assert chip.bus.read(0x1FFFF7E0, 2) == 32                      # F_SIZE = 32 KB
    assert chip.bus.read(0x1FFFF7E8, 4) != 0xFFFFFFFF
    assert rd(chip, 0xE0042000) & 0xFFF == 0x412                   # DEV_ID low density
    unlock(chip)
    wr(chip, FLASH_IF + 0x08, 0x45670123)
    wr(chip, FLASH_IF + 0x08, 0xCDEF89AB)
    assert rd(chip, FLASH_IF + 0x10) & (1 << 9)                   # OPTWRE
    wr(chip, FLASH_IF + 0x10, (1 << 9) | (1 << 5))
    wr(chip, FLASH_IF + 0x10, (1 << 9) | (1 << 5) | (1 << 6))     # OPTER STRT
    chip.run_for(31e6)
    wr(chip, FLASH_IF + 0x10, (1 << 9) | (1 << 4))                # OPTPG
    chip.bus.write(0x1FFFF802, 2, 0xFE)                           # USER: WDG_SW = 0 (hardware watchdog)
    chip.bus.write(0x1FFFF800, 2, 0xA5)
    assert chip.bus.read(0x1FFFF802, 2) == 0x01FE
    assert rd(chip, FLASH_IF + 0x1C) == 0x03FFFFFC                # effective only after reset
    chip.reset()
    assert not (rd(chip, FLASH_IF + 0x1C) >> 2) & 1
    assert chip.peripherals["IWDG"].running                       # started by the reset


# ------------------------------------------------------------------ reused F4 models
def test_reused_models_use_f1_clocks_and_gates(desc):
    chip = spin_chip(desc)
    pll72(chip)
    usart, tim1, iwdg = chip.peripherals["USART1"], chip.peripherals["TIM1"], chip.peripherals["IWDG"]
    assert usart.module_hz() == 72e6 and chip.peripherals["USART2"].module_hz() == 36e6
    assert tim1.kernel_hz() == 72e6 and chip.peripherals["TIM2"].kernel_hz() == 72e6    # APB1 /2 -> x2
    assert chip.peripherals["TIM2"].bits == 16
    assert tim1._irqn == 25 and tim1._cc_irqn == 27 and tim1._brk_irqn == 24 and tim1._trg_irqn == 26
    assert iwdg.lsi_hz == 40000
    wr(chip, USART1 + 0x08, 0x1234)                                # clock off: warn, proceeds
    assert any("USART1 accessed while its clock is disabled (RCC_APB2ENR bit 14)" in m for m in warns(chip))


def test_can_f1_parameters(desc):
    chip = spin_chip(desc)
    can = chip.peripherals["CAN"]
    assert can.n_banks == 14 and can.rx_signal == "CAN_RX" and can.gate == "CAN"
    assert can._irqs == {"TX": 19, "RX0": 20, "RX1": 21, "SCE": 22}
    assert "F14R1" not in can.regs and "F13R2" in can.regs
    wr(chip, RCC + 0x1C, 1 << 25)
    wr(chip, CAN + 0x200, 1)                                       # FMR = FINIT (CAN2SB written 0)
    assert (rd(chip, CAN + 0x200) >> 8) & 0x3F == 14               # all 14 banks stay with the controller
