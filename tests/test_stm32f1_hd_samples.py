"""End-to-end runs of the STM32F103 package-variant samples (firmware/samples_stm32f1_variants/build/*.elf) on the
medium-density STM32F103C8 ("Blue Pill") and the high-density STM32F103ZE/VE/RC, plus the boards
stm32f103c8_stm32f405_uart, stm32f103ze_{spi_i2c,sdcard,fsmc_sram}.  Skipped when the samples are not built
(firmware/samples_stm32f1_variants/build.sh)."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import load_firmware
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / "samples_stm32f1_variants" / "build"
F405_BUILD = ROOT / "firmware" / "samples_stm32" / "build"


def elf(name: str) -> Path:
    p = BUILD / f"{name}.elf"
    if not p.exists():
        pytest.skip(f"{p} not built (firmware/samples_stm32f1_variants/build.sh)")
    return p


def boot(part: str, name: str, *pins: str) -> tuple[Chip, dict]:
    chip = Chip(load_chip(ROOT / "chips" / f"{part}.yaml"))
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


def period_ns(ev, pin):
    t = [x[0] for x in ev[pin] if x[1] == "H"]
    return [b - a for a, b in zip(t, t[1:])]


def test_bluepill_c8():
    chip, ev = boot("stm32f103c8", "bluepill", "PC13")
    chip.run_for(3.5e6)
    assert chip.clock.hz == 72e6
    u = chip.peripherals["USART1"]
    assert bytes(u.output).startswith(b"Blue Pill ID 20036410 FLASH 64K\r\n")      # RM0008 31.6.1, F_SIZE 64 KB
    assert u.baud() == pytest.approx(115200, rel=1e-3)
    chip.run_for(10e6)
    out = bytes(u.output)
    assert b"ping 0\r\nping 1\r\nping 2\r\n" in out
    tx = [e.t_ns for e in chip.log if e.kind == "uart" and e.fields.get("dir") == "tx" and e.fields.get("byte") == ord("p")]
    gaps = [b - a for a, b in zip(tx, tx[1:])]
    assert gaps and all(abs(g - 2e6) < 20e3 for g in gaps), gaps                    # a ping every 2 ms (SysTick)
    t13 = [x[0] for x in ev["PC13"]][1:]                                            # [0]: configured as output
    assert len(t13) >= 2 and all(abs((b - a) - 5e6) < 20e3 for a, b in zip(t13, t13[1:]))   # LED: 5 ms
    assert 9 <= u32(chip, "bluepill", "tim4_ticks") <= 11                         # TIM4 1 kHz for ~10 ms
    u.feed(b"x\n")
    chip.run_for(1e6)
    assert u32(chip, "bluepill", "lines") == 1 and u32(chip, "bluepill", "rx_bytes") == 2
    assert not warns(chip)


def test_bluepill_runs_on_other_medium_parts():
    for part, kb in (("stm32f103cb", 128), ("stm32f103rb", 128), ("stm32f103v8", 64)):
        chip, _ = boot(part, "bluepill")
        chip.run_for(3.5e6)
        assert bytes(chip.peripherals["USART1"].output).startswith(f"Blue Pill ID 20036410 FLASH {kb}K".encode())


def test_hd_uart_tim_ze():
    chip, ev = boot("stm32f103ze", "hd_uart_tim", "PC6", "PB6", "PE0", "PF0", "PG0")
    chip.run_for(16e6)
    out = bytes(chip.peripherals["USART1"].output)
    assert out == b"HD ID 10036414 FLASH 512K\r\nT5 5 T6 10 T7 20\r\ndone\r\n"
    assert bytes(chip.peripherals["USART3"].output) == b"USART3\r\n"
    assert bytes(chip.peripherals["UART4"].output) == b"UART4\r\n"
    assert bytes(chip.peripherals["UART5"].output) == b"UART5\r\n"
    assert chip.peripherals["UART4"].baud() == pytest.approx(115200, rel=2e-3)     # BRR 313 from 36 MHz
    p8 = period_ns(ev, "PC6")
    assert len(p8) > 50 and all(abs(p - 100e3) < 100 for p in p8[1:])               # TIM8 PWM 10 kHz
    hi = [b[0] - a[0] for a, b in zip(ev["PC6"], ev["PC6"][1:]) if a[1] == "H"]
    assert all(abs(h - 25e3) < 100 for h in hi[1:])                                 # 25 % duty
    p4 = period_ns(ev, "PB6")
    assert p4 and all(abs(p - 1e6) < 1e3 for p in p4)                               # TIM4 PWM 1 kHz
    for p in ("PE0", "PF0", "PG0"):                                                 # TIM6 toggles every 1 ms
        t = [x[0] for x in ev[p]]
        assert len(t) >= 10 and all(abs((b - a) - 1e6) < 1e3 for a, b in zip(t, t[1:]))
    assert chip.pins.get("PC6").owner == "TIM8_CH1" and chip.pins.get("PC10").owner == "UART4_TX"
    chip.peripherals["UART4"].feed(b"abc")
    chip.run_for(1e6)
    assert bytes(chip.peripherals["UART4"].output).endswith(b"ABC")
    assert u32(chip, "hd_uart_tim", "u4_rx") == 3
    assert not warns(chip)


def test_hd_uart_tim_on_ve_and_rc():
    for part in ("stm32f103ve", "stm32f103rc"):
        chip, _ = boot(part, "hd_uart_tim")
        chip.run_for(16e6)
        kb = 512 if part.endswith("e") else 256
        assert bytes(chip.peripherals["USART1"].output).startswith(f"HD ID 10036414 FLASH {kb}K\r\n".encode())
        assert b"done" in bytes(chip.peripherals["USART1"].output)


def test_hd_dac_adc():
    elf("hd_dac_adc")
    s = Session(ROOT / "chips" / "stm32f103ze.yaml", elf("hd_dac_adc"), quiet=True)
    s.set_adc("PF6", 1.0)
    s.set_adc("PA0", 2.0)
    s.run_for(7e6)
    out = s.uart_output("USART1")
    assert out.startswith(b"DAC 2048 ADC1 2048\r\n")                             # DAC_OUT1 read back on ADC12_IN4
    assert b"ADC3 1241 2482\r\ndone\r\n" in out                                    # 1.0 V / 2.0 V of 3.3 V
    wave = int(out.split(b"WAVE ")[1].split(b"\r")[0])
    assert 4 <= wave <= 6                                                          # 10 kHz / 4 entries for ~2 ms
    dac = s.chip.peripherals["DAC"]
    assert dac.conversions[0] >= 16


def board(name):
    return Session.from_board(ROOT / "boards" / name, quiet=True)


def test_board_bluepill_with_f405():
    elf("bluepill")
    if not (F405_BUILD / "uart_link_echo.elf").exists():
        pytest.skip("STM32F405 samples not built")
    s = board("stm32f103c8_stm32f405_uart.yaml")
    s.run_for(12e6)
    assert s.uart_output("B.USART1").startswith(b"BLUE PILL ID 20036410 FLASH 64K\r\nPING 0\r\nPING 1\r\n")
    assert u32(s.chips["A"], "bluepill", "lines") >= 4


def test_board_spi_i2c():
    elf("hd_spi_i2c")
    s = board("stm32f103ze_spi_i2c.yaml")
    s.run_for(20e6)
    assert s.uart_output("A.USART1") == b"JEDEC EF4016\r\nEEPROM HD I2C2\r\nSPI3 A5 3C\r\ndone\r\n"
    assert u32(s.chips["A"], "hd_spi_i2c", "polls") >= 1


def test_board_sdcard():
    elf("hd_sdio")
    s = board("stm32f103ze_sdcard.yaml")
    s.run_for(30e6)
    out = s.uart_output("A.USART1")
    assert b"SD v2 SDHC" in out and b"READ ok CHIP_EM SDIO block\r\nDMA ok\r\ndone\r\n" in out
    assert u32(s.chips["A"], "hd_sdio", "result") == 1


def test_board_fsmc_sram():
    elf("hd_fsmc")
    s = board("stm32f103ze_fsmc_sram.yaml")
    s.run_for(20e6)
    assert s.uart_output("A.USART1") == (b"FSMC BCR1 00001091\r\nSRAM ok\r\nMIRROR ok\r\nEXEC 42\r\nFAULT ok\r\n"
                                         b"done\r\n")
