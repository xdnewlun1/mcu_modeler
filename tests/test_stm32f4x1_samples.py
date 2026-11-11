"""STM32F401/F411 samples (firmware/samples_stm32f4x1, firmware/samples_stm32_hal_f4x1) and boards
(boards/stm32f411re_f401re_uart.yaml, boards/stm32f411_i2s5_codec.yaml): clock trees (84 / 100 MHz from HSE 8 MHz),
USART2 banner + echo, TIM2 1 ms period before and after DCKCFGR.TIMPRE, a UART request/response link F411 <-> F401 with
timings, I2S5 on the F411 with the PLLI2SM divider, and the STM32CubeF4 HAL for STM32F401xE / STM32F411xE.
Tests skip when the samples are not built (firmware/samples_stm32f4x1/build.sh, firmware/samples_stm32_hal_f4x1/build.sh)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
CHIPS = ROOT / "chips"
BUILD = ROOT / "firmware" / "samples_stm32f4x1" / "build"
HAL_BUILD = ROOT / "firmware" / "samples_stm32_hal_f4x1" / "build"
LINK_BOARD = ROOT / "boards" / "stm32f411re_f401re_uart.yaml"
I2S_BOARD = ROOT / "boards" / "stm32f411_i2s5_codec.yaml"


def need(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"{path.relative_to(ROOT)} not built")
    return path


def sym(elf: Path, symbol: str) -> int:
    from elftools.elf.elffile import ELFFile
    with open(elf, "rb") as f:
        return ELFFile(f).get_section_by_name(".symtab").get_symbol_by_name(symbol)[0]["st_value"]


@pytest.mark.parametrize("fn,elf,hz,banner", [
    ("stm32f401re", "f401_clock_uart", 84e6, b"hello F401 84 MHz\r\n"),
    ("stm32f401cc", "f401_clock_uart", 84e6, b"hello F401 84 MHz\r\n"),
    ("stm32f411re", "f411_clock_uart", 100e6, b"hello F411 100 MHz\r\n"),
    ("stm32f411ce", "f411_clock_uart", 100e6, b"hello F411 100 MHz\r\n"),
])
def test_clock_uart(fn, elf, hz, banner):
    s = Session(CHIPS / f"{fn}.yaml", need(BUILD / f"{elf}.elf"))
    chip = s.chip
    edges: list[float] = []
    chip.pins.subscribe(lambda e: edges.append(e.t_ns) if e.pin == "PA5" and e.field == "level" else None)
    s.schedule_uart(14e6, "USART2", b"abc")
    s.run_for(15e6)
    assert chip.clock.hz == hz
    rcc = chip.peripherals["RCC"]
    assert (rcc.apb1_hz, rcc.apb2_hz) == (hz / 2, hz)
    assert rd_dck(chip) & (1 << 24)                          # TIMPRE set by the firmware after 10 ticks
    assert s.uart_output("USART2") == banner + b"ABC"
    u = chip.peripherals["USART2"]
    assert u.baud() == pytest.approx(115200, rel=2e-3)
    # PA5 toggles every 1 ms (TIM2: timer clock HCLK, PSC = MHz - 1, ARR 999), unchanged by TIMPRE (APB1 /2)
    per = [b - a for a, b in zip(edges[1:], edges[2:])]
    assert len(per) >= 12 and all(p == pytest.approx(1e6, rel=1e-4) for p in per)
    assert chip.peripherals["TIM2"].get("PSC") == hz / 1e6 - 1
    assert not [e for e in s.events(kinds={"warn", "fault"})]
    s.close()


def rd_dck(chip) -> int:
    return chip.bus.read(0x40023800 + 0x8C, 4)


def test_f401_firmware_on_f411_package_and_vice_versa_limits():
    """The F411 image (100 MHz, scale 1) on an F401: VOS 11 is scale 2 there and 100 MHz is above the 84 MHz limit."""
    s = Session(CHIPS / "stm32f401re.yaml", need(BUILD / "f411_clock_uart.elf"))
    s.run_for(1e6)
    w = [e.fields.get("msg", "") for e in s.events(kinds={"warn"})]
    assert any("exceeds the 84 MHz maximum" in m for m in w), w
    s.close()


# ------------------------------------------------------------------------------------------------ UART link board
def board_session() -> Session:
    need(BUILD / "f411_link_requester.elf")
    need(BUILD / "f401_link_responder.elf")
    return Session.from_board(LINK_BOARD, quiet=True)


def test_board_f411_f401_request_response():
    s = board_session()
    a, b = s.chips["A"], s.chips["B"]
    assert (a.desc.name, b.desc.name) == ("STM32F411RE", "STM32F401RE")
    s.run_for(21e6)
    assert a.clock.hz == 100e6 and b.clock.hz == 84e6
    ua, ub = a.peripherals["USART1"], b.peripherals["USART6"]
    assert ua.baud() == pytest.approx(100e6 / 434) and ub.baud() == pytest.approx(84e6 / 365)
    out_a, out_b = s.uart_output("A.USART1"), s.uart_output("B.USART6")
    assert out_a.startswith(b"REQ 1\r\nREQ 2\r\nREQ 3\r\n")
    assert out_b.startswith(b"ACK 1 1\r\nACK 2 4\r\nACK 3 9\r\n") and b"ACK 10 100\r\n" in out_b
    req_elf, rsp_elf = BUILD / "f411_link_requester.elf", BUILD / "f401_link_responder.elf"
    assert a.bus.read(sym(req_elf, "req_n"), 4) == 10
    assert a.bus.read(sym(req_elf, "acks_ok"), 4) == 10 and a.bus.read(sym(req_elf, "ack_errors"), 4) == 0
    assert b.bus.read(sym(rsp_elf, "requests"), 4) == 10 and b.bus.read(sym(rsp_elf, "bad_lines"), 4) == 0
    assert a.pins.get("PA9").owner == "USART1_TX" and a.pins.get("PA10").owner == "USART1_RX"
    assert b.pins.get("PA11").owner == "USART6_TX" and b.pins.get("PA12").owner == "USART6_RX"
    req = [e.t_ns for e in s.events(kinds={"uart"}, chip="A") if e.fields.get("dir") == "tx" and e.byte == ord("R")]
    assert len(req) == 10 and all(y - x == pytest.approx(2e6, rel=1e-3) for x, y in zip(req, req[1:]))
    led_b = [e.t_ns for e in s.events(kinds={"pin"}, chip="B") if e.pin == "PA5" and e.field == "level"]
    assert len(led_b) == 1 + 10
    # B toggles PA5 after queuing the last reply byte: 7-byte request at A's baud, then 7 reply characters at B's
    t = led_b[1] - req[0]
    lo = 7 * 10 / ua.baud() * 1e9 + 7 * 10 / ub.baud() * 1e9
    assert lo < t < lo + 10 / ub.baud() * 1e9
    assert a.pins.get("PC0").level() == "L"
    assert not [e for e in s.events(kinds={"warn", "fault"})]
    s.close()


def test_board_cli():
    board_session().close()
    r = subprocess.run([sys.executable, "-m", "chipem.cli", "run", "--board", str(LINK_BOARD), "--for", "9ms"],
                       capture_output=True, text=True, cwd=ROOT, timeout=180)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "A| === summary chip A (STM32F411RE)" in out and "B| === summary chip B (STM32F401RE)" in out
    assert "uart A.USART1 -> B.USART6: 28 bytes A->B" in out and "uart B.USART6 -> A.USART1: 37 bytes B->A" in out


# ------------------------------------------------------------------------------------------------ I2S5 (F411)
def test_i2s5_tone_board_uses_plli2sm():
    need(BUILD / "f411_i2s5_tone.elf")
    s = Session.from_board(I2S_BOARD, quiet=True)
    s.run_for(12e6)
    a = s.chips["A"]
    assert s.uart_output("A.USART2") == b"I2S5 54002010 tx 256 ok\r\n"      # bit 26 reserved, resets 1 (RM0383 6.3.20)
    eng = a.peripherals["SPI5"].i2s
    # PLLI2SCLK = 16 MHz / PLLI2SM 16 x 128 / 5 = 25.6 MHz; Fs = 25.6 MHz / (32 x 25) = 32 kHz
    assert a.peripherals["RCC"].i2s_clk_hz() == pytest.approx(25.6e6)
    assert eng.fs_hz() == pytest.approx(32000)
    fr = s.board.parts["CODEC"].frames
    assert len(fr) >= 120 and (7071, -7071) in fr and (10000, -10000) in fr
    assert a.pins.get("PB0").owner.endswith("I2S5_CK")
    assert not [e for e in s.events(kinds={"warn", "fault"})]
    s.close()


# ------------------------------------------------------------------------------------------------ HAL
@pytest.mark.parametrize("fn,elf,text", [
    ("stm32f401re", "f401_clock_uart_hal", b"HAL F4x1 sys 84000000 p1 42000000 p2 84000000\r\n"),
    ("stm32f411re", "f411_clock_uart_hal", b"HAL F4x1 sys 100000000 p1 50000000 p2 100000000 i2s 96000000\r\n"),
])
def test_hal_clock_uart(fn, elf, text):
    path = need(HAL_BUILD / f"{elf}.elf")
    s = Session(CHIPS / f"{fn}.yaml", path)
    s.schedule_uart(13e6, "USART2", b"ok")                             # after the banner (64 bytes at 115200)
    s.run_for(14e6)
    assert s.uart_output("USART2") == text + b"OK"
    chip = s.chip
    assert chip.bus.read(sym(path, "hal_status"), 4) == 0
    assert chip.bus.read(sym(path, "delay_ticks"), 4) in (5, 6)          # HAL_Delay adds one tick (HAL_MAX_DELAY rule)
    assert rd_dck(chip) & (1 << 24)                                     # HAL_RCCEx_PeriphCLKConfig: TIMPRE
    if "411" in fn:
        assert chip.peripherals["RCC"].i2s_clk_hz() == pytest.approx(96e6)   # PLLI2SM 8 x 192 / 2 from HSE 8 MHz
    assert not [e for e in s.events(kinds={"warn", "fault"})]
    s.close()
