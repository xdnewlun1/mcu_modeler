"""STM32F446 samples: firmware/samples_stm32f446/qspi_flash.c (STM32F446RE, dual I/O; board
boards/stm32f446re_qspi_flash.yaml), qspi_quad.c (STM32F446ZE, quad I/O) and the STM32CubeF4 HAL sample
firmware/samples_stm32_hal_f446/qspi_hal.c (HAL_RCC with PLL_R / CLK48 from PLLSAI_P, HAL_QSPI).  Skipped when the
samples are not built (build.sh)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / "samples_stm32f446" / "build"
HAL_ELF = ROOT / "firmware" / "samples_stm32_hal_f446" / "build" / "qspi_hal.elf"
BOARD = ROOT / "boards" / "stm32f446re_qspi_flash.yaml"
CHIPS = ROOT / "chips"

EXPECT = (b"F446 180MHz PLLR\r\nID EF4018\r\nerase 45000us\r\nprog 400us\r\nread: QSPI on F446 ok!\r\n"
          b"dma: QSPI on F446 ok!\r\nmm: QSPI on F446 ok!\r\nirqs 2\r\ndone\r\n")


def need(p: Path) -> Path:
    if not p.exists():
        pytest.skip(f"{p.relative_to(ROOT)} not built")
    return p


def test_board_qspi_dual_rerun():
    need(BUILD / "qspi_flash.elf")
    s = Session.from_board(BOARD, quiet=True)
    c = s.chips["A"]
    s.run_for(70e6)
    assert c.clock.hz == 180e6 and c.peripherals["RCC"].source == "PLL_R"
    assert s.uart_output("A.USART2") == EXPECT
    q = c.peripherals["QUADSPI"]
    dev = q.flash[1]
    assert bytes(dev.mem[0x1000:0x1010]) == b"QSPI on F446 ok!"
    assert [op for op, _ in dev.ops] == ["erase4k", "program"]
    assert q.mm == "fast"                                       # 0x3B dual output, 8 dummy: array mapped directly
    assert c.arch.entries == 2                                  # the two SMF interrupts (IRQ 92) of wait_ready()
    owners = {p: c.pins.get(p).owner for p in ("PB2", "PB6", "PC9", "PC10")}
    assert owners == {"PB2": "QUADSPI_CLK", "PB6": "QUADSPI_BK1_NCS", "PC9": "QUADSPI_BK1_IO0",
                      "PC10": "QUADSPI_BK1_IO1"}
    assert c.pins.get("PA5").level() == "H"
    # nCS (PB6) goes low for every indirect command and the two polling phases: ID, WREN, erase, WREN, program,
    # read, DMA read + 2 polls = 9 low pulses, plus memory-mapped mode, where nCS stays low after the first access
    # (prefetch, RM0390 12.3.7) until a timeout or an abort = 10
    ncs = [e.new for e in s.events(kinds={"pin"}, chip="A") if e.pin == "PB6" and e.field == "level"]
    assert ncs.count("L") == 10
    assert not [e for e in s.events(kinds={"warn", "fault"})]
    s.close()


def test_quad_sample_on_ze():
    elf = need(BUILD / "qspi_quad.elf")
    s = Session(str(CHIPS / "stm32f446ze.yaml"), str(elf))
    s.attach_spi_flash("QUADSPI:jedec=0xEF4018")
    s.run_for(70e6)
    out = s.uart_output("USART2")
    assert out.startswith(b"F446 180MHz PLLR\r\nID EF4018\r\nerase 45001us\r\nprog 401us\r\n")    # + QE write
    assert b"read: QSPI on F446 ok!\r\ndma: QSPI on F446 ok!\r\nmm: QSPI on F446 ok!\r\nirqs 3\r\ndone\r\n" in out
    q = s.chip.peripherals["QUADSPI"]
    assert q.flash[1].sr2 & 2 and q.mm == "fast"
    assert [op for op, _ in q.flash[1].ops] == ["write_status2", "erase4k", "program"]
    assert not [e for e in s.events(kinds={"warn", "fault"})]


def test_hal_qspi_sample():
    elf = need(HAL_ELF)
    s = Session(str(CHIPS / "stm32f446re.yaml"), str(elf))
    s.attach_spi_flash("QUADSPI:jedec=0xEF4018")
    s.run_for(60e6)
    assert s.uart_output("USART2") == (b"HAL F446 SYSCLK 0ABA9500\r\nID EF4018\r\nread: HAL QSPI on F446\r\n"
                                        b"mm: HAL QSPI on F446\r\ndone\r\n")
    r = s.chip.peripherals["RCC"]
    assert r.source == "PLL_R" and r.clk48_hz() == 48e6               # CK48MSEL = PLLSAI_P (PLLSAIM 4, N 96, P /4)
    assert not [e for e in s.events(kinds={"warn", "fault"})]


def test_board_cli():
    need(BUILD / "qspi_flash.elf")
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "run", "--board", str(BOARD), "--for", "70ms",
                        "--summary-only"], capture_output=True, text=True, cwd=ROOT, timeout=180)
    assert r.returncode == 0, r.stderr
    assert "mm: QSPI on F446 ok!" in r.stdout and "IRQ92 QUADSPI" in r.stdout
