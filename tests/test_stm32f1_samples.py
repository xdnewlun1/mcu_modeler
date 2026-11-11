"""End-to-end runs of the STM32F103T6 samples (firmware/samples_stm32f1/build/*.elf) and boards, with timings.
Skipped when the samples are not built (firmware/samples_stm32f1/build.sh)."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import load_firmware
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / "samples_stm32f1" / "build"
YAML = ROOT / "chips" / "stm32f103t6.yaml"
F405_BUILD = ROOT / "firmware" / "samples_stm32" / "build"


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def elf(name: str) -> Path:
    p = BUILD / f"{name}.elf"
    if not p.exists():
        pytest.skip(f"{p} not built (firmware/samples_stm32f1/build.sh)")
    return p


def boot(desc, name: str, *pins: str) -> tuple[Chip, dict]:
    chip = Chip(desc)
    chip.load_firmware(load_firmware(elf(name)))
    chip.reset()
    ev: dict = {p: [] for p in pins}
    chip.pins.subscribe(lambda e: ev[e.pin].append((e.t_ns, e.new)) if e.pin in ev and e.field == "level" else None)
    return chip, ev


def sym(name: str, sample: str) -> int:
    from elftools.elf.elffile import ELFFile
    with open(elf(sample), "rb") as f:
        return ELFFile(f).get_section_by_name(".symtab").get_symbol_by_name(name)[0]["st_value"]


def u32(chip, sample, name):
    return chip.bus.read(sym(name, sample), 4)


def warns(chip):
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def test_blinky(desc):
    chip, ev = boot(desc, "blinky", "PB0")
    chip.run_for(12.5e6)
    assert chip.clock.hz == 8e6 and chip.pins.get("PB0").owner == "GPIO"
    t = [x[0] for x in ev["PB0"]]
    assert len(t) >= 11
    gaps = [b - a for a, b in zip(t[1:], t[2:])]
    assert all(abs(g - 1e6) < 1e3 for g in gaps), gaps         # 1 ms +- 1 us (SysTick, 8000 HCLK)
    assert not warns(chip)


def test_uart_echo_72mhz(desc):
    chip, _ = boot(desc, "uart_echo")
    chip.run_for(1.9e6)
    assert chip.clock.hz == 72e6
    u = chip.peripherals["USART1"]
    assert bytes(u.output) == b"STM32F103T6 72 MHz\r\n" and u.baud() == pytest.approx(115200, rel=1e-3)
    u.feed(b"hi")
    chip.run_for(1e6)
    assert bytes(u.output).endswith(b"HI")
    tx = [e.t_ns for e in chip.log if e.kind == "uart" and e.fields.get("dir") == "tx"]
    assert abs((tx[1] - tx[0]) - 10e9 / 115200) < 200            # back-to-back characters: one frame apart
    assert u32(chip, "uart_echo", "rx_count") == 2
    assert not warns(chip)


def test_button_exti(desc):
    chip, ev = boot(desc, "button_exti", "PB1")
    chip.run_for(0.5e6)
    assert chip.pins.get("PA0").pull == "up" and chip.pins.get("PB1").level() == "H"
    for k, lvl in enumerate((0, 1, 0, 1)):
        t0 = chip.now_ns()
        chip.pins.set_external("PA0", lvl)
        chip.run_for(0.5e6)
        assert ev["PB1"] and ev["PB1"][-1][1] == ("H" if lvl else "L")
        assert ev["PB1"][-1][0] - t0 < 10e3                       # ISR latency well under 10 us at 8 MHz
    assert u32(chip, "button_exti", "presses") == 2 and u32(chip, "button_exti", "edges") == 4


def test_flash_prog(desc):
    chip, _ = boot(desc, "flash_prog")
    chip.run_for(40e6)
    assert u32(chip, "flash_prog", "status") == 0x600D
    assert u32(chip, "flash_prog", "pgerr") == 1
    cyc = u32(chip, "flash_prog", "erase_cycles")
    assert abs(cyc / 8e6 - 30e-3) < 0.2e-3                         # page erase 30 ms (DS15060 tERASE 20-40 ms)
    assert chip.core.read_mem(0x08007C00, 8) == bytes.fromhex("3412cdab0000aa55")
    assert bytes(chip.peripherals["USART1"].output) == b"FLASH OK\r\n"


def test_rtc_alarm(desc):
    chip, ev = boot(desc, "rtc_alarm", "PB0", "PB1")
    chip.run_for(3.2e9)
    assert u32(chip, "rtc_alarm", "seconds") == 3 and u32(chip, "rtc_alarm", "alarms") == 1
    t = [x[0] for x in ev["PB0"] if x[0] > 0.5e9]                 # second ticks (not the output enable at boot)
    gaps = [b - a for a, b in zip(t, t[1:])]
    assert len(t) == 3 and all(abs(g - 1e9) < 1e3 for g in gaps)   # 32768 / (32767 + 1) = 1 Hz
    alarm = ev["PB1"][-1]
    assert alarm[1] == "H" and abs(alarm[0] - t[1]) < 50e3        # ALR = 2: with the second tick (CNT 1 -> 2)
    assert chip.bus.read(0x40006C04, 4) == 1                        # BKP_DR1 boot counter


def test_stop_exti(desc):
    chip, _ = boot(desc, "stop_exti")
    chip.run_for(15e6)
    pwr = chip.peripherals["PWR"]
    assert pwr.mode == "stop" and chip.sleeping and pwr.entries["stop"] == 1
    assert chip.clock.hz == 8e6                                     # HSE/PLL stopped, SYSCLK = HSI
    chip.pins.set_external("PA0", 0)
    chip.run_for(1e6)
    chip.pins.set_external("PA0", 1)
    chip.run_for(2e6)
    assert u32(chip, "stop_exti", "wakes") == 1 and u32(chip, "stop_exti", "sws_after_wake") == 0
    assert chip.clock.hz == 72e6
    assert b"WAKE 1\r\n" in bytes(chip.peripherals["USART1"].output)


def test_can_loopback(desc):
    chip, _ = boot(desc, "can_loopback")
    chip.run_for(5.5e6)
    assert u32(chip, "can_loopback", "received") >= 4 and u32(chip, "can_loopback", "bad") == 0
    assert u32(chip, "can_loopback", "received") == u32(chip, "can_loopback", "sent")
    assert chip.peripherals["CAN"].bitrate() == pytest.approx(500e3)
    assert chip.pins.get("PA12").owner == "CAN_TX" and chip.pins.get("PA12").level() == "H"   # silent: TX recessive


def test_spi_flash(desc):
    elf("spi_flash")
    s = Session(YAML, elf("spi_flash"), quiet=True)
    s.attach_spi_flash("SPI1:cs=PA4")
    s.run_for(10e6)
    out = s.uart_output("USART1")
    assert b"JEDEC " in out and b"TEXT CHIP_EM\r\ndone\r\n" in out


def board(name):
    p = ROOT / "boards" / name
    return Session.from_board(p, quiet=True)


def test_board_i2c_eeprom():
    elf("i2c_eeprom")
    s = board("stm32f103_i2c_eeprom.yaml")
    s.run_for(20e6)
    assert b"EEPROM: Hello F103!\r\n" in s.uart_output("A.USART1")


def test_board_pwm_capture():
    elf("tim_pwm_capture")
    s = board("stm32f103_pwm_capture.yaml")
    s.run_for(6e6)
    chip = s.chips["A"]
    assert u32(chip, "tim_pwm_capture", "period_us") == 1000 and u32(chip, "tim_pwm_capture", "high_us") == 250
    assert u32(chip, "tim_pwm_capture", "captures") >= 4
    assert chip.pins.get("PA1").owner == "TIM2_CH2" and chip.pins.get("PA6").owner == "TIM3_CH1"


def test_board_uart_with_f405():
    elf("uart_link_sender")
    if not (F405_BUILD / "uart_link_echo.elf").exists():
        pytest.skip("STM32F405 samples not built")
    s = board("stm32f103_stm32f405_uart.yaml")
    s.run_for(10.5e6)
    assert s.uart_output("B.USART1").startswith(b"PING 1\r\nPING 2\r\n")
    assert u32(s.chips["A"], "uart_link_sender", "replies") >= 4


def test_board_can_with_f405():
    elf("can_node")
    if not (F405_BUILD / "can_echo.elf").exists():
        pytest.skip("STM32F405 samples not built")
    s = board("stm32f103_stm32f405_can.yaml")
    s.run_for(35e6)
    a = s.chips["A"]
    assert u32(a, "can_node", "echoes") == 3 and u32(a, "can_node", "bad") == 0
    assert b"RX 100 [4] 00 02 46 31" in s.uart_output("B.USART1")
