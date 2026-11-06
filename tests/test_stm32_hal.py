"""End-to-end runs of firmware/samples_stm32_hal/build/uart_chat_hal.elf: the "chat" protocol (HELLO / PING / PONG)
written on ST's official STM32CubeF4 HAL (SystemClock_Config HSE 8 MHz + PLL -> 168 MHz, HAL_Init SysTick 1 kHz,
HAL_UART on USART1 with HAL_UART_Receive_IT, HAL_GPIO on PB0/PB1).  Skipped when the ELF is not built
(firmware/samples_stm32_hal/build.sh needs the vendor HAL under firmware/vendor/STM32CubeF4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from chipem.chip import Chip
from chipem.chipdesc import load_chip
from chipem.firmware import load_firmware
from chipem.session import Session

ROOT = Path(__file__).resolve().parent.parent
ELF = ROOT / "firmware" / "samples_stm32_hal" / "build" / "uart_chat_hal.elf"
YAML = ROOT / "chips" / "stm32f405zg.yaml"
C2000_BUILD = ROOT / "firmware" / "samples_c2000" / "build"
BOARD = ROOT / "boards" / "c2000_stm32_chat_hal.yaml"

RCC, FLASH_IF, PWR = 0x40023800, 0x40023C00, 0x40007000
SYST_RVR = 0xE000E014


@pytest.fixture(scope="module")
def desc():
    return load_chip(YAML)


def need_elf():
    if not ELF.exists():
        pytest.skip(f"{ELF} not built (firmware/samples_stm32_hal/build.sh)")


def boot(desc) -> Chip:
    need_elf()
    chip = Chip(desc)
    chip.load_firmware(load_firmware(ELF))
    chip.reset()
    return chip


def sym(name: str) -> int:
    from elftools.elf.elffile import ELFFile
    with open(ELF, "rb") as f:
        s = ELFFile(f).get_section_by_name(".symtab").get_symbol_by_name(name)
    return s[0]["st_value"]


def tx_lines(log, uart: str = "USART1") -> list[tuple[float, str]]:
    """(time of the first byte, text) for every line the firmware transmitted."""
    out, cur, t0 = [], "", None
    for e in log:
        if e.kind != "uart" or e.fields.get("dir") != "tx" or e.fields.get("uart", uart) != uart:
            continue
        if t0 is None:
            t0 = e.t_ns
        ch = chr(e.fields["byte"])
        if ch == "\n":
            out.append((t0, cur.rstrip("\r")))
            cur, t0 = "", None
        else:
            cur += ch
    return out


def warns(chip) -> list[str]:
    return [e.fields.get("msg", "") for e in chip.log if e.kind == "warn"]


def test_boots_through_system_clock_config_to_168mhz(desc):
    chip = boot(desc)
    chip.run_for(2e6)
    rcc = chip.peripherals["RCC"]
    assert chip.clock.hz == 168e6 and rcc.core_hz == 168e6
    assert (rcc.apb1_hz, rcc.apb2_hz) == (42e6, 84e6)
    rd = chip.bus.read
    cr, cfgr = rd(RCC + 0x00, 4), rd(RCC + 0x08, 4)
    assert cr & (1 << 17) and cr & (1 << 25)                     # HSERDY, PLLRDY
    assert (cfgr >> 2) & 3 == 2                                  # SWS = PLL
    assert rd(RCC + 0x04, 4) & 0x7FFF == 8 | 336 << 6            # PLLM 8, PLLN 336, PLLP /2
    assert rd(FLASH_IF, 4) & 0xF == 5                            # 5 wait states
    assert rd(PWR, 4) & (1 << 14)                                # VOS scale 1
    assert rd(sym("SystemCoreClock"), 4) == 168_000_000
    assert rd(SYST_RVR, 4) == 168_000 - 1                        # HAL tick: 1 kHz
    assert chip.bus.read(0xE000ED08, 4) == 0x08000000            # VTOR set by CMSIS SystemInit
    assert chip.pins.get("PA9").owner == "USART1_TX" and chip.pins.get("PA10").owner == "USART1_RX"
    assert not warns(chip)


def test_hello_ping_every_5ms_and_hal_tick(desc):
    chip = boot(desc)
    chip.run_for(32e6)
    lines = tx_lines(chip.log)
    assert lines[0][1] == "HELLO STM32"
    pings = [(t, s) for t, s in lines if s.startswith("PING ")]
    assert [s for _, s in pings] == [f"PING {n}" for n in range(1, len(pings) + 1)]
    assert len(pings) == 5                                       # first at ~8 ms (HAL_Delay + HELLO), then every 5 ms
    gaps = [b[0] - a[0] for a, b in zip(pings, pings[1:])]
    assert all(abs(g - 5e6) <= 0.05 * 5e6 for g in gaps), gaps
    assert 4e6 <= pings[0][0] <= 9e6                             # first PING ~5 ms after HELLO
    assert chip.bus.read(sym("uwTick"), 4) in range(30, 33)      # HAL tick counter ~= ms since HAL_Init
    assert chip.pins.get("PB1").level() == "L"                   # no peer yet


def test_pong_reply_led_toggle_and_peer_seen(desc):
    chip = boot(desc)
    chip.run_for(2e6)
    pb0: list = []
    chip.pins.subscribe(lambda e: pb0.append(e.new) if e.pin == "PB0" and e.field == "level" else None)
    u = chip.peripherals["USART1"]
    u.feed(b"HELLO C2000\r\nPING 7\r\nPONG 1\r\n\nJUNK 3\r\nPONG 2\r\nPING 12\r\n")
    chip.run_for(10e6)
    lines = [s for _, s in tx_lines(chip.log)]
    assert "PONG 7" in lines and "PONG 12" in lines
    assert lines.count("HELLO STM32") == 2                       # boot + answer to the first peer HELLO
    assert chip.pins.get("PB1").level() == "H"
    assert pb0 == ["H", "L"]                                     # toggled once per PONG
    rd = chip.bus.read
    assert (rd(sym("pongs"), 4), rd(sym("pings_rx"), 4), rd(sym("peer_seen"), 4)) == (2, 2, 1)
    assert rd(sym("uart_errors"), 4) == 0
    # a second HELLO is not answered again
    u.feed(b"HELLO again\r\n")
    chip.run_for(3e6)
    assert [s for _, s in tx_lines(chip.log)].count("HELLO STM32") == 2
    assert not warns(chip)


def _board_or_skip() -> Path:
    need_elf()
    if not (C2000_BUILD / "uart_chat.c28img").exists():
        pytest.skip("firmware/samples_c2000/build/uart_chat.c28img not built")
    return BOARD


def test_board_net_auto_links_with_c2000_sender(tmp_path):
    """Board wiring of boards/c2000_stm32_chat_hal.yaml with the older C2000 uart_link_sender ("ping N", which the
    chat firmware ignores): both nets become auto UART links and the STM32 receives every byte by interrupt."""
    need_elf()
    sender = C2000_BUILD / "uart_link_sender.c28img"
    if not sender.exists():
        pytest.skip(f"{sender} not built")
    text = BOARD.read_text().replace("../firmware/samples_c2000/build/uart_chat.c28img", str(sender)) \
        .replace("../firmware/samples_stm32_hal/build/uart_chat_hal.elf", str(ELF))
    b = tmp_path / "b.yaml"
    b.write_text(text)
    s = Session.from_board(b, quiet=True)
    s.run_for(12e6)
    links = {(lk.a[0], lk.b[0]) if hasattr(lk, "a") else str(lk) for lk in s.board.links}
    assert len(s.board.links) == 2 and all(getattr(lk, "auto", False) for lk in s.board.links), links
    stm = s.chips["B"]
    got = stm.bus.read(sym("pings_rx"), 4)
    assert got == 0                                              # lowercase "ping" lines are ignored
    assert s.uart_output("A.SCIA").startswith(b"ping 1\r\n")
    assert s.uart_output("B.USART1").startswith(b"HELLO STM32\r\nPING 1\r\n")


def test_board_chat_with_c2000():
    s = Session.from_board(_board_or_skip(), quiet=True)
    s.run_for(40e6)
    stm, c2k = s.chips["B"], s.chips["A"]
    rd = stm.bus.read
    assert stm.clock.hz == 168e6
    assert rd(sym("peer_seen"), 4) == 1 and stm.pins.get("PB1").level() == "H"
    assert rd(sym("pings_rx"), 4) >= 4 and rd(sym("pongs"), 4) >= 4
    assert c2k.pins.get("GPIO35").level() == "H"
    out = s.uart_output("A.SCIA")
    assert b"HELLO C2000" in out and b"PONG 1\r\n" in out
