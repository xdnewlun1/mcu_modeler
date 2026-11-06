"""STM32F405ZG: chip description (pin table, SVD cross-check) and the stm32 family models (RCC, GPIO, SYSCFG/EXTI,
USART, TIM, IWDG, FLASH/PWR/DBGMCU), driven through the bus on the real chip YAML with a spin-loop firmware."""
from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import FirmwareImage

from engine_util import asm

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "chips" / "stm32f405zg.yaml"
REGS = ROOT / "chips" / "stm32f405_regs.yaml"
SVD = sorted((ROOT / "svd").glob("dl-stm32f405-*/STM32F405.svd"))

FLASH = 0x08000000
RCC, FLASH_IF, PWR = 0x40023800, 0x40023C00, 0x40007000
GPIOA, GPIOB = 0x40020000, 0x40020400
SYSCFG, EXTI = 0x40013800, 0x40013C00
USART1, USART2 = 0x40011000, 0x40004400
TIM2, TIM3, TIM6 = 0x40000000, 0x40000400, 0x40001000
IWDG = 0x40003000


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def spin_chip(desc, **kw) -> Chip:
    """The STM32F405ZG with a firmware that only spins (vector table at 0x08000000, VTOR from the chip YAML)."""
    code = asm("spin: b spin", FLASH + 0x200)
    img = bytearray(0x200 + len(code))
    img[0:8] = struct.pack("<II", 0x20020000, (FLASH + 0x200) | 1)
    img[0x200:] = code
    chip = Chip(desc, **kw)
    chip.load_firmware(FirmwareImage([(FLASH, bytes(img))]))
    chip.reset()
    return chip


def rd(chip, addr):
    return chip.bus.read(addr, 4)


def wr(chip, addr, v):
    chip.bus.write(addr, 4, v)


def asserted(chip, irq: int) -> bool:
    return (16 + irq) in chip.arch.asserted


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


# ------------------------------------------------------------------------------------------------ chip YAML
def test_pin_table(desc):
    assert desc.name == "STM32F405ZG" and desc.package == "LQFP144"
    assert desc.gpio_name_format == "P{port}{bit}"
    nums = [p.num for p in desc.pins]
    assert len(nums) == 144 and sorted(nums) == list(range(1, 145))
    gpio = [p for p in desc.pins if p.kind == "gpio"]
    assert len(gpio) == 114                                   # PA-PG (7 x 16) + PH0/PH1
    assert len({p.gpio_name for p in gpio}) == 114
    pin = desc.pin
    assert pin("PA9").num == 101 and pin("PA9").functions["AF7"] == "USART1_TX"
    assert pin("PA10").num == 102 and pin("PA10").functions["AF7"] == "USART1_RX"
    assert pin("PD8").num == 77 and pin("PD8").functions["AF7"] == "USART3_TX"
    assert pin("PB6").num == 136 and pin("PB6").functions["AF4"] == "I2C1_SCL"
    assert pin("PB7").functions["AF4"] == "I2C1_SDA" and pin("PB7").functions["AF7"] == "USART1_RX"
    assert pin("PA0").functions == {"AF1": "TIM2_CH1_ETR", "AF2": "TIM5_CH1", "AF3": "TIM8_ETR", "AF7": "USART2_CTS",
                                    "AF8": "UART4_TX", "AF15": "EVENTOUT", "ADC": "ADC123_IN0", "additional": "WKUP"}
    assert pin("PA13").functions["AF0"] == "JTMS-SWDIO" and pin("PA13").default == "JTMS-SWDIO"
    assert pin("PA4").functions["ADC"] == "ADC12_IN4" and pin("PA4").functions["analog"] == "DAC_OUT1"
    assert pin("PG13").functions["AF8"] == "USART6_CTS"     # data sheet typo "UART6_CTS" corrected
    assert pin(23).name == "PH0/OSC_IN" and pin("PH0").functions["analog"] == "OSC_IN"
    assert pin(24).name == "PH1/OSC_OUT" and pin(8).name == "PC14/OSC32_IN"
    assert pin(25).name == "NRST" and pin(25).kind == "special"
    assert pin(138).name == "BOOT0" and pin(138).kind == "special"
    assert pin(143).name == "PDR_ON"
    for n in (6, 16, 17, 30, 31, 32, 33, 38, 39, 51, 52, 61, 62, 71, 72, 83, 84, 94, 95, 106, 107, 108, 120, 121,
              130, 131, 144):
        assert pin(n).kind == "power", n
    assert {pin(n).name for n in (31, 32, 33, 71, 106)} == {"VSSA", "VREF+", "VDDA", "VCAP_1", "VCAP_2"}
    # F407-only functions are not offered on the F405
    assert not any(v.startswith(("ETH_", "DCMI_")) for p in gpio for v in p.functions.values())


def test_memory_irqs_and_peripherals(desc):
    mem = {m.name: (m.base, m.size, m.kind) for m in desc.memory}
    assert mem["FLASH"] == (0x08000000, 0x100000, "flash")
    assert mem["SRAM"] == (0x20000000, 0x20000, "ram")
    assert mem["CCMRAM"] == (0x10000000, 0x10000, "ram")
    assert mem["BKPSRAM"] == (0x40024000, 0x1000, "ram")
    assert desc.extra["vtor"] == 0x08000000
    assert desc.core.clock_hz == 16e6 and desc.core.num_irqs == 82
    assert sorted(desc.irqs) == list(range(82))
    assert desc.irqs[6] == "EXTI0" and desc.irqs[28] == "TIM2" and desc.irqs[37] == "USART1"
    assert desc.irqs[4] == "FLASH" and desc.irqs[81] == "FPU"
    models = {p.name: p.model for p in desc.peripherals}
    assert models["RCC"] == "stm32.rcc" and models["EXTI"] == "stm32.exti" and models["IWDG"] == "stm32.iwdg"
    assert [models[f"GPIO{p}"] for p in "ABCDEFGHI"] == ["stm32.gpio"] * 9
    for n in ("USART1", "USART2", "USART3", "UART4", "UART5", "USART6"):
        assert models[n] == "stm32.usart"
    for n in ("TIM1", "TIM2", "TIM3", "TIM4", "TIM5", "TIM6", "TIM7", "TIM8"):
        assert models[n] == "stm32.tim"


@pytest.mark.skipif(not SVD, reason="STM32F405.svd not downloaded")
def test_svd_check_zero_mismatches():
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import svd2chip
    finally:
        sys.path.pop(0)
    out = io.StringIO()
    assert svd2chip.check(SVD[0], YAML, out=out) == 0, out.getvalue()


def test_chip_builds_without_stub_fallbacks(desc):
    chip = spin_chip(desc)
    assert not [w for w in warns(chip) if "not available" in w]
    import chipem.periph.stm32 as fam
    assert fam.IMPORT_ERRORS == {}
    assert chip.arch.reset_vtor == 0x08000000
    assert chip.core.pc == FLASH + 0x200
    assert chip.pins.get("PA9").id == "PA9" and chip.pins.gpio("A", 9).num == 101
    assert rd(chip, 0xE0042000) & 0xFFF == 0x413                 # DBGMCU_IDCODE DEV_ID


# ------------------------------------------------------------------------------------------------ RCC
def test_rcc_reset_hsi(desc):
    chip = spin_chip(desc)
    rcc = chip.peripherals["RCC"]
    assert rd(chip, RCC) & 3 == 3                                # HSION | HSIRDY
    assert rcc.core_hz == 16e6 and rcc.apb1_hz == 16e6 and rcc.apb2_hz == 16e6
    assert rcc.timer_hz(1) == 16e6 and chip.clock.hz == 16e6
    assert rd(chip, RCC + 0x74) >> 25 == 0b0111                  # CSR: BORRSTF | PINRSTF | PORRSTF


def test_rcc_hse_pll_168mhz(desc):
    chip = spin_chip(desc)
    rcc = chip.peripherals["RCC"]
    wr(chip, RCC, rd(chip, RCC) | 1 << 16)                       # HSEON
    assert rd(chip, RCC) & 1 << 17                               # HSERDY immediately
    wr(chip, RCC + 0x04, 8 | 336 << 6 | 0 << 16 | 1 << 22 | 7 << 24)   # M=8 N=336 P=2 HSE Q=7
    assert rcc.pll_hz() == 168e6 and rcc.pll_q_hz() == 48e6
    wr(chip, RCC + 0x08, 2)                                      # SW = PLL before PLLON: refused
    assert (rd(chip, RCC + 0x08) >> 2) & 3 == 0 and chip.clock.hz == 16e6
    assert any("refused" in w for w in warns(chip))
    wr(chip, RCC, rd(chip, RCC) | 1 << 24)                       # PLLON
    assert rd(chip, RCC) & 1 << 25                               # PLLRDY
    wr(chip, RCC + 0x08, 5 << 10 | 4 << 13 | 2)                  # PPRE1 /4, PPRE2 /2, SW = PLL
    assert (rd(chip, RCC + 0x08) >> 2) & 3 == 2                  # SWS = PLL
    assert rcc.core_hz == 168e6 and chip.clock.hz == 168e6
    assert rcc.apb1_hz == 42e6 and rcc.apb2_hz == 84e6
    assert rcc.timer_hz(1) == 84e6 and rcc.timer_hz(2) == 168e6
    wr(chip, RCC + 0x08, 5 << 10 | 4 << 13 | 0x9 << 4 | 2)       # HPRE /4
    assert rcc.core_hz == 42e6 and chip.clock.hz == 42e6 and rcc.apb1_hz == 10.5e6


def test_rcc_frozen_by_explicit_clock(desc):
    chip = spin_chip(desc, clock_hz=100e6)
    rcc = chip.peripherals["RCC"]
    assert rcc.frozen and chip.clock.hz == 100e6
    wr(chip, RCC + 0x08, 0x8 << 4)                               # HPRE /2
    assert rcc.core_hz == 8e6 and chip.clock.hz == 100e6
    assert rcc.effective_apb_hz(1) == 100e6


def test_rcc_gates_and_peripheral_reset(desc):
    chip = spin_chip(desc)
    rcc = chip.peripherals["RCC"]
    assert not rcc.clock_enabled("GPIOB") and not rcc.clock_enabled("USART1")
    wr(chip, GPIOB + 0x00, 1)                                    # gated off: warn once, write proceeds
    assert any("GPIOB accessed while its clock is disabled" in w for w in warns(chip))
    wr(chip, RCC + 0x30, 1 << 1)
    wr(chip, RCC + 0x44, 1 << 4)
    assert rcc.clock_enabled("GPIOB") and rcc.clock_enabled("USART1")
    assert rd(chip, GPIOB) & 3 == 1
    wr(chip, RCC + 0x10, 1 << 1)                                 # AHB1RSTR GPIOBRST
    assert rd(chip, GPIOB) == 0x280                              # SVD reset value of GPIOB_MODER


# ------------------------------------------------------------------------------------------------ GPIO
def gpio_on(chip):
    wr(chip, RCC + 0x30, 0x1FF)
    wr(chip, RCC + 0x44, 1 << 14 | 1 << 4)


def test_gpio_reset_state_swd_pins(desc):
    chip = spin_chip(desc)
    p = chip.pins.get
    assert (p("PA13").owner, p("PA13").pull) == ("JTMS-SWDIO", "up")
    assert (p("PA14").owner, p("PA14").pull) == ("JTCK-SWCLK", "down")
    assert (p("PA15").owner, p("PA15").pull) == ("JTDI", "up")
    assert p("PB3").owner == "JTDO/TRACESWO" and (p("PB4").owner, p("PB4").pull) == ("NJTRST", "up")
    assert (p("PA5").owner, p("PA5").direction, p("PA5").level()) == ("GPIO", "in", "X")


def test_gpio_modes_pulls_af_and_idr(desc):
    chip = spin_chip(desc)
    gpio_on(chip)
    pins = chip.pins
    # PB0 output, PB1 input pull-up, PB6 AF4 (I2C1_SCL), PB7 AF7 (USART1_RX), PB8 analog (no ADC fn -> "analog")
    wr(chip, GPIOB + 0x20, 4 << 24 | 7 << 28)                   # AFRL
    wr(chip, GPIOB + 0x24, 0)
    wr(chip, GPIOB + 0x0C, 1 << 2)                              # PUPDR: PB1 up (PB4 pull-up dropped)
    wr(chip, GPIOB + 0x00, 1 << 0 | 2 << 12 | 2 << 14 | 3 << 16)
    assert (pins.get("PB0").owner, pins.get("PB0").direction, pins.get("PB0").level()) == ("GPIO", "out", "L")
    assert (pins.get("PB1").direction, pins.get("PB1").pull, pins.get("PB1").level()) == ("in", "up", "H")
    assert pins.get("PB6").owner == "I2C1_SCL" and pins.get("PB7").owner == "USART1_RX"
    assert (pins.get("PB8").owner, pins.get("PB8").direction) == ("analog", "analog")
    assert pins.get("PB3").owner == "GPIO"                       # MODER rewrote PB3 to input
    assert rd(chip, GPIOB + 0x10) & 3 == 0b10                    # IDR: PB0 L, PB1 H (pull-up)
    wr(chip, GPIOB + 0x18, 1 << 0)                               # BSRR set PB0
    assert pins.get("PB0").level() == "H" and rd(chip, GPIOB + 0x14) & 1 and rd(chip, GPIOB + 0x18) == 0
    wr(chip, GPIOB + 0x18, 1 << 16 | 1 << 0)                     # set wins over reset
    assert pins.get("PB0").level() == "H"
    wr(chip, GPIOB + 0x18, 1 << 16)
    assert pins.get("PB0").level() == "L"
    wr(chip, GPIOB + 0x14, 1)                                    # ODR
    assert pins.get("PB0").level() == "H"
    pins.set_external("PB1", "L")                                # external drive beats the pull-up
    assert rd(chip, GPIOB + 0x10) & 3 == 0b01
    # analog-mode pins read 0 even when driven
    pins.set_external("PB8", "H")
    assert not rd(chip, GPIOB + 0x10) >> 8 & 1
    # AF mode with an AF number the pin does not have
    wr(chip, GPIOB + 0x24, 0xE)                                  # PB8 AF14: nothing
    wr(chip, GPIOB + 0x00, rd(chip, GPIOB) & ~(3 << 16) | 2 << 16)
    assert pins.get("PB8").owner == "disabled"
    assert any("AF14 selects no function" in w for w in warns(chip))
    # analog mode on an ADC pin is owned by the ADC channel
    wr(chip, GPIOA + 0x00, rd(chip, GPIOA) | 3 << 8)             # PA4
    assert pins.get("PA4").owner == "ADC12_IN4" and pins.get("PA4").direction == "analog"


def test_gpio_open_drain(desc):
    chip = spin_chip(desc)
    gpio_on(chip)
    pa1 = chip.pins.get("PA1")
    wr(chip, GPIOA + 0x04, 1 << 1)                               # OTYPER: PA1 open-drain
    wr(chip, GPIOA + 0x00, rd(chip, GPIOA) | 1 << 2)             # PA1 output, ODR=0 -> drives L
    assert (pa1.direction, pa1.level()) == ("out", "L")
    wr(chip, GPIOA + 0x18, 1 << 1)                               # ODR=1 -> released: floating
    assert (pa1.owner, pa1.direction, pa1.level()) == ("GPIO", "in", "X")
    wr(chip, GPIOA + 0x0C, rd(chip, GPIOA + 0x0C) | 1 << 2)      # pull-up
    assert pa1.level() == "H" and rd(chip, GPIOA + 0x10) >> 1 & 1
    chip.pins.set_external("PA1", "L")                           # another open-drain device pulls low
    assert not rd(chip, GPIOA + 0x10) >> 1 & 1
    wr(chip, GPIOA + 0x18, 1 << 17)                              # drive low again
    assert pa1.direction == "out" and pa1.drive == "L"


def test_gpio_floating_warning_and_lock(desc):
    chip = spin_chip(desc)
    gpio_on(chip)
    rd(chip, GPIOA + 0x10)
    assert not [w for w in warns(chip) if "floating" in w]       # untouched reset-state pins read 0 silently
    wr(chip, GPIOA + 0x00, rd(chip, GPIOA) | 1 << 4)             # PA2 output ...
    wr(chip, GPIOA + 0x00, rd(chip, GPIOA) & ~(3 << 4))          # ... back to input, floating
    assert rd(chip, GPIOA + 0x10) & 4 == 0
    assert [w for w in warns(chip) if "floating pin PA2" in w]
    # LCKR sequence locks PA2's configuration
    for v in (1 << 16 | 1 << 2, 1 << 2, 1 << 16 | 1 << 2):
        wr(chip, GPIOA + 0x1C, v)
    rd(chip, GPIOA + 0x1C)
    assert rd(chip, GPIOA + 0x1C) == 1 << 16 | 1 << 2
    wr(chip, GPIOA + 0x00, rd(chip, GPIOA) | 1 << 4 | 1 << 6)    # PA2 locked, PA3 not
    assert chip.pins.get("PA2").direction == "in" and chip.pins.get("PA3").direction == "out"
    assert any("locked" in w for w in warns(chip))


# ------------------------------------------------------------------------------------------------ EXTI
def test_exti_edges_and_irq_lines(desc):
    chip = spin_chip(desc)
    gpio_on(chip)
    wr(chip, GPIOA + 0x0C, 2)                                    # PA0 pull-down
    wr(chip, GPIOB + 0x0C, 2 << 10 | 2 << 26)                    # PB5, PB13 pull-down
    wr(chip, SYSCFG + 0x0C, 1 << 4)                              # EXTICR2: line 5 <- port B
    wr(chip, SYSCFG + 0x14, 1 << 4)                              # EXTICR4: line 13 <- port B
    wr(chip, EXTI + 0x08, 1 << 0 | 1 << 5)                       # RTSR lines 0, 5
    wr(chip, EXTI + 0x0C, 1 << 13)                               # FTSR line 13
    wr(chip, EXTI + 0x00, 1 << 0 | 1 << 13)                      # IMR lines 0, 13 (5 masked)
    pins = chip.pins
    pins.set_external("PA0", "H")
    assert rd(chip, EXTI + 0x14) == 1 and asserted(chip, 6)
    pins.set_external("PA0", "L")                                # falling: not selected
    assert rd(chip, EXTI + 0x14) == 1
    wr(chip, EXTI + 0x14, 1)                                     # w1c
    assert rd(chip, EXTI + 0x14) == 0 and not asserted(chip, 6)
    pins.set_external("PB5", "H")                                # masked line: no pending bit
    assert rd(chip, EXTI + 0x14) == 0 and not asserted(chip, 23)
    pins.set_external("PC13", "H")                               # port C not selected for line 13
    pins.set_external("PC13", "L")
    assert rd(chip, EXTI + 0x14) == 0
    pins.set_external("PB13", "H")
    pins.set_external("PB13", "L")
    assert rd(chip, EXTI + 0x14) == 1 << 13 and asserted(chip, 40)   # EXTI15_10
    wr(chip, EXTI + 0x14, 1 << 13)
    assert not asserted(chip, 40)
    wr(chip, EXTI + 0x10, 1 << 0)                                # SWIER line 0
    assert rd(chip, EXTI + 0x14) == 1 and asserted(chip, 6)
    wr(chip, EXTI + 0x14, 1)
    assert rd(chip, EXTI + 0x10) == 0                            # clearing PR clears SWIER


# ------------------------------------------------------------------------------------------------ USART
def test_usart_baud_frame_tx_rx_irq(desc):
    chip = spin_chip(desc)
    gpio_on(chip)
    u = chip.peripherals["USART1"]
    assert rd(chip, USART1) & 0xC0 == 0xC0                       # TXE | TC after reset
    wr(chip, USART1 + 0x08, 139)                                 # 16 MHz / 139 = 115,108 baud
    assert u.baud() == pytest.approx(16e6 / 139)
    wr(chip, USART1 + 0x0C, 1 << 15)                             # OVER8: BRR 0x116 = 34.75 -> 16e6/(8*34.75)
    wr(chip, USART1 + 0x08, 0x116)
    assert u.baud() == pytest.approx(16e6 / (8 * (0x11 + 6 / 8)))
    wr(chip, USART1 + 0x08, 139)
    wr(chip, USART1 + 0x0C, 1 << 13 | 1 << 3 | 1 << 2 | 1 << 5)  # UE TE RE RXNEIE
    assert u.frame() == "8N1" and u.char_bits() == 10
    assert u.char_ns() == pytest.approx(10 * 139 / 16e6 * 1e9)
    sent = []
    u.tx_listeners.append(lambda blk, b: sent.append(b))
    wr(chip, USART1 + 0x04, 0x41)
    assert bytes(u.output) == b"A" and sent == [0x41]
    assert rd(chip, USART1) & 0xC0 == 0                          # TXE/TC busy for one character
    chip.run_for(u.char_ns() + 1000)
    assert rd(chip, USART1) & 0xC0 == 0xC0
    assert [e.fields["byte"] for e in chip.log if e.kind == "uart" and e.fields.get("dir") == "tx"] == [0x41]
    # RX: nothing arrives while PA10 is not in AF7 (USART1_RX)
    u.feed(b"z")
    chip.run_for(u.char_ns() + 1000)
    assert not rd(chip, USART1) & 0x20
    assert [(e.get("byte"), e.get("reason")) for e in chip.log if e.kind == "uart" and e.get("dir") == "drop"] \
        == [(ord("z"), "pin_not_muxed")]
    wr(chip, GPIOA + 0x24, 7 << 8)                               # AFRH: PA10 AF7
    wr(chip, GPIOA + 0x00, rd(chip, GPIOA) | 2 << 20)            # MODER: PA10 alternate function
    # RX: one character time per byte, RXNE + IRQ 37 while unread
    u.feed(b"hi")
    assert not rd(chip, USART1) & 0x20
    chip.run_for(u.char_ns() + 1000)
    assert rd(chip, USART1) & 0x20 and asserted(chip, 37)
    assert rd(chip, USART1 + 0x04) == ord("h")
    assert not rd(chip, USART1) & 0x20 and not asserted(chip, 37)
    chip.run_for(u.char_ns() + 1000)
    assert rd(chip, USART1 + 0x04) == ord("i")
    chip.run_for(u.char_ns() + 1000)
    assert rd(chip, USART1) & 0x10                               # IDLE after the line went quiet
    rd(chip, USART1 + 0x04)                                      # SR then DR read clears IDLE
    assert not rd(chip, USART1) & 0x10
    # TCIE: TC with interrupt; TC is rc_w0
    wr(chip, USART1 + 0x0C, 1 << 13 | 1 << 3 | 1 << 6)
    assert asserted(chip, 37)
    wr(chip, USART1, ~(1 << 6) & 0xFFFF)
    assert not rd(chip, USART1) & 0x40 and not asserted(chip, 37)
    assert rd(chip, USART1) & 0x80                               # TXE unaffected (read-only)
    # 9-bit / parity / 2 stop bits framing
    wr(chip, USART1 + 0x10, 2 << 12)
    wr(chip, USART1 + 0x0C, 1 << 13 | 1 << 12 | 1 << 10)
    assert u.frame() == "8E2" and u.char_bits() == 12


def test_usart_rx_drop_reasons_and_overrun(desc):
    chip = spin_chip(desc)
    gpio_on(chip)
    u = chip.peripherals["USART1"]
    wr(chip, GPIOA + 0x24, 7 << 8)
    wr(chip, GPIOA + 0x00, rd(chip, GPIOA) | 2 << 20)            # PA10 = USART1_RX
    wr(chip, USART1 + 0x08, 139)
    wr(chip, USART1 + 0x0C, 1 << 13 | 1 << 3)                    # UE TE, RE off
    drops = lambda: [(chr(e.get("byte")), e.get("reason")) for e in chip.log   # noqa: E731
                     if e.kind == "uart" and e.get("dir") == "drop"]
    t = u.char_ns() + 1000
    u.feed(b"a")
    chip.run_for(t)
    wr(chip, RCC + 0x44, rd(chip, RCC + 0x44) & ~(1 << 4))       # USART1EN off
    u.feed(b"b")
    chip.run_for(t)
    wr(chip, RCC + 0x44, rd(chip, RCC + 0x44) | 1 << 4)
    wr(chip, RCC + 0x24, 1 << 4)                                 # APB2RSTR: USART1 held in reset
    u.feed(b"c")
    chip.run_for(t)
    wr(chip, RCC + 0x24, 0)
    assert drops() == [("a", "rx_disabled"), ("b", "clock_gated"), ("c", "reset")]
    # overrun: RXNE still set when the next stop bit arrives -> ORE, the new byte is lost, RDR keeps the old one
    wr(chip, USART1 + 0x08, 139)
    wr(chip, USART1 + 0x0C, 1 << 13 | 1 << 3 | 1 << 2)
    u.feed(b"xyz")
    chip.run_for(3 * u.char_ns() + 1000)
    assert rd(chip, USART1) & 0x28 == 0x28                       # RXNE | ORE
    assert drops()[3:] == [("y", "overrun"), ("z", "overrun")]
    assert rd(chip, USART1 + 0x04) == ord("x")                   # SR read, then DR read clears ORE and RXNE
    assert not rd(chip, USART1) & 0x28
    assert u.rx_dropped == 5 and not warns(chip)


def test_usart_apb1_clock_and_disabled_tx(desc):
    chip = spin_chip(desc)
    u2 = chip.peripherals["USART2"]
    wr(chip, RCC + 0x08, 4 << 10)                                # PPRE1 /2: PCLK1 8 MHz
    wr(chip, USART2 + 0x08, 0x45)
    assert u2.module_hz() == 8e6 and u2.baud() == pytest.approx(8e6 / (16 * (4 + 5 / 16)))
    wr(chip, USART2 + 0x04, 0x55)                                # UE/TE off: ignored
    assert bytes(u2.output) == b"" and any("transmitter disabled" in w for w in warns(chip))
    assert chip.peripherals["UART4"].apb == 1 and chip.peripherals["USART6"].apb == 2


# ------------------------------------------------------------------------------------------------ TIM
def test_tim_period_irq_and_cnt(desc):
    chip = spin_chip(desc)
    t2 = chip.peripherals["TIM2"]
    wr(chip, TIM2 + 0x28, 15)                                    # PSC: 1 MHz counter
    wr(chip, TIM2 + 0x2C, 999)                                   # ARR: 1 ms
    wr(chip, TIM2 + 0x14, 1)                                     # UG: loads PSC/ARR, sets UIF
    assert rd(chip, TIM2 + 0x10) & 1
    wr(chip, TIM2 + 0x10, 0)
    wr(chip, TIM2 + 0x0C, 1)                                     # UIE
    wr(chip, TIM2 + 0x00, 1)                                     # CEN
    chip.run_for(0.5e6)
    assert rd(chip, TIM2 + 0x24) == pytest.approx(500, abs=3)    # live CNT
    assert not asserted(chip, 28)
    chip.run_for(0.6e6)
    assert rd(chip, TIM2 + 0x10) & 1 and asserted(chip, 28) and t2.updates == 1
    wr(chip, TIM2 + 0x10, ~1 & 0xFFFF)                           # rc_w0
    assert not asserted(chip, 28)
    chip.run_for(10e6)
    assert t2.updates == 11
    assert t2.period_ns() == pytest.approx(1e6)
    # TIM2 is 32-bit, TIM3 16-bit
    assert chip.peripherals["TIM2"].bits == 32 and chip.peripherals["TIM3"].bits == 16


def test_tim_preload_downcount_opm_and_clock_change(desc):
    chip = spin_chip(desc)
    t6 = chip.peripherals["TIM6"]
    t3 = chip.peripherals["TIM3"]
    wr(chip, TIM6 + 0x28, 15)
    wr(chip, TIM6 + 0x2C, 99)
    wr(chip, TIM6 + 0x14, 1)
    wr(chip, TIM6 + 0x00, 1 | 1 << 3)                            # CEN | OPM
    chip.run_for(150e3)
    assert t6.updates == 1 and not rd(chip, TIM6) & 1            # one overflow, then OPM cleared CEN
    # down-counting TIM3 with ARR preload: a new ARR applies at the next update
    wr(chip, TIM3 + 0x28, 15)
    wr(chip, TIM3 + 0x2C, 199)
    wr(chip, TIM3 + 0x14, 1)
    wr(chip, TIM3 + 0x00, 1 | 1 << 4 | 1 << 7)                   # CEN | DIR | ARPE
    chip.run_for(50e3)
    assert rd(chip, TIM3 + 0x24) == pytest.approx(150, abs=3)
    wr(chip, TIM3 + 0x2C, 499)
    n = t3.updates
    chip.run_for(160e3)
    assert t3.updates == n + 1 and t3.arr == 499
    # a core clock change re-derives the counter clock (timer clock = PCLK1 = HCLK here)
    wr(chip, RCC + 0x08, 0x8 << 4)                               # HPRE /2: 8 MHz
    assert t3.counter_hz() == pytest.approx(0.5e6)
    assert t3.period_ns() == pytest.approx(1e6)


# ------------------------------------------------------------------------------------------------ IWDG etc.
def test_iwdg_timeout_resets_chip(desc):
    chip = spin_chip(desc)
    wr(chip, IWDG + 0x04, 3)                                     # locked: ignored
    assert rd(chip, IWDG + 0x04) == 0 and any("without the 0x5555" in w for w in warns(chip))
    wr(chip, IWDG, 0x5555)
    wr(chip, IWDG + 0x04, 0)                                     # /4 -> 8 kHz
    wr(chip, IWDG + 0x08, 79)                                    # 80 ticks = 10 ms
    wr(chip, IWDG, 0xCCCC)
    iw = chip.peripherals["IWDG"]
    assert iw.timeout_ns == pytest.approx(10e6)
    for _ in range(3):                                           # reloads keep it alive
        chip.run_for(8e6)
        wr(chip, IWDG, 0xAAAA)
    assert iw.timeouts == 0
    chip.run_for(11e6)
    assert iw.timeouts == 1
    resets = [e for e in chip.log if e.kind == "reset"]
    assert resets[-1].fields.get("reason") == "watchdog"
    assert rd(chip, RCC + 0x74) & 1 << 29                         # CSR.IWDGRSTF
    assert not iw.running
    wr(chip, RCC + 0x74, 1 << 24)                                # RMVF clears the flags
    assert rd(chip, RCC + 0x74) >> 24 == 0


def test_flash_pwr_dbgmcu(desc):
    chip = spin_chip(desc)
    wr(chip, FLASH_IF, 0x705)                                    # ACR: LATENCY 5, PRFTEN, ICEN, DCEN
    assert rd(chip, FLASH_IF) == 0x705 and chip.peripherals["FLASH"].latency == 5
    assert rd(chip, FLASH_IF + 0x10) >> 31                       # CR.LOCK
    wr(chip, FLASH_IF + 0x04, 0x45670123)
    wr(chip, FLASH_IF + 0x04, 0xCDEF89AB)
    assert not rd(chip, FLASH_IF + 0x10) >> 31
    chip.bus.write_bytes(0x08020000, b"\x00" * 16)               # sector 5 (128 KB at 0x08020000)
    wr(chip, FLASH_IF + 0x10, 1 << 1 | 5 << 3 | 1 << 24 | 1 << 16)   # SER, SNB=5, EOPIE, STRT
    assert chip.bus.read_bytes(0x08020000, 16) == b"\xff" * 16
    assert chip.bus.read_bytes(0x08000000, 4) != b"\xff" * 4      # vector table untouched
    assert rd(chip, FLASH_IF + 0x0C) & 1                         # EOP
    wr(chip, FLASH_IF + 0x0C, 1)
    assert not rd(chip, FLASH_IF + 0x0C) & 1
    assert rd(chip, PWR + 0x04) & 1 << 14                        # VOSRDY
    wr(chip, PWR, 1 << 14)
    assert rd(chip, PWR) & 1 << 14
